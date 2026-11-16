"""Socket Mode is primary intake. ACK precedes normalization and callbacks."""
from datetime import datetime
import threading
import logging
import time

from .normalize import normalize_slack_event
from .tokens import load_token, quiet_logger

LOG = logging.getLogger(__name__)


class SlackSocketSource:
    def __init__(self, config, on_event, env_file, app_token_key):
        self.config, self.on_event = config, on_event
        self.env_file, self.app_token_key = env_file, app_token_key
        self.client = None
        self.disconnects = 0
        self.failures = 0
        self._stopped = threading.Event()
        self._thread = None
        self._counts_lock = threading.Lock()
        self._opened = False
        self._down = False
        self.reconnects = 0
        self.disconnected_at = None
        self.on_connection = None
        self.clock = time.time

    def _observe(self, connected):
        transition = None
        with self._counts_lock:
            if connected:
                if self._down:
                    transition = "reconnected"
                    self.reconnects += 1
                elif not self._opened:
                    transition = "connected"
                self._opened, self._down = True, False
                self.disconnected_at = None
            elif self._opened and not self._down:
                self.disconnects += 1
                self._down = True
                self.disconnected_at = self.clock()
                transition = "disconnected"
        if transition:
            LOG.info("Socket %s", transition)
            if self.on_connection:
                self.on_connection(transition, self.clock())

    def _receive(self, client, request):
        try:
            client.send_socket_mode_response(self._response_class(envelope_id=request.envelope_id))
            if request.type == "disconnect":
                self._observe(False)
                return
            if request.type != "events_api" or self._stopped.is_set():
                return
            payload = dict(request.payload)
            retry = getattr(request, "retry_attempt", None)
            if retry is not None:
                payload["retry_attempt"] = retry
            event = normalize_slack_event(payload, datetime.now().astimezone().isoformat(),
                                          store_body=self.config.store_body)
            if event is not None:
                event["origin"] = "socket"
                # P16: the bot flag is decided once, in the pipeline (user_directory rule).
                self.on_event(event)
        except Exception as error:
            LOG.error("Socket callback class=%s operation=receive", type(error).__name__)
            # No exception text: it may contain tokens or message bodies.
            self.failures += 1

    def start(self):
        if self.client is not None:
            return
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        token = load_token(self.env_file, self.app_token_key, "xapp-")
        self._response_class = SocketModeResponse
        self._stopped.clear()
        try:
            logger = quiet_logger()
            web_client = WebClient(token=None, logger=logger)
            self.client = SocketModeClient(app_token=token, web_client=web_client, logger=logger)
            self.client.socket_mode_request_listeners.append(self._receive)
            self.client.connect()
            self._observe(self.client.is_connected())
        except Exception:
            self.stop()
            raise RuntimeError("Socket startup failed") from None

        def monitor():
            while not self._stopped.wait(0.2):
                try:
                    self._observe(self.client.is_connected())
                except Exception:
                    self.failures += 1
        self._thread = threading.Thread(target=monitor, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopped.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                self.failures += 1
            self.client = None
