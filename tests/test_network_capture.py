import json
import threading
import time
from unittest.mock import patch

from sgcc_ha_bridge.network_capture import NetworkRecorder, parse_json_body
from sgcc_ha_bridge.observation import CaptureScope


class FakeDriver:
    current_url = "https://95598.cn/osgweb/userAcc"
    capabilities = {"goog:chromeOptions": {"debuggerAddress": "127.0.0.1:19222"}}


def test_parse_json_body_supports_anti_xssi_prefix():
    assert parse_json_body(")]}'\n{\"data\":{\"value\":1}}") == {"data": {"value": 1}}
    assert parse_json_body("<html>no</html>") is None


def test_invalid_response_size_env_falls_back_safely():
    with patch.dict("os.environ", {"SGCC_DEBUG_MAX_RESPONSE_BYTES": "invalid"}):
        recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})

    assert recorder.max_body_bytes == 2 * 1024 * 1024


def test_network_events_become_scoped_observation():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    sent = []

    def fake_send(method, params=None):
        sent.append((method, params))
        return 7

    recorder._send = fake_send
    scope = CaptureScope.create("账户余额", "1234567890123")
    recorder.set_scope(scope)
    recorder._handle_event("Network.responseReceived", {
        "requestId": "request-1",
        "type": "XHR",
        "response": {
            "url": "https://95598.cn/api/balance",
            "status": 200,
            "mimeType": "application/json",
        },
    })
    recorder._handle_event("Network.loadingFinished", {
        "requestId": "request-1",
        "encodedDataLength": 100,
    })
    recorder._handle_command_result(7, {
        "id": 7,
        "result": {
            "body": json.dumps({"data": {"accountBalance": "23.46"}}),
            "base64Encoded": False,
        },
    })
    observations = recorder.observations(scope.id)
    assert sent == [("Network.getResponseBody", {"requestId": "request-1"})]
    assert len(observations) == 1
    assert observations[0].payload["data"]["accountBalance"] == "23.46"


def test_network_scope_is_bound_when_request_starts():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    first_scope = CaptureScope.create("账户余额", "1234567890123")
    second_scope = CaptureScope.create("月度电费", "1234567890999")
    recorder.set_scope(first_scope)
    recorder._handle_event("Network.requestWillBeSent", {
        "requestId": "request-1",
        "type": "XHR",
        "request": {"url": "https://95598.cn/api/balance"},
    })
    recorder.set_scope(second_scope)
    recorder._handle_event("Network.responseReceived", {
        "requestId": "request-1",
        "type": "XHR",
        "response": {
            "url": "https://95598.cn/api/balance",
            "status": 200,
            "mimeType": "application/json",
        },
    })

    assert recorder._responses["request-1"]["scope"] == first_scope


def test_network_recorder_rejects_unrelated_hosts():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    recorder._handle_event("Network.responseReceived", {
        "requestId": "request-1",
        "type": "XHR",
        "response": {
            "url": "https://example.invalid/api",
            "status": 200,
            "mimeType": "application/json",
        },
    })
    assert recorder._responses == {}


def test_login_recorder_never_reads_document_response_body():
    recorder = NetworkRecorder(
        FakeDriver(),
        allowed_hosts={"95598.cn"},
        unscoped_metadata_only=True,
    )
    sent = []
    recorder._send = lambda method, params=None: sent.append((method, params))
    recorder.set_scope(CaptureScope.create("登录后页面", "1234567890123"))
    recorder._handle_event("Network.responseReceived", {
        "requestId": "document-1",
        "type": "Document",
        "response": {
            "url": "https://95598.cn/osgweb/userAcc",
            "status": 200,
            "mimeType": "text/html",
        },
    })
    recorder._handle_event("Network.loadingFinished", {
        "requestId": "document-1",
        "encodedDataLength": 100,
    })

    assert sent == []
    assert recorder.observations() == []


def test_network_loading_failed_discards_pending_metadata():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    recorder._handle_event("Network.responseReceived", {
        "requestId": "request-1",
        "type": "Fetch",
        "response": {
            "url": "https://95598.cn/api/balance",
            "status": 200,
            "mimeType": "application/json",
        },
    })

    recorder._handle_event("Network.loadingFailed", {"requestId": "request-1"})

    assert recorder._responses == {}


def test_flush_waits_for_pending_body_command_to_finish():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    recorder._body_commands[1] = {"request_id": "request-1"}

    def complete():
        time.sleep(0.02)
        recorder._body_commands.pop(1, None)

    thread = threading.Thread(target=complete)
    thread.start()
    recorder.flush(timeout=0.5)
    thread.join()

    assert recorder._body_commands == {}


def test_flush_can_wait_for_only_one_scope():
    recorder = NetworkRecorder(FakeDriver(), allowed_hosts={"95598.cn"})
    first = CaptureScope.create("账户余额", "1234567890123")
    second = CaptureScope.create("月度电费", "1234567890999")
    recorder._body_commands[1] = {"request_id": "request-1", "scope": first}
    recorder._body_commands[2] = {"request_id": "request-2", "scope": second}

    def complete_first():
        time.sleep(0.02)
        recorder._body_commands.pop(1, None)

    thread = threading.Thread(target=complete_first)
    thread.start()
    recorder.flush(timeout=0.5, scope_id=first.id)
    thread.join()

    assert 1 not in recorder._body_commands
    assert 2 in recorder._body_commands
