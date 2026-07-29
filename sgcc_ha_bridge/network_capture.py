"""Passive Chrome DevTools Network recorder for SGCC XHR/fetch payloads."""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from websocket import WebSocketApp

from .observation import CaptureScope, Observation
from .redact import redact_text, redact_url


DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
_SAFE_REQUEST_HEADERS = {
    ":authority",
    ":method",
    ":scheme",
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "content-type",
    "dnt",
    "origin",
    "pragma",
    "priority",
    "referer",
    "sec-ch-ua",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-mobile",
    "sec-ch-ua-model",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "user-agent",
    "x-requested-with",
}
_FINGERPRINT_REQUEST_HEADERS = (
    "user-agent",
    "accept-language",
    "sec-ch-ua",
    "sec-ch-ua-platform",
    "sec-ch-ua-mobile",
)
_BODY_RESOURCE_TYPES = {"XHR", "Fetch"}
_LOGIN_METADATA_RESOURCE_TYPES = {"Document", "XHR", "Fetch"}
_SAFE_RESPONSE_TIMING_FIELDS = {
    "requestTime",
    "proxyStart",
    "proxyEnd",
    "dnsStart",
    "dnsEnd",
    "connectStart",
    "connectEnd",
    "sslStart",
    "sslEnd",
    "workerStart",
    "workerReady",
    "workerFetchStart",
    "workerRespondWithSettled",
    "sendStart",
    "sendEnd",
    "pushStart",
    "pushEnd",
    "receiveHeadersStart",
    "receiveHeadersEnd",
}
_SAFE_SECURITY_DETAIL_FIELDS = {
    "protocol",
    "keyExchange",
    "keyExchangeGroup",
    "cipher",
    "mac",
    "serverSignatureAlgorithm",
    "certificateTransparencyCompliance",
    "encryptedClientHello",
}


def _safe_request_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    safe: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name not in _SAFE_REQUEST_HEADERS:
            continue
        value = str(raw_value)
        if name in {"origin", "referer"}:
            value = redact_url(value)
        safe[name] = value[:1024]
    return safe


def _safe_mapping(value: Any, allowed_fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in allowed_fields
        if key in value
    }


def _fingerprint_request_headers(headers: Any) -> dict[str, str]:
    safe_headers = _safe_request_headers(headers)
    return {
        name: safe_headers[name]
        for name in _FINGERPRINT_REQUEST_HEADERS
        if name in safe_headers
    }


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def cdp_address_for_driver(driver) -> str:
    capabilities = getattr(driver, "capabilities", {}) or {}
    chrome_options = capabilities.get("goog:chromeOptions") or {}
    debugger_address = str(chrome_options.get("debuggerAddress") or "").strip()
    if debugger_address:
        return debugger_address
    configured = os.getenv("SGCC_CDP_ADDRESS", "").strip()
    if configured:
        return configured.removeprefix("http://").removeprefix("https://").rstrip("/")
    host = os.getenv("SGCC_CDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("SGCC_CDP_PORT", "9222").strip() or "9222"
    return f"{host}:{port}"


class NetworkRecorder:
    """Record response bodies without changing Selenium navigation behavior."""

    def __init__(
        self,
        driver,
        *,
        max_body_bytes: Optional[int] = None,
        allowed_hosts: Optional[set[str]] = None,
        unscoped_metadata_only: bool = False,
    ):
        self.driver = driver
        self.cdp_address = cdp_address_for_driver(driver)
        self.max_body_bytes = max(1, max_body_bytes) if max_body_bytes is not None else _positive_int_env(
            "SGCC_DEBUG_MAX_RESPONSE_BYTES",
            DEFAULT_MAX_BODY_BYTES,
        )
        page_host = urlparse(str(getattr(driver, "current_url", "") or "")).hostname or ""
        configured_hosts = {
            item.strip().lower()
            for item in os.getenv("SGCC_DEBUG_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        self.allowed_hosts = allowed_hosts or configured_hosts or {page_host, "95598.cn"}
        self.unscoped_metadata_only = bool(unscoped_metadata_only)
        self._lock = threading.RLock()
        self._opened = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[WebSocketApp] = None
        self._next_id = 0
        self._body_commands: dict[int, dict[str, Any]] = {}
        self._request_scopes: dict[str, Optional[CaptureScope]] = {}
        self._request_metadata: dict[str, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, Any]] = {}
        self._observations: list[Observation] = []
        self._current_scope: Optional[CaptureScope] = None
        self._errors: list[str] = []
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def start(self, timeout: float = 8.0) -> bool:
        try:
            websocket_url = self._find_page_websocket()
            self._ws = WebSocketApp(
                websocket_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._thread = threading.Thread(
                target=lambda: self._ws.run_forever(suppress_origin=True),
                name="sgcc-network-recorder",
                daemon=True,
            )
            self._thread.start()
            if not self._opened.wait(timeout):
                raise RuntimeError("CDP Network recorder open timeout")
            self._started = True
            logging.info(f"SGCC Network recorder 已连接 CDP: {self.cdp_address}")
            return True
        except Exception as exc:
            self._record_error(exc)
            logging.warning(f"SGCC Network recorder 启动失败，回退 Vue 路径: {redact_text(exc)}")
            self.stop()
            return False

    def stop(self) -> None:
        self._started = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def flush(self, timeout: float = 1.5, scope_id: Optional[str] = None) -> None:
        """Wait briefly for requests and response bodies assigned to a scope.

        The wait is bounded because SGCC pages may keep long-polling requests
        open. Scope assignment remains the request-start scope.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                pending_scopes = [
                    scope
                    for scope in self._request_scopes.values()
                    if scope_id is None or (scope is not None and scope.id == scope_id)
                ]
                response_scopes = [
                    item.get("scope")
                    for item in self._responses.values()
                    if scope_id is None
                    or (
                        item.get("scope") is not None
                        and item["scope"].id == scope_id
                    )
                ]
                body_scopes = [
                    item.get("scope")
                    for item in self._body_commands.values()
                    if scope_id is None
                    or (
                        item.get("scope") is not None
                        and item["scope"].id == scope_id
                    )
                ]
                pending = len(pending_scopes) + len(response_scopes) + len(body_scopes)
            if pending == 0:
                return
            time.sleep(0.05)

    def set_scope(self, scope: Optional[CaptureScope]) -> None:
        with self._lock:
            self._current_scope = scope

    def observations(self, scope_id: Optional[str] = None) -> list[Observation]:
        with self._lock:
            values = list(self._observations)
        if scope_id:
            values = [item for item in values if item.scope_id == scope_id]
        return values

    def _find_page_websocket(self) -> str:
        response = requests.get(f"http://{self.cdp_address}/json/list", timeout=5)
        response.raise_for_status()
        targets = response.json()
        page_targets = [target for target in targets if target.get("type") == "page"]
        preferred = next(
            (
                target for target in page_targets
                if "95598.cn" in str(target.get("url") or "")
            ),
            None,
        )
        target = preferred or (page_targets[0] if page_targets else None)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise RuntimeError("CDP page target not found")
        return str(target["webSocketDebuggerUrl"])

    def _on_open(self, ws) -> None:
        self._send("Network.enable", {
            "maxTotalBufferSize": self.max_body_bytes * 4,
            "maxResourceBufferSize": self.max_body_bytes,
            "maxPostDataSize": 0,
        })
        self._opened.set()

    def _on_close(self, ws, status_code, message) -> None:
        self._started = False

    def _on_error(self, ws, error) -> None:
        self._record_error(error)

    def _on_message(self, ws, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
            if "method" in message:
                self._handle_event(str(message["method"]), message.get("params") or {})
            elif "id" in message:
                self._handle_command_result(int(message["id"]), message)
        except Exception as exc:
            self._record_error(exc)

    def _handle_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Network.requestWillBeSent":
            request_id = str(params.get("requestId") or "")
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            if request_id and self._allowed_url(url):
                with self._lock:
                    self._request_scopes[request_id] = self._current_scope
                    previous = self._request_metadata.get(request_id) or {}
                    previous_headers = previous.get("headers") or {}
                    header_sources = set(previous.get("header_sources") or [])
                    header_sources.add("requestWillBeSent")
                    previous.update({
                        "method": request.get("method"),
                        "resource_type": params.get("type"),
                        "initiator_type": (params.get("initiator") or {}).get("type"),
                        "has_post_data": bool(request.get("hasPostData")),
                        "headers": {
                            **previous_headers,
                            **_safe_request_headers(request.get("headers")),
                        },
                        "header_sources": sorted(header_sources),
                    })
                    self._request_metadata[request_id] = previous
            elif request_id:
                with self._lock:
                    self._request_scopes.pop(request_id, None)
                    self._request_metadata.pop(request_id, None)
            return

        if method == "Network.requestWillBeSentExtraInfo":
            request_id = str(params.get("requestId") or "")
            if request_id:
                with self._lock:
                    previous = self._request_metadata.get(request_id) or {}
                    header_sources = set(previous.get("header_sources") or [])
                    header_sources.add("requestWillBeSentExtraInfo")
                    previous["headers"] = {
                        **(previous.get("headers") or {}),
                        **_safe_request_headers(params.get("headers")),
                    }
                    previous["header_sources"] = sorted(header_sources)
                    previous["client_security_state"] = _safe_mapping(
                        params.get("clientSecurityState"),
                        {
                            "initiatorIPAddressSpace",
                            "privateNetworkRequestPolicy",
                            "ipAddressSpace",
                        },
                    )
                    self._request_metadata[request_id] = previous
            return

        if method == "Network.responseReceived":
            resource_type = str(params.get("type") or "")
            allowed_resource_types = (
                _LOGIN_METADATA_RESOURCE_TYPES
                if self.unscoped_metadata_only
                else _BODY_RESOURCE_TYPES
            )
            if resource_type not in allowed_resource_types:
                return
            response = params.get("response") or {}
            url = str(response.get("url") or "")
            if not self._allowed_url(url):
                request_id = str(params.get("requestId") or "")
                with self._lock:
                    self._request_scopes.pop(request_id, None)
                    self._request_metadata.pop(request_id, None)
                    self._responses.pop(request_id, None)
                return
            request_id = str(params.get("requestId") or "")
            if not request_id:
                return
            with self._lock:
                scope = self._request_scopes.get(request_id, self._current_scope)
                request_metadata = self._request_metadata.get(request_id) or {}
                self._responses[request_id] = {
                    "url": url,
                    "status": response.get("status"),
                    "mime_type": response.get("mimeType") or "",
                    "resource_type": resource_type,
                    "request": request_metadata,
                    "protocol": response.get("protocol"),
                    "remote_ip_address": response.get("remoteIPAddress"),
                    "remote_port": response.get("remotePort"),
                    "connection_reused": response.get("connectionReused"),
                    "connection_id": response.get("connectionId"),
                    "from_disk_cache": response.get("fromDiskCache"),
                    "from_service_worker": response.get("fromServiceWorker"),
                    "from_prefetch_cache": response.get("fromPrefetchCache"),
                    "security_state": response.get("securityState"),
                    "security_details": _safe_mapping(
                        response.get("securityDetails"),
                        _SAFE_SECURITY_DETAIL_FIELDS,
                    ),
                    "timing": _safe_mapping(
                        response.get("timing"),
                        _SAFE_RESPONSE_TIMING_FIELDS,
                    ),
                    "scope": scope,
                }
            return

        if method == "Network.loadingFinished":
            request_id = str(params.get("requestId") or "")
            encoded_length = int(params.get("encodedDataLength") or 0)
            with self._lock:
                self._request_scopes.pop(request_id, None)
                self._request_metadata.pop(request_id, None)
                metadata = self._responses.pop(request_id, None)
            if not metadata:
                return
            if self.unscoped_metadata_only and metadata.get("scope") is None:
                request_metadata = metadata.get("request") or {}
                request_headers = request_metadata.get("headers") or {}
                observation = Observation(
                    source="network_metadata",
                    scope_id="login",
                    scope_label="登录阶段",
                    payload={
                        "request": {
                            **request_metadata,
                            "fingerprint_headers": _fingerprint_request_headers(
                                request_headers
                            ),
                        },
                        "response": {
                            key: metadata.get(key)
                            for key in (
                                "status",
                                "mime_type",
                                "resource_type",
                                "protocol",
                                "remote_ip_address",
                                "remote_port",
                                "connection_reused",
                                "connection_id",
                                "from_disk_cache",
                                "from_service_worker",
                                "from_prefetch_cache",
                                "security_state",
                                "security_details",
                                "timing",
                            )
                        },
                        "encoded_data_length": encoded_length,
                    },
                    metadata={"url": metadata.get("url")},
                )
                with self._lock:
                    self._observations.append(observation)
                return
            if metadata.get("resource_type") not in _BODY_RESOURCE_TYPES:
                return
            if encoded_length > self.max_body_bytes:
                self._record_error(
                    f"response body skipped: {metadata.get('url')} bytes={encoded_length}"
                )
                return
            command_id = self._send("Network.getResponseBody", {"requestId": request_id})
            with self._lock:
                self._body_commands[command_id] = {"request_id": request_id, **metadata}
            return

        if method == "Network.loadingFailed":
            request_id = str(params.get("requestId") or "")
            with self._lock:
                self._request_scopes.pop(request_id, None)
                self._request_metadata.pop(request_id, None)
                self._responses.pop(request_id, None)

    def _handle_command_result(self, command_id: int, message: dict[str, Any]) -> None:
        with self._lock:
            metadata = self._body_commands.pop(command_id, None)
        if not metadata:
            return
        if message.get("error"):
            self._record_error(message["error"])
            return
        result = message.get("result") or {}
        body = result.get("body")
        if body is None:
            return
        if result.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            except Exception as exc:
                self._record_error(exc)
                return
        payload = parse_json_body(str(body))
        if payload is None:
            return
        scope = metadata.get("scope")
        observation = Observation(
            source="network",
            scope_id=scope.id if scope else "unscoped",
            scope_label=scope.label if scope else "unscoped",
            account_no=scope.account_no if scope else "",
            payload=payload,
            metadata={
                "url": metadata.get("url"),
                "status": metadata.get("status"),
                "mime_type": metadata.get("mime_type"),
                "resource_type": metadata.get("resource_type"),
                "request": metadata.get("request") or {},
                "protocol": metadata.get("protocol"),
                "remote_ip_address": metadata.get("remote_ip_address"),
                "remote_port": metadata.get("remote_port"),
                "security_state": metadata.get("security_state"),
            },
        )
        with self._lock:
            self._observations.append(observation)

    def _send(self, method: str, params: Optional[dict[str, Any]] = None) -> int:
        with self._lock:
            self._next_id += 1
            command_id = self._next_id
            ws = self._ws
        if ws is None:
            raise RuntimeError("CDP websocket is not connected")
        ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        return command_id

    def _allowed_url(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return bool(hostname) and any(
            hostname == allowed or hostname.endswith(f".{allowed}")
            for allowed in self.allowed_hosts
            if allowed
        )

    def _record_error(self, error: Any) -> None:
        text = redact_text(error)
        with self._lock:
            if len(self._errors) < 80:
                self._errors.append(text)


def parse_json_body(body: str) -> Any | None:
    text = body.strip()
    if not text:
        return None
    prefixes = (")]}'", "while(1);", "for(;;);")
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("\r\n ;")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
