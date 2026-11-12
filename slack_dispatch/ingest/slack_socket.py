"""Socket Mode is primary intake. ACK precedes normalization and callbacks."""
from datetime import datetime
import threading

from .normalize import normalize_slack_event
from .tokens import load_token, quiet_logger


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

    def _observe(self, connected):
        with self._counts_lock:
            if connected:
                self._opened, self._down = True, False
            elif self._opened and not self._down:
                self.disconnects += 1
                self._down = True

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
                self.on_event(event)
        except Exception:
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
