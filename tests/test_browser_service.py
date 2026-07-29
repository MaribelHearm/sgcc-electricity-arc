import importlib
import os
import socket
import sys
import unittest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BrowserServiceConfigTestCase(unittest.TestCase):
    def _load_browser_service(self, **env):
        old_env = os.environ.copy()
        os.environ.update(env)
        sys.modules.pop("sgcc_ha_bridge.browser_service", None)
        try:
            return importlib.import_module("sgcc_ha_bridge.browser_service")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_cdp_forward_disabled_keeps_existing_port_and_host(self):
        browser_service = self._load_browser_service(
            SGCC_BROWSER_CDP_HOST="0.0.0.0",
            SGCC_BROWSER_CDP_PORT="19222",
            SGCC_BROWSER_CDP_FORWARD_ENABLED="false",
        )

        self.assertFalse(browser_service.CDP_FORWARD_ENABLED)
        self.assertEqual(browser_service._chrome_cdp_host(), "0.0.0.0")
        self.assertEqual(browser_service._chrome_cdp_port(), 19222)
        self.assertEqual(browser_service._cdp_probe_host(), "127.0.0.1")

    def test_standalone_defaults_bind_management_and_cdp_to_loopback(self):
        browser_service = self._load_browser_service()

        self.assertEqual(browser_service.HOST, "127.0.0.1")
        self.assertEqual(browser_service.CDP_HOST, "127.0.0.1")
        self.assertEqual(browser_service.START_URL, "about:blank")

    def test_status_payload_exposes_image_revision(self):
        browser_service = self._load_browser_service(VERSION="commit-abc123")

        self.assertEqual(
            browser_service._status_payload()["revision"],
            "commit-abc123",
        )

    def test_chrome_starts_blank_with_consistent_automation_flags(self):
        browser_service = self._load_browser_service(
            BROWSER_LANGUAGE="zh-CN,zh,en-US,en",
            BROWSER_WINDOW_SIZE="1920,1080",
        )

        args = browser_service._chrome_args()

        self.assertEqual(args[-1], "about:blank")
        self.assertIn("--disable-blink-features=AutomationControlled", args)
        self.assertIn("--lang=zh-CN", args)
        self.assertIn("--accept-lang=zh-CN,zh,en-US,en", args)
        self.assertIn("--window-size=1920,1080", args)
        self.assertNotIn("https://95598.cn/osgweb/login", args)

    def test_cdp_forward_enabled_uses_internal_loopback_port(self):
        browser_service = self._load_browser_service(
            SGCC_BROWSER_CDP_HOST="0.0.0.0",
            SGCC_BROWSER_CDP_PORT="19222",
            SGCC_BROWSER_CDP_INTERNAL_PORT="19223",
            SGCC_BROWSER_CDP_FORWARD_ENABLED="true",
        )

        self.assertTrue(browser_service.CDP_FORWARD_ENABLED)
        self.assertEqual(browser_service._chrome_cdp_host(), "127.0.0.1")
        self.assertEqual(browser_service._chrome_cdp_port(), 19223)
        self.assertEqual(browser_service._cdp_forward_bind_host(), "0.0.0.0")

    def test_cdp_payload_rewrites_websocket_debugger_url_to_public_host(self):
        browser_service = self._load_browser_service(
            SGCC_BROWSER_CDP_PORT="19222",
            SGCC_BROWSER_CDP_INTERNAL_PORT="19223",
            SGCC_BROWSER_CDP_FORWARD_ENABLED="true",
        )

        body = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:19223/devtools/browser/abc"}'
        rewritten = browser_service._rewrite_cdp_payload(body, "sgcc_browser:19222")

        self.assertIn(b"ws://sgcc_browser:19222/devtools/browser/abc", rewritten)
        self.assertNotIn(b"127.0.0.1:19223", rewritten)

    def test_response_status_code_parses_websocket_upgrade(self):
        browser_service = self._load_browser_service()

        self.assertEqual(browser_service._response_status_code(b"HTTP/1.1 101 Switching Protocols"), 101)
        self.assertEqual(browser_service._response_status_code(b"HTTP/1.1 404 Not Found"), 404)
        self.assertIsNone(browser_service._response_status_code(b"HTTP/1.1"))
        self.assertIsNone(browser_service._response_status_code(b"not-http"))

    def test_cdp_forward_rejects_same_public_and_internal_port(self):
        browser_service = self._load_browser_service(
            SGCC_BROWSER_CDP_PORT="19222",
            SGCC_BROWSER_CDP_INTERNAL_PORT="19222",
            SGCC_BROWSER_CDP_FORWARD_ENABLED="true",
        )

        with self.assertRaisesRegex(RuntimeError, "requires"):
            browser_service._start_cdp_forwarder()

    def test_cdp_forward_starts_threaded_proxy_when_enabled(self):
        public_port = _free_port()
        internal_port = _free_port()
        browser_service = self._load_browser_service(
            SGCC_BROWSER_CDP_HOST="127.0.0.1",
            SGCC_BROWSER_CDP_PORT=str(public_port),
            SGCC_BROWSER_CDP_INTERNAL_PORT=str(internal_port),
            SGCC_BROWSER_CDP_FORWARD_ENABLED="true",
        )

        try:
            browser_service._start_cdp_forwarder()
            self.assertTrue(browser_service._cdp_forward_running())
            server, thread = browser_service._cdp_forward_proc
            self.assertEqual(server.server_address[1], public_port)
            self.assertTrue(thread.is_alive())
        finally:
            browser_service._stop_cdp_forwarder()


if __name__ == "__main__":
    unittest.main()
