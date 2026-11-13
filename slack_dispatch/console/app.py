"""FastAPI application for the local operator console (P10).

Local operator tool: no authentication, 127.0.0.1 by default, `Cache-Control: no-store`
on every response, Host and Origin checks against DNS rebinding and cross-site posts.
Writes go through `slack_dispatch.console.actions`, which mirrors the inbox CLI.
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
from slack_dispatch.ledger.store import ProposalStateError
from slack_dispatch.notify.session_resolve import SessionResolver

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
POLL_SECONDS = 20
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PAGES = ("/", "/lanes", "/service", "/proposals")


class QueueActionRequest(BaseModel):
    action: Literal["assign", "direct", "exclude", "done"]
    ids: list[str] = Field(min_length=1, max_length=actions.MAX_IDS)
    lane: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


class CommitDecision(BaseModel):
    """One staged triage decision (P14)."""
    id: str = Field(min_length=1, max_length=500)
    action: Literal["assign", "direct", "exclude", "done"]
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


class LaneActionRequest(BaseModel):
    action: Literal["ack", "done", "reroute"]
    ids: list[str] = Field(min_length=1, max_length=actions.MAX_IDS)
    lane: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


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


def create_app(config, store, *, clock=time.time, resolver=None, host=None):
    """Build the console app around an open ledger store and a loaded config."""
    app = FastAPI(title="slack-dispatch console", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None)
    view = queries.View(config)
    root = queries.inbox_root(config)
    resolver = resolver or SessionResolver(config.sessions_dir)
    lanes = queries.lane_ids(config)
    threshold = config.scope_learning.min_evidence
    app.state.config, app.state.store, app.state.view = config, store, view

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
                "ui_language": config.console.ui_language}

    @app.get("/api/queue")
    def queue(offset: int = Query(0, ge=0), limit: int | None = Query(None, ge=1, le=500)):
        return queries.queue_page(store, config, view, clock(), offset=offset, limit=limit)

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
        results = actions.apply(store, root, request.action, request.ids, lane=lane, note=note,
                                guard=actions.queue_guard, min_evidence=threshold)
        if request.action == "exclude":
            for result in results:
                add_channel_hint(result, result["ok"])
        return summary(results, lane)

    def add_channel_hint(row, applied):
        """P14: flag an applied exclude whose channel has no lane default."""
        message = store.get_message(row["id"]) if applied else None
        row["channel_hint"] = bool(message) and queries.channel_hint(store, config, message)
        if row["channel_hint"]:
            row["channel"] = view.channel(message["channel"])

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
                                   note=note, guard=actions.queue_guard,
                                   min_evidence=threshold)[0]
            status = actions.commit_status(result)
            row.update(lane=lane, status=status,
                       detail=result["detail"] or (result["error"] if status == "error" else None))
            if decision.action == "exclude":
                add_channel_hint(row, row["status"] == "applied")
        counts = {name: sum(row["status"] == name for row in results)
                  for name in actions.COMMIT_STATUSES}
        return {"results": results, "counts": counts}

    @app.get("/api/lanes")
    def lane_list():
        return {"lanes": queries.lane_cards(store, config, view, resolver, root, clock())}

    @app.get("/api/lanes/{lane}/inbox")
    def lane_inbox(lane: str, full: bool = False):
        checked_lane(lane, registered=False)
        return {"lane": lane, "groups": queries.lane_inbox(store, config, view, root, lane, full=full)}

    @app.post("/api/lanes/{lane}/actions")
    def lane_actions(lane: str, request: LaneActionRequest):
        checked_lane(lane, registered=False)
        target = None
        if request.action == "reroute":
            if not request.lane:
                raise HTTPException(400, "Lane is required")
            target = checked_lane(request.lane)
        note = (request.note or "").strip() or None
        results = actions.apply(store, root, request.action, request.ids, lane=target, note=note,
                                guard=actions.lane_guard(lane), min_evidence=threshold)
        return summary(results, target)

    @app.get("/api/service")
    def service():
        return queries.service_status(store, config, view, clock())

    @app.get("/api/search")
    def search(q: str = Query("", max_length=200)):
        query = q.strip()
        if len(query) < 2:
            raise HTTPException(400, "Query needs at least 2 characters")
        return queries.search(store, view, query)

    @app.get("/api/proposals")
    def proposal_list():
        return queries.proposals(store, config, view)

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
        return {"yaml": queries.export_yaml(store, config, view)}

    app.add_middleware(ConsoleMiddleware, hosts=allowed_hosts(config, host))
    return app


def summary(results, lane):
    applied = sum(result["ok"] for result in results)
    return {"lane": lane, "applied": applied, "failed": len(results) - applied, "results": results}
