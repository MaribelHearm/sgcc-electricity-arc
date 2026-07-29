#!/usr/bin/env python3
"""Small HTTP manager for on-demand official Google Chrome sidecar.

The app container attaches to Chrome through CDP. This process owns the Chrome
lifecycle so the heavy browser is only running during a fetch/login check.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import atexit
import json
import os
import shutil
import signal
import select
import socket
import subprocess
import threading
import time
from urllib.request import urlopen

HOST = os.getenv("SGCC_BROWSER_SERVICE_HOST", "127.0.0.1")
PORT = int(os.getenv("SGCC_BROWSER_SERVICE_PORT", "39222"))
CDP_HOST = os.getenv("SGCC_BROWSER_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("SGCC_BROWSER_CDP_PORT", "9222"))
CDP_FORWARD_ENABLED = os.getenv("SGCC_BROWSER_CDP_FORWARD_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
_CDP_INTERNAL_PORT_RAW = os.getenv("SGCC_BROWSER_CDP_INTERNAL_PORT", "").strip()
CDP_INTERNAL_PORT = int(_CDP_INTERNAL_PORT_RAW) if _CDP_INTERNAL_PORT_RAW else (CDP_PORT + 1 if CDP_FORWARD_ENABLED else CDP_PORT)
PROFILE_DIR = os.getenv("SGCC_BROWSER_PROFILE", "/data/chrome-profile")
START_URL = os.getenv("SGCC_BROWSER_START_URL", "about:blank")
CHROME_BIN = os.getenv("SGCC_CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable") or "/usr/bin/google-chrome"
DISPLAY = os.getenv("DISPLAY", ":99")
BROWSER_LANGUAGE = os.getenv("BROWSER_LANGUAGE", "zh-CN,zh,en-US,en")
BROWSER_LANGUAGE_PRIMARY = BROWSER_LANGUAGE.split(",", 1)[0].strip() or "zh-CN"
WINDOW_SIZE = os.getenv("BROWSER_WINDOW_SIZE", "1280,900")
START_TIMEOUT = float(os.getenv("SGCC_BROWSER_START_TIMEOUT", "60"))
REVISION = os.getenv("VERSION", "").strip() or "unknown"

_state_lock = threading.RLock()
_proc: subprocess.Popen | None = None
_cdp_forward_proc: tuple[ThreadingHTTPServer, threading.Thread] | None = None
_last_started_at: float | None = None
_last_stopped_at: float | None = None
_last_error: str = ""


def _cdp_probe_host() -> str:
    # Chrome may force remote debugging to loopback even when
    # --remote-debugging-address=0.0.0.0 is passed. Local health checks should
    # always probe the actual Chrome endpoint, not the optional public forwarder.
    if CDP_FORWARD_ENABLED:
        return "127.0.0.1"
    if CDP_HOST in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return CDP_HOST


def _chrome_cdp_host() -> str:
    if CDP_FORWARD_ENABLED:
        return "127.0.0.1"
    return CDP_HOST


def _chrome_cdp_port() -> int:
    return CDP_INTERNAL_PORT if CDP_FORWARD_ENABLED else CDP_PORT


def _cdp_forward_bind_host() -> str:
    if CDP_HOST in {"", "0.0.0.0", "::"}:
        return "0.0.0.0"
    if CDP_HOST == "localhost":
        return "127.0.0.1"
    return CDP_HOST


def _cdp_forward_running() -> bool:
    return _cdp_forward_proc is not None


def _is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _ready() -> bool:
    if not _is_running():
        return False
    try:
        with socket.create_connection((_cdp_probe_host(), _chrome_cdp_port()), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_ready(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ready():
            return True
        if _proc is not None and _proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _clear_profile_locks() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.unlink(os.path.join(PROFILE_DIR, name))
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"warn: remove profile lock {name} failed: {exc}", flush=True)


def _chrome_args() -> list[str]:
    return [
        CHROME_BIN,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-address={_chrome_cdp_host()}",
        f"--remote-debugging-port={_chrome_cdp_port()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate",
        "--password-store=basic",
        "--use-mock-keychain",
        f"--lang={BROWSER_LANGUAGE_PRIMARY}",
        f"--accept-lang={BROWSER_LANGUAGE}",
        f"--window-size={WINDOW_SIZE}",
        START_URL,
    ]


def _rewrite_cdp_payload(body: bytes, public_host: str) -> bytes:
    if not public_host:
        public_host = f"{CDP_HOST}:{CDP_PORT}"
    replacements = {
        f"ws://127.0.0.1:{CDP_INTERNAL_PORT}".encode(): f"ws://{public_host}".encode(),
        f"ws://localhost:{CDP_INTERNAL_PORT}".encode(): f"ws://{public_host}".encode(),
        f"ws://0.0.0.0:{CDP_INTERNAL_PORT}".encode(): f"ws://{public_host}".encode(),
        f"ws=127.0.0.1:{CDP_INTERNAL_PORT}".encode(): f"ws={public_host}".encode(),
        f"ws=localhost:{CDP_INTERNAL_PORT}".encode(): f"ws={public_host}".encode(),
        f"ws=0.0.0.0:{CDP_INTERNAL_PORT}".encode(): f"ws={public_host}".encode(),
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    return body


def _split_http_response(raw: bytes) -> tuple[bytes, bytes, list[tuple[str, str]]]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return raw, b"", []
    lines = head.split(b"\r\n")
    status_line = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if colon:
            headers.append((name.decode("iso-8859-1"), value.decode("iso-8859-1").strip()))
    return status_line, body, headers


def _response_status_code(status_line: bytes) -> int | None:
    try:
        parts = status_line.split(None, 2)
        if len(parts) < 2:
            return None
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def _build_http_response(status_line: bytes, headers: list[tuple[str, str]], body: bytes) -> bytes:
    lines = [status_line]
    skipped = {"content-length", "transfer-encoding", "connection"}
    for name, value in headers:
        if name.lower() in skipped:
            continue
        lines.append(f"{name}: {value}".encode("iso-8859-1"))
    lines.append(f"Content-Length: {len(body)}".encode("ascii"))
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def _relay_streams(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while sockets:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                continue
            for src in readable:
                dst = right if src is left else left
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        return


def _read_until_header_end(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > 1024 * 1024:
            raise RuntimeError("upstream CDP response header is too large")
    return data


class CDPProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"cdp-proxy {self.client_address[0]} - {fmt % args}", flush=True)

    def _proxy(self) -> None:
        public_host = self.headers.get("Host", f"{CDP_HOST}:{CDP_PORT}")
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length:
            body = self.rfile.read(int(content_length))

        is_websocket = self.headers.get("Upgrade", "").lower() == "websocket"
        upstream = socket.create_connection(("127.0.0.1", CDP_INTERNAL_PORT), timeout=5)
        upstream.settimeout(30)
        try:
            lines = [f"{self.command} {self.path} {self.request_version}".encode("iso-8859-1")]
            for name, value in self.headers.items():
                lname = name.lower()
                if lname == "host":
                    lines.append(f"Host: 127.0.0.1:{CDP_INTERNAL_PORT}".encode("iso-8859-1"))
                elif lname == "origin":
                    lines.append(f"Origin: http://127.0.0.1:{CDP_INTERNAL_PORT}".encode("iso-8859-1"))
                elif lname == "accept-encoding" and not is_websocket:
                    lines.append(b"Accept-Encoding: identity")
                elif lname == "connection" and not is_websocket:
                    lines.append(b"Connection: close")
                else:
                    lines.append(f"{name}: {value}".encode("iso-8859-1"))
            header_names = {k.lower() for k in self.headers.keys()}
            if "host" not in header_names:
                lines.append(f"Host: 127.0.0.1:{CDP_INTERNAL_PORT}".encode("iso-8859-1"))
            if not is_websocket and "connection" not in header_names:
                lines.append(b"Connection: close")
            upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)

            if is_websocket:
                response_head = _read_until_header_end(upstream)
                status_line, _, _ = _split_http_response(response_head)
                self.connection.sendall(response_head)
                if _response_status_code(status_line) == 101:
                    self.connection.settimeout(None)
                    upstream.settimeout(None)
                    _relay_streams(self.connection, upstream)
                return

            raw = _read_until_header_end(upstream)
            status_line, resp_body, resp_headers = _split_http_response(raw)
            header_map = {name.lower(): value for name, value in resp_headers}
            content_length = header_map.get("content-length")
            if content_length is not None:
                remaining = int(content_length) - len(resp_body)
                while remaining > 0:
                    chunk = upstream.recv(min(65536, remaining))
                    if not chunk:
                        break
                    resp_body += chunk
                    remaining -= len(chunk)
            else:
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    resp_body += chunk
            resp_body = _rewrite_cdp_payload(resp_body, public_host)
            self.connection.sendall(_build_http_response(status_line, resp_headers, resp_body))
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()


def _start_cdp_forwarder() -> None:
    """Optionally expose Chrome's loopback-only CDP to peer containers.

    Newer Chrome/Chromium builds can ignore --remote-debugging-address=0.0.0.0
    and bind DevTools to 127.0.0.1 for safety. Keep this disabled by default;
    advanced Docker Compose users can opt in when they run app/browser on a
    private custom network and explicitly need service-name CDP access.
    """
    global _cdp_forward_proc
    if not CDP_FORWARD_ENABLED:
        return
    if CDP_INTERNAL_PORT == CDP_PORT:
        raise RuntimeError(
            "SGCC_BROWSER_CDP_FORWARD_ENABLED=true requires "
            "SGCC_BROWSER_CDP_INTERNAL_PORT to differ from SGCC_BROWSER_CDP_PORT"
        )
    if _cdp_forward_running():
        return

    bind_host = _cdp_forward_bind_host()
    server = ThreadingHTTPServer((bind_host, CDP_PORT), CDPProxyHandler)
    thread = threading.Thread(target=server.serve_forever, name="sgcc-cdp-proxy", daemon=True)
    thread.start()
    _cdp_forward_proc = (server, thread)
    print(f"CDP proxy listening on {bind_host}:{CDP_PORT}; forwarding to 127.0.0.1:{CDP_INTERNAL_PORT}", flush=True)


def _stop_cdp_forwarder() -> None:
    global _cdp_forward_proc
    proxy = _cdp_forward_proc
    if proxy is not None:
        server, thread = proxy
        print(f"stopping CDP proxy on {CDP_PORT}", flush=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    _cdp_forward_proc = None


def _start_chrome() -> dict:
    global _proc, _last_started_at, _last_error
    with _state_lock:
        if _ready():
            _start_cdp_forwarder()
            return _status_payload()
        if _proc is not None and _proc.poll() is None:
            # A previous Chrome process exists but CDP is not reachable. Do not
            # start a second Chrome on the same profile/port.
            _stop_cdp_forwarder()
            _proc.terminate()
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait(timeout=10)
            _proc = None
        if _proc is not None and _proc.poll() is not None:
            _proc = None
        _clear_profile_locks()
        env = os.environ.copy()
        env["DISPLAY"] = DISPLAY
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        args = _chrome_args()
        print("starting chrome: " + " ".join(args), flush=True)
        try:
            _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            _last_started_at = time.time()
            _last_error = ""
        except Exception as exc:
            _proc = None
            _last_error = str(exc)
            raise
        if not _wait_ready(START_TIMEOUT):
            code = _proc.poll() if _proc is not None else None
            _last_error = f"chrome CDP did not become ready, exit_code={code}"
            raise RuntimeError(_last_error)
        _start_cdp_forwarder()
        return _status_payload()


def _stop_chrome() -> dict:
    global _proc, _last_stopped_at
    with _state_lock:
        _stop_cdp_forwarder()
        proc = _proc
        if proc is not None and proc.poll() is None:
            print(f"stopping chrome pid={proc.pid}", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        _proc = None
        _last_stopped_at = time.time()
        return _status_payload()


def _status_payload() -> dict:
    proc = _proc
    running = proc is not None and proc.poll() is None
    return {
        "revision": REVISION,
        "running": running,
        "ready": _ready() if running else False,
        "pid": proc.pid if running else None,
        "exit_code": None if proc is None or running else proc.poll(),
        "chrome_bin": CHROME_BIN,
        "profile_dir": PROFILE_DIR,
        "display": DISPLAY,
        "start_url": START_URL,
        "browser_language": BROWSER_LANGUAGE,
        "window_size": WINDOW_SIZE,
        "cdp_url": f"http://{CDP_HOST}:{CDP_PORT}",
        "chrome_cdp_url": f"http://{_chrome_cdp_host()}:{_chrome_cdp_port()}",
        "cdp_forward_enabled": CDP_FORWARD_ENABLED,
        "cdp_forward_running": _cdp_forward_running(),
        "last_started_at": _last_started_at,
        "last_stopped_at": _last_stopped_at,
        "last_error": _last_error,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)

    def do_GET(self):
        if self.path in {"/health", "/status"}:
            _json_response(self, 200, _status_payload())
            return
        if self.path == "/json/version":
            try:
                with urlopen(f"http://{_cdp_probe_host()}:{_chrome_cdp_port()}/json/version", timeout=2) as resp:
                    body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                _json_response(self, 503, {"error": str(exc), **_status_payload()})
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/start":
                _json_response(self, 200, _start_chrome())
                return
            if self.path == "/stop":
                _json_response(self, 200, _stop_chrome())
                return
            _json_response(self, 404, {"error": "not found"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc), **_status_payload()})


def _handle_signal(signum, frame):
    try:
        _stop_chrome()
    finally:
        raise SystemExit(0)


def main() -> None:
    atexit.register(_stop_cdp_forwarder)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"sgcc browser service listening on {HOST}:{PORT}; chrome launches on demand", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
