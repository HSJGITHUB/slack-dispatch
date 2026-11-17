"""FastAPI application for the local operator console (P10).

Local operator tool: no authentication, 127.0.0.1 by default, `Cache-Control: no-store`
on every response, Host and Origin checks against DNS rebinding and cross-site posts.
Writes go through `slack_dispatch.console.actions`, which mirrors the inbox CLI.
Since P19 every write that changes message rows is journaled and can be undone for
`console.undo_seconds` (`POST /api/undo/{id}`, `POST /api/undo/last`, `GET /api/actions`).
Since P20 every page build binds the view to the store (`view.bind(store)`), so user and
channel names follow `queries.DisplayNames` (config, then ledger, then id) with one read per
table per request, and message rows carry Slack permalinks when
`console.workspace_domain` is set.
Since P21 lanes carry the session identity (`SessionResolver.describe`: sessions table title,
live name, id8) and `POST /api/lanes/{lane}/wake` re-delivers a lane's open items and queues
wake requests (journaled, undoable), refused while the lane is paused or not live.
Since P25 the triage queue is the operator queue: `human_pending` items plus the open items
of the direct lane (`queue_kind` `undecided` or `direct`, `GET /api/queue?kind=`, `GET
/api/queue/counts`); queue writes accept both kinds (an assign of a direct row reroutes it).
Nothing here logs request lines, queries or message bodies.
"""
import logging
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from slack_dispatch import __version__
from slack_dispatch.console import actions, queries
from slack_dispatch.inbox.cli import wake_lane
from slack_dispatch.ledger.store import ProposalStateError, UndoStateError
from slack_dispatch.notify.session_resolve import SessionResolver
from slack_dispatch.resolver.owner import OwnerResolver

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
POLL_SECONDS = 20
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PAGES = ("/", "/lanes", "/service", "/proposals")


class QueueActionRequest(BaseModel):
    action: Literal["assign", "direct", "exclude", "exclude_channel", "done"]
    ids: list[str] = Field(min_length=1, max_length=actions.MAX_IDS)
    lane: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


class CommitDecision(BaseModel):
    """One staged triage decision (P14)."""
    id: str = Field(min_length=1, max_length=500)
    action: Literal["assign", "direct", "exclude", "exclude_channel", "done"]
    lane: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


class QueueCommitRequest(BaseModel):
    """P14 GO: every staged decision, applied in order."""
    decisions: list[CommitDecision] = Field(min_length=1, max_length=actions.MAX_IDS)


class ProposalActionRequest(BaseModel):
    """P13 proposal decision: confirm, reject or defer one (kind, key, lane) proposal."""
    action: Literal["confirm", "reject", "defer"]
    kind: Literal["channel", "dm", "person"]
    key: str = Field(min_length=1, max_length=100)
    lane: str = Field(min_length=1, max_length=100)


class ChannelExclusionRequest(BaseModel):
    """P15: undo the ledger exclusion of one channel."""
    channel: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9]{1,63}$")


class LaneActionRequest(BaseModel):
    action: Literal["ack", "done", "reroute"]
    ids: list[str] = Field(min_length=1, max_length=actions.MAX_IDS)
    lane: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    # P24: reroute only; also move the thread's open items in the old lane and the binding.
    thread: bool = False


class UndoRequest(BaseModel):
    """P19 undo: the body is an empty JSON object, so a cross-site form cannot post it."""


class WakeRequest(BaseModel):
    """P21 wake now: the ids to wake, or every assigned and acked item of the lane."""
    ids: list[str] | None = Field(default=None, min_length=1, max_length=actions.MAX_IDS)


def allowed_hosts(config, host=None):
    """Host names accepted in Host and Origin headers: loopback, the bind host, base_url host."""
    names = set(LOCAL_HOSTS)
    for value in (host, config.console.host):
        if value:
            names.add(value.strip("[]").lower())
    if config.console.base_url:
        hostname = urlsplit(config.console.base_url).hostname
        if hostname:
            names.add(hostname.lower())
    return names


def _hostname(value):
    try:
        return (urlsplit("//" + value).hostname or "").lower()
    except ValueError:
        return ""


class ConsoleMiddleware:
    """Set no-store on every response, check Host/Origin, and hide internal errors."""

    def __init__(self, app, hosts):
        self.app = app
        self.hosts = frozenset(hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_no_store(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = dict(message, headers=headers)
            await send(message)

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = _hostname(headers.get(b"host", b"").decode("latin-1"))
        origin = headers.get(b"origin")
        if host not in self.hosts:
            await self._plain(send_no_store, 403, b'{"detail":"Host not allowed"}')
            return
        if scope.get("method") not in ("GET", "HEAD") and origin is not None:
            origin_host = urlsplit(origin.decode("latin-1")).hostname or ""
            if origin_host.lower() not in self.hosts:
                await self._plain(send_no_store, 403, b'{"detail":"Origin not allowed"}')
                return
        try:
            await self.app(scope, receive, send_no_store)
        except Exception as error:
            # Class name only: exception text could carry ledger content.
            LOG.error("Console request failed class=%s", type(error).__name__)
            if not started:
                await self._plain(send_no_store, 500, b'{"detail":"Internal error"}')

    @staticmethod
    async def _plain(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def create_app(config, store, *, clock=time.time, resolver=None, host=None, owner=None):
    """Build the console app around an open ledger store and a loaded config.

    `owner` is the P17 `OwnerResolver`; by default one is built when
    `owner_resolver.enabled` (registry badges, `registry_closed`, `GET /api/owner`)."""
    app = FastAPI(title="slack-dispatch console", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None)
    view = queries.View(config)
    root = queries.inbox_root(config)
    resolver = resolver or SessionResolver(config.sessions_dir,
                                           sessions_table=config.console.sessions_table)
    titles = queries.session_titles(resolver)  # P21: None without a sessions table
    lanes = queries.lane_ids(config)
    threshold = config.scope_learning.min_evidence
    undo_seconds = config.console.undo_seconds
    if owner is None and config.owner_resolver.enabled:
        owner = OwnerResolver(config)
    app.state.config, app.state.store, app.state.view = config, store, view
    app.state.owner = owner

    def checked_lane(lane, *, registered=True):
        if lane not in lanes:
            raise HTTPException(400, "Unknown lane")
        if registered and store.get_lane(lane) is None:
            raise HTTPException(404, "Lane not found in ledger")
        return lane

    def page():
        return FileResponse(STATIC / "console.html", media_type="text/html; charset=utf-8")

    for path in PAGES:
        app.add_api_route(path, page, methods=["GET"], include_in_schema=False)

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str):
        types = {"console.js": "text/javascript; charset=utf-8",
                 "console.css": "text/css; charset=utf-8"}
        if name not in types:
            raise HTTPException(404, "Not found")
        return FileResponse(STATIC / name, media_type=types[name])

    @app.get("/api/config")
    def console_config():
        return {"version": __version__, "lanes": lanes, "direct_lane": queries.direct_lane(config),
                "page_size": config.console.page_size, "poll_seconds": POLL_SECONDS,
                "display_timezone": config.display_timezone,
                "timezone_warning": view.zone_warning, "store_body": config.ledger.store_body,
                "lane_hint_people": config.console.lane_hint_people,
                "ui_language": config.console.ui_language,
                "owner_resolver": owner is not None, "undo_seconds": undo_seconds,
                "permalinks": view.domain is not None,
                # P25: the operator's name (people name of always_human.owner_user) for labels.
                "operator_name": queries.operator_name(config)}

    @app.get("/api/queue")
    def queue(offset: int = Query(0, ge=0), limit: int | None = Query(None, ge=1, le=500),
              kind: Literal["all", "undecided", "direct"] = "all"):
        # P21: lane button tooltips name each lane's session.
        identities = queries.lane_identities(store, config, resolver)
        direct = queries.direct_lane(config)
        live = identities[direct]["live"] if direct in identities else None
        # P25: kind filters the rows; counts and pending_ids always cover both kinds.
        data = queries.queue_page(store, config, view.bind(store), clock(), offset=offset,
                                  limit=limit, owner=owner, kind=kind, session_live=live)
        data["lane_sessions"] = identities
        return data

    @app.get("/api/queue/counts")
    def queue_counts():
        """P25: `{undecided, direct, total}` for the queue tab label (polled on every view)."""
        counts = queries.queue_counts(store, config)
        return dict(counts, total=sum(counts.values()))

    def queue_guard():
        """P25: the operator queue guard (human_pending, or open items of the direct lane)."""
        return actions.operator_guard(queries.direct_lane(config))

    @app.post("/api/queue/actions")
    def queue_actions(request: QueueActionRequest):
        lane = None
        if request.action == "assign":
            if not request.lane:
                raise HTTPException(400, "Lane is required")
            lane = checked_lane(request.lane)
        elif request.action == "direct":
            lane = queries.direct_lane(config)
            if lane is None:
                raise HTTPException(400, "Direct lane is not configured")
            checked_lane(lane)
        note = (request.note or "").strip() or None
        journal = actions.Journal(store, request.action, lane=lane)
        results = actions.apply(store, root, request.action, request.ids, lane=lane, note=note,
                                guard=queue_guard(), min_evidence=threshold, journal=journal,
                                direct_lane=queries.direct_lane(config))
        undo = journal.finish(undo_seconds, clock())
        bound = view.bind(store)  # P20: one name cache for the whole response
        if request.action == "exclude":
            for result in results:
                add_channel_hint(result, result["ok"], bound)
        if request.action == "exclude_channel":
            for result in results:
                add_excluded_channel(result, result["ok"], bound)
        return dict(summary(results, lane), undo=undo, journal=journal.status)

    def add_excluded_channel(row, applied, bound):
        """P15: name the channel an applied exclude_channel closed."""
        message = store.get_message(row["id"]) if applied else None
        if message:
            row["channel"] = bound.channel(message["channel"])

    def add_channel_hint(row, applied, bound):
        """P14: flag an applied exclude whose channel has no lane default."""
        message = store.get_message(row["id"]) if applied else None
        row["channel_hint"] = bool(message) and queries.channel_hint(store, config, message)
        if row["channel_hint"]:
            row["channel"] = bound.channel(message["channel"])

    def decision_lane(action, lane):
        """(destination lane, None) for a staged decision, or (None, error detail)."""
        if action == "assign":
            if not lane:
                return None, "lane is required"
            if lane not in lanes:
                return None, "unknown lane"
        elif action == "direct":
            lane = queries.direct_lane(config)
            if lane is None:
                return None, "direct lane is not configured"
        else:
            return None, None
        if store.get_lane(lane) is None:
            return None, "lane not found in ledger"
        return lane, None

    @app.post("/api/queue/commit")
    def queue_commit(request: QueueCommitRequest):
        """P14 GO: apply each staged decision in order through the same action functions.

        One result per decision, in request order: applied, conflict (handled meanwhile),
        not_found or error. A failure never stops the rest; ids not sent are untouched.
        """
        results, seen = [], set()
        journal = actions.Journal(store, "commit")
        bound = view.bind(store)  # P20: one name cache for the whole response
        for decision in request.decisions:
            row = {"id": decision.id, "action": decision.action, "lane": None,
                   "status": "error", "detail": None}
            results.append(row)
            if decision.id in seen:
                row["detail"] = "duplicate id"
                continue
            seen.add(decision.id)
            lane, problem = decision_lane(decision.action, decision.lane)
            if problem:
                row["detail"] = problem
                continue
            note = (decision.note or "").strip() or None
            result = actions.apply(store, root, decision.action, [decision.id], lane=lane,
                                   note=note, guard=queue_guard(),
                                   min_evidence=threshold, journal=journal,
                                   direct_lane=queries.direct_lane(config))[0]
            status = actions.commit_status(result)
            row.update(lane=lane, status=status,
                       detail=result["detail"] or (result["error"] if status == "error" else None))
            if decision.action == "exclude":
                add_channel_hint(row, row["status"] == "applied", bound)
            if decision.action == "exclude_channel":
                row["closed"] = result.get("closed", 0)
                add_excluded_channel(row, row["status"] == "applied", bound)
        counts = {name: sum(row["status"] == name for row in results)
                  for name in actions.COMMIT_STATUSES}
        # P19: one journal row for the whole commit (null when nothing changed). A journal
        # failure never changes the commit; `journal` says `failed` then (fix1).
        undo = journal.finish(undo_seconds, clock())
        return {"results": results, "counts": counts, "undo": undo, "journal": journal.status}

    @app.get("/api/lanes")
    def lane_list():
        # P25: `direct_lane` says whether the direct lane card is listed or the Lanes tab
        # links to the queue instead (no session attached).
        cards = queries.lane_cards(store, config, view, resolver, root, clock())
        direct = next((card for card in cards if card["direct"]), None)
        live = (direct.get("identity") or {}).get("live") if direct else None
        return {"lanes": cards, "direct_lane": queries.direct_lane_info(store, config, live)}

    @app.get("/api/lanes/{lane}/inbox")
    def lane_inbox(lane: str, full: bool = False):
        checked_lane(lane, registered=False)
        session, paused = queries.effective_lane(config, store, lane)
        return {"lane": lane, "groups": queries.lane_inbox(store, config, view.bind(store), root,
                                                           lane, full=full, owner=owner),
                # P21: the lane header shows the session identity and the Wake now state.
                "identity": queries.session_identity(resolver, session, paused),
                "wake": queries.lane_wake_info(config, store, resolver, lane)}

    @app.post("/api/lanes/{lane}/wake")
    def lane_wake(lane: str, request: WakeRequest):
        """P21 Wake now: re-deliver the given ids (default every assigned and acked item of
        the lane) as `backfill` inbox records and queue one wake request each, journaled as
        `lane_wake` so P19 undo removes the unconsumed requests. 409 when refused."""
        checked_lane(lane)
        refusal = queries.wake_refusal(config, store, resolver, lane)
        if refusal:
            raise HTTPException(409, refusal[1])
        result = wake_lane(store, root, lane, request.ids, source=actions.JOURNAL_BY,
                           undo_seconds=undo_seconds, now=clock())
        return result

    @app.post("/api/lanes/{lane}/actions")
    def lane_actions(lane: str, request: LaneActionRequest):
        checked_lane(lane, registered=False)
        target = None
        if request.action == "reroute":
            if not request.lane:
                raise HTTPException(400, "Lane is required")
            target = checked_lane(request.lane)
        note = (request.note or "").strip() or None
        journal = actions.Journal(store, "lane_" + request.action, lane=target or lane)
        results = actions.apply(store, root, request.action, request.ids, lane=target, note=note,
                                guard=actions.lane_guard(lane), min_evidence=threshold,
                                journal=journal,
                                thread=request.thread and request.action == "reroute")
        undo = journal.finish(undo_seconds, clock())
        return dict(summary(results, target), undo=undo, journal=journal.status)

    # P19 undo: recent journal rows for the Service tab, undo one row or the last one.

    @app.get("/api/actions")
    def recent_actions(limit: int = Query(20, ge=1, le=200)):
        return {"undo_seconds": undo_seconds,
                "actions": queries.recent_actions(store, view.bind(store), clock(), limit=limit)}

    def undo_response(run):
        try:
            return run()
        except KeyError:
            raise HTTPException(404, "Action not found") from None
        except UndoStateError as error:
            detail = "Already undone" if error.code == "undone" else "Undo window expired"
            raise HTTPException(409, detail) from None

    @app.post("/api/undo/last")
    def undo_last(request: UndoRequest):
        row = actions.last_undoable(store, clock())
        if row is None:
            raise HTTPException(404, "Nothing to undo")
        return undo_response(lambda: actions.undo(store, row["id"], now=clock()))

    @app.post("/api/undo/{action_id}")
    def undo_action(action_id: int, request: UndoRequest):
        return undo_response(lambda: actions.undo(store, action_id, now=clock()))

    @app.get("/api/service")
    def service():
        return queries.service_status(store, config, view, clock())

    @app.get("/api/search")
    def search(q: str = Query("", max_length=200)):
        query = q.strip()
        if len(query) < 2:
            raise HTTPException(400, "Query needs at least 2 characters")
        return queries.search(store, view.bind(store), query)

    @app.get("/api/owner")
    def owner_candidates(channel: str = Query(..., max_length=100),
                         ts: str | None = Query(None, max_length=40)):
        """P17: owner candidates of a channel, or of a message or thread with `ts`. Read-only."""
        if owner is None:
            raise HTTPException(404, "Owner resolver is disabled")
        try:
            return queries.owner_lookup(store, config, owner, channel.strip(),
                                        (ts or "").strip() or None, titles=titles)
        except ValueError:
            raise HTTPException(400, "Invalid channel or ts") from None

    @app.get("/api/proposals")
    def proposal_list():
        return queries.proposals(store, config, view.bind(store))

    @app.post("/api/proposals/actions")
    def proposal_action(request: ProposalActionRequest):
        if request.action == "confirm":
            # A confirmed claim becomes routing input, so its lane must exist everywhere.
            checked_lane(request.lane)
        try:
            row = store.decide_scope_proposal(request.kind, request.key, request.lane, request.action,
                                              by="console", min_evidence=threshold)
        except KeyError:
            raise HTTPException(404, "Proposal not found") from None
        except ProposalStateError as error:
            raise HTTPException(409, "Proposal is " + str(error)) from None
        return {"kind": row["kind"], "key": row["key"], "lane": row["lane"], "status": row["status"]}

    @app.get("/api/proposals/export")
    def proposal_export():
        return {"yaml": queries.export_yaml(store, config, view.bind(store))}

    @app.post("/api/channel-exclusions/undo")
    def channel_exclusion_undo(request: ChannelExclusionRequest):
        """P15: end the ledger exclusion; items it closed stay excluded."""
        if not store.remove_channel_exclusion(request.channel):
            raise HTTPException(404, "No active channel exclusion")
        return {"channel": request.channel, "removed": True}

    @app.get("/api/channel-exclusions/export")
    def channel_exclusion_export():
        return {"yaml": queries.export_exclusions_yaml(store, config, view.bind(store))}

    # P18: delivery log tab (page, rows with receipts, window summary). Read-only.
    # P28: each row's items carry author, channel, time, attention flag and a body preview,
    # read from the ledger once per page.
    app.add_api_route("/deliveries", page, methods=["GET"], include_in_schema=False)

    def delivery_filters(lane, purpose, status):
        if lane and lane not in [row.id for row in config.lanes] + [queries.HUMAN_QUEUE]:
            raise HTTPException(400, "Unknown lane")
        if purpose and purpose not in queries.DELIVERY_PURPOSES:
            raise HTTPException(400, "Unknown purpose")
        if status and status not in queries.DELIVERY_STATUS_FILTERS:
            raise HTTPException(400, "Unknown status")
        return lane or None, purpose or None, status or None

    @app.get("/api/deliveries")
    def deliveries(lane: str = Query("", max_length=100), purpose: str = Query("", max_length=20),
                   status: str = Query("", max_length=30),
                   hours: int = Query(queries.DELIVERY_DEFAULT_HOURS, ge=0, le=24 * 366),
                   page: int = Query(1, ge=1, le=100000)):
        lane, purpose, status = delivery_filters(lane, purpose, status)
        return queries.deliveries_page(store, config, view.bind(store), clock(), lane=lane,
                                       purpose=purpose, status=status, hours=hours, page=page,
                                       titles=titles)

    @app.get("/api/deliveries/summary")
    def deliveries_summary(hours: int = Query(queries.DELIVERY_DEFAULT_HOURS, ge=0, le=24 * 366)):
        return queries.deliveries_summary(store, clock(), hours=hours)

    app.add_middleware(ConsoleMiddleware, hosts=allowed_hosts(config, host))
    return app


def summary(results, lane):
    applied = sum(result["ok"] for result in results)
    return {"lane": lane, "applied": applied, "failed": len(results) - applied, "results": results}
