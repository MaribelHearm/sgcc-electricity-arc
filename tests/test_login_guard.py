import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sgcc_ha_bridge.login_guard import (
    classify_login_failure,
    clear_login_cooldown,
    get_login_cooldown,
    set_login_cooldown,
    should_retry_login_failure,
)


class LoginGuardTestCase(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("SGCC_LOGIN_COOLDOWN_FILE", None)

    def test_classify_risk_keywords_are_non_retryable(self):
        category = classify_login_failure("RK001 操作过于频繁，请稍后再试")
        self.assertEqual(category, "risk_blocked")
        self.assertFalse(should_retry_login_failure(category))

    def test_captcha_passed_but_still_login_page_is_non_retryable(self):
        category = classify_login_failure("验证码已通过但仍停留在登录页面", captcha_passed=True)
        self.assertEqual(category, "captcha_passed_login_failed")
        self.assertFalse(should_retry_login_failure(category))

    def test_phone_code_timeout_is_non_retryable(self):
        self.assertFalse(should_retry_login_failure("phone_code_timeout"))

    def test_phone_code_request_failure_is_non_retryable(self):
        self.assertFalse(should_retry_login_failure("phone_code_request_failed"))

    def test_login_ui_failure_is_non_retryable(self):
        self.assertFalse(should_retry_login_failure("login_ui_failed"))

    def test_generic_login_failure_can_retry(self):
        category = classify_login_failure("temporary network error")
        self.assertEqual(category, "login_failed")
        self.assertTrue(should_retry_login_failure(category))

    def test_cooldown_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cooldown.json"
            os.environ["SGCC_LOGIN_COOLDOWN_FILE"] = str(path)
            state = set_login_cooldown("risk_blocked: RK001", minutes=5)
            self.assertTrue(state.active)
            loaded = get_login_cooldown()
            self.assertTrue(loaded.active)
            self.assertIn("RK001", loaded.reason)
            self.assertGreater(loaded.remaining_seconds, 0)

    def test_clear_cooldown_removes_persisted_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cooldown.json"
            os.environ["SGCC_LOGIN_COOLDOWN_FILE"] = str(path)
            set_login_cooldown("risk_blocked: RK001", minutes=5)

            self.assertTrue(path.exists())
            self.assertTrue(clear_login_cooldown())
            self.assertFalse(path.exists())
            self.assertFalse(get_login_cooldown().active)
            self.assertFalse(clear_login_cooldown())

    def test_expired_cooldown_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cooldown.json"
            os.environ["SGCC_LOGIN_COOLDOWN_FILE"] = str(path)
            path.write_text(
                '{"until": "'
                + (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                + '", "reason": "old"}',
                encoding="utf-8",
            )
            self.assertFalse(get_login_cooldown().active)


if __name__ == "__main__":
    unittest.main()
