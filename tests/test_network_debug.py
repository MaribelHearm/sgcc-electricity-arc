import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sgcc_ha_bridge.diag import DiagnosticCollector
from sgcc_ha_bridge.network_capture import NetworkRecorder


class FakeDriver:
    current_url = "https://95598.cn/osgweb/login"
    capabilities = {"goog:chromeOptions": {"debuggerAddress": "127.0.0.1:19222"}}


class LoginNetworkMetadataTestCase(unittest.TestCase):
    def test_login_metadata_records_actual_fingerprint_without_secrets_or_body(self):
        recorder = NetworkRecorder(
            FakeDriver(),
            allowed_hosts={"95598.cn"},
            unscoped_metadata_only=True,
        )
        sent = []
        recorder._send = lambda method, params=None: sent.append((method, params))

        recorder._handle_event("Network.requestWillBeSent", {
            "requestId": "login-request",
            "type": "Document",
            "initiator": {"type": "other"},
            "request": {
                "url": (
                    "https://95598.cn/osgweb/login"
                    "?token=secret-token&accountNo=1234567890016"
                ),
                "method": "POST",
                "hasPostData": True,
                "headers": {
                    "User-Agent": "Mozilla/5.0 Test Chrome/138.0.0.0",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "sec-ch-ua": '"Chromium";v="138", "Not_A Brand";v="99"',
                    "sec-ch-ua-platform": '"Linux"',
                    "sec-ch-ua-mobile": "?0",
                    "Cookie": "SESSION=secret-cookie",
                    "Authorization": "Bearer secret-authorization",
                },
            },
        })
        recorder._handle_event("Network.requestWillBeSentExtraInfo", {
            "requestId": "login-request",
            "headers": {
                "User-Agent": "Mozilla/5.0 Test Chrome/138.0.0.0",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "sec-ch-ua": '"Chromium";v="138", "Not_A Brand";v="99"',
                "sec-ch-ua-platform": '"Linux"',
                "sec-ch-ua-mobile": "?0",
                "Cookie": "SESSION=secret-cookie",
                "Authorization": "Bearer secret-authorization",
            },
            "clientSecurityState": {
                "initiatorIPAddressSpace": "Local",
                "privateNetworkRequestPolicy": "PreflightBlock",
            },
        })
        recorder._handle_event("Network.responseReceived", {
            "requestId": "login-request",
            "type": "Document",
            "response": {
                "url": (
                    "https://95598.cn/api/login"
                    "?token=secret-token&accountNo=1234567890016"
                ),
                "status": 200,
                "mimeType": "application/json",
                "protocol": "h2",
                "remoteIPAddress": "203.0.113.8",
                "remotePort": 443,
                "connectionReused": True,
                "connectionId": 42,
                "fromDiskCache": False,
                "fromServiceWorker": False,
                "securityState": "secure",
                "securityDetails": {
                    "protocol": "TLS 1.3",
                    "cipher": "AES_256_GCM",
                    "keyExchangeGroup": "X25519",
                    "subjectName": "must-not-be-recorded.example",
                },
                "timing": {
                    "dnsStart": 1,
                    "dnsEnd": 2,
                    "connectStart": 2,
                    "connectEnd": 4,
                    "sslStart": 2.5,
                    "sslEnd": 3.5,
                    "receiveHeadersEnd": 9,
                    "unlistedSecret": "must-not-be-recorded",
                },
            },
        })
        recorder._handle_event("Network.loadingFinished", {
            "requestId": "login-request",
            "encodedDataLength": 321,
        })

        observations = recorder.observations()
        self.assertEqual(sent, [])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].source, "network_metadata")
        self.assertEqual(observations[0].scope_id, "login")
        request = observations[0].payload["request"]
        response = observations[0].payload["response"]
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["has_post_data"])
        self.assertEqual(
            request["fingerprint_headers"],
            {
                "user-agent": "Mozilla/5.0 Test Chrome/138.0.0.0",
                "accept-language": "zh-CN,zh;q=0.9",
                "sec-ch-ua": '"Chromium";v="138", "Not_A Brand";v="99"',
                "sec-ch-ua-platform": '"Linux"',
                "sec-ch-ua-mobile": "?0",
            },
        )
        self.assertEqual(
            request["header_sources"],
            ["requestWillBeSent", "requestWillBeSentExtraInfo"],
        )
        self.assertNotIn("cookie", request["headers"])
        self.assertNotIn("authorization", request["headers"])
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["protocol"], "h2")
        self.assertEqual(response["remote_ip_address"], "203.0.113.8")
        self.assertEqual(response["security_details"]["protocol"], "TLS 1.3")
        self.assertEqual(response["security_details"]["cipher"], "AES_256_GCM")
        self.assertNotIn("subjectName", response["security_details"])
        self.assertNotIn("unlistedSecret", response["timing"])
        self.assertNotIn("body", observations[0].payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DiagnosticCollector(output_dir=temp_dir)
            collector.record_browser_runtime("login_page", {
                "page": {
                    "userAgent": "Mozilla/5.0 Test Chrome/138.0.0.0",
                    "language": "zh-CN",
                    "languages": ["zh-CN", "zh"],
                    "platform": "Linux x86_64",
                    "maxTouchPoints": 0,
                },
                "webdriver": {"browser_version": "138.0.0.0"},
            })
            collector.record_observations(observations)
            collector.emit("failed")

            latest = Path(temp_dir) / "latest"
            login_network_text = (latest / "login-network.json").read_text(
                encoding="utf-8"
            )
            login_network = json.loads(login_network_text)
            with zipfile.ZipFile(latest / "sgcc-debug-bundle.zip") as bundle:
                bundle_names = set(bundle.namelist())

        self.assertIn("login-network.json", bundle_names)
        self.assertEqual(len(login_network["requests"]), 1)
        self.assertEqual(len(login_network["comparisons"]), 1)
        self.assertEqual(
            login_network["comparisons"][0]["missing_headers"],
            [],
        )
        self.assertTrue(all(
            value is True
            for value in login_network["comparisons"][0]["checks"].values()
        ), login_network["comparisons"][0]["checks"])
        self.assertNotIn("secret-token", login_network_text)
        self.assertNotIn("secret-cookie", login_network_text)
        self.assertNotIn("secret-authorization", login_network_text)
        self.assertNotIn("must-not-be-recorded", login_network_text)
        self.assertNotIn("1234567890016", login_network_text)


if __name__ == "__main__":
    unittest.main()
