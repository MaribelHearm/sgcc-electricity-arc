import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from sgcc_ha_bridge.diag import (
    DiagnosticCollector,
    SUMMARY_END,
    SUMMARY_START,
    debug_enabled,
    diag_enabled,
    diag_output_root,
    redact_structure,
)
from sgcc_ha_bridge.model import (
    Account,
    AccountData,
    Balance,
    DailyReading,
    MonthlyReading,
    SessionCheck,
    YearlyReading,
)
from sgcc_ha_bridge.observation import CaptureScope, Observation, ParserDecision


class DiagSwitchTestCase(unittest.TestCase):
    def test_sgcc_debug_is_canonical_and_old_switches_are_aliases(self):
        with patch.dict(
            os.environ,
            {"SGCC_DEBUG": "true", "SGCC_DIAG": "false", "DEBUG_MODE": "false"},
            clear=False,
        ):
            self.assertTrue(debug_enabled())
        with patch.dict(
            os.environ,
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "true", "DEBUG_MODE": "false"},
            clear=False,
        ):
            self.assertTrue(diag_enabled())
        with patch.dict(
            os.environ,
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "false", "DEBUG_MODE": "true"},
            clear=False,
        ):
            self.assertTrue(debug_enabled())
        with patch.dict(
            os.environ,
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "false", "DEBUG_MODE": "false"},
            clear=False,
        ):
            self.assertFalse(diag_enabled())

    def test_runtime_reports_effective_debug_switch(self):
        switch_cases = (
            {"SGCC_DEBUG": "true", "SGCC_DIAG": "false", "DEBUG_MODE": "false"},
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "true", "DEBUG_MODE": "false"},
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "false", "DEBUG_MODE": "true"},
        )
        for env in switch_cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=False):
                collector = DiagnosticCollector()
                collector.record_runtime(stage="test")
                self.assertEqual(collector.runtime["debug_mode"], "true")

        with patch.dict(
            os.environ,
            {"SGCC_DEBUG": "false", "SGCC_DIAG": "false", "DEBUG_MODE": "false"},
            clear=False,
        ):
            collector = DiagnosticCollector()
            collector.record_runtime(stage="test")
            self.assertEqual(collector.runtime["debug_mode"], "false")

    def test_force_debug_overrides_disabled_user_switches(self):
        with patch.dict(
            os.environ,
            {
                "SGCC_FORCE_DEBUG": "true",
                "SGCC_DEBUG": "false",
                "SGCC_DIAG": "false",
                "DEBUG_MODE": "false",
            },
            clear=False,
        ):
            collector = DiagnosticCollector()
            collector.record_runtime(stage="test")
            self.assertTrue(debug_enabled())
            self.assertEqual(collector.runtime["debug_mode"], "true")
            self.assertEqual(collector.runtime["env"]["SGCC_FORCE_DEBUG"], "true")
            self.assertEqual(diag_output_root(), Path("/data/debug"))

    def test_runtime_includes_daily_jitter_in_safe_environment(self):
        with patch.dict(
            os.environ,
            {"SGCC_DAILY_JITTER_MINUTES": "45"},
            clear=False,
        ):
            collector = DiagnosticCollector()
            collector.record_runtime(stage="test")
            self.assertEqual(collector.runtime["env"]["SGCC_DAILY_JITTER_MINUTES"], "45")

    def test_runtime_includes_login_fallback_switches_without_telegram_credentials(self):
        with patch.dict(
            os.environ,
            {
                "SGCC_RISK_FALLBACK_OVERRIDE": "true",
                "SGCC_LOGIN_FALLBACK_UNATTENDED": "false",
                "SGCC_TELEGRAM_BOT_TOKEN": "sensitive-token",
                "SGCC_TELEGRAM_CHAT_ID": "123456789",
            },
            clear=False,
        ):
            collector = DiagnosticCollector()
            collector.record_runtime(stage="test")

        self.assertEqual(collector.runtime["env"]["SGCC_RISK_FALLBACK_OVERRIDE"], "true")
        self.assertEqual(collector.runtime["env"]["SGCC_LOGIN_FALLBACK_UNATTENDED"], "false")
        self.assertNotIn("SGCC_TELEGRAM_BOT_TOKEN", collector.runtime["env"])
        self.assertNotIn("SGCC_TELEGRAM_CHAT_ID", collector.runtime["env"])

    def test_legacy_diag_switch_keeps_legacy_output_directory(self):
        with patch.dict(
            os.environ,
            {
                "SGCC_DEBUG": "false",
                "DEBUG_MODE": "false",
                "SGCC_DIAG": "true",
                "SGCC_DEBUG_DIR": "/data/debug",
                "SGCC_DIAG_DIR": "/data/diag-custom",
            },
            clear=False,
        ):
            self.assertEqual(diag_output_root(), Path("/data/diag-custom"))


class DiagnosticCollectorTestCase(unittest.TestCase):
    def test_emit_writes_redacted_summary_and_field_package(self):
        account_data = AccountData(
            account=Account(
                account_no="1234567890016",
                display_name="张三",
                address="福建省福州市测试路 1 号",
                province="福建",
            ),
            balance=Balance(
                account_no="1234567890016",
                observed_at="2026-07-07 00:00:00",
                balance_cny=86.44,
                prepay_balance_cny=217.7,
                arrears_cny=0.0,
            ),
            yearly=YearlyReading(
                account_no="1234567890016",
                year="2026",
                total_usage_kwh=1234.5,
                total_charge_cny=678.9,
            ),
            monthly=[
                MonthlyReading(
                    account_no="1234567890016",
                    year_month="2026-06",
                    total_usage_kwh=321.0,
                    total_charge_cny=212.33,
                )
            ],
            daily=[
                DailyReading(
                    account_no="1234567890016",
                    date="2026-07-06",
                    total_usage_kwh=8.5,
                )
            ],
        )
        snapshot = {
            "url": "https://95598.cn/osgweb/userAcc?accountNo=1234567890016&token=secret-token",
            "store": {
                "state": {
                    "userAcc": {
                        "accountNo": "1234567890016",
                        "queryTime": "2026-07-07 00:00:00",
                        "accountBalance": "86.44元",
                        "phone": "13800138000",
                        "address": "福建省福州市测试路 1 号",
                        "password": "plain-password",
                        "token": "secret-token",
                    },
                    "mixinGetYuEdata": {
                        "consNo": "1234567890016",
                        "historyOwe": "0.00",
                        "prepayBal": "217.7",
                    },
                },
                "getters": {},
            },
            "components": [
                {
                    "tag": "DIV",
                    "className": "balance-card",
                    "text": "包含页面文字但不应进入诊断包 1234567890016",
                    "data": {
                        "api_key": "sk-secret",
                        "prepayBalance": "217.7",
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DiagnosticCollector(trigger_type="manual", output_dir=temp_dir)
            collector.set_run_id(42)
            collector.record_runtime(stage="test")
            collector.record_browser_runtime(
                "login_page_loaded",
                {
                    "page": {
                        "userAgent": "Mozilla/5.0 Test",
                        "webdriver": None,
                        "languages": ["zh-CN", "zh"],
                    },
                    "webdriver": {
                        "browser_version": "138.0.0.0",
                        "chromedriver_version": "138.0.0.0",
                    },
                    "current_url": (
                        "https://95598.cn/osgweb/login"
                        "?token=secret-token&accountNo=1234567890016"
                    ),
                },
            )
            collector.record_session(
                "before_login",
                SessionCheck(
                    checked_at="2026-07-07T00:00:00+08:00",
                    status="authenticated",
                    current_url="https://95598.cn/osgweb/userAcc?accountNo=1234567890016",
                    check_method="dom",
                    redirected_to_login=False,
                    evidence_redacted="account 1234567890016 phone 13800138000",
                ),
            )
            collector.record_page("账户余额", snapshot, account_data)
            collector.record_account_saved(account_data)
            collector.record_publish("1234567890016", "mqtt", True, "ok")
            collector.emit("success")

            latest = Path(temp_dir) / "latest"
            summary_text = (latest / "summary.txt").read_text(encoding="utf-8")
            summary_json_text = (latest / "summary.json").read_text(encoding="utf-8")
            fields_text = (latest / "fields.redacted.json").read_text(encoding="utf-8")
            browser_runtime_text = (latest / "browser-runtime.json").read_text(
                encoding="utf-8"
            )
            with zipfile.ZipFile(latest / "sgcc-debug-bundle.zip") as bundle:
                bundle_names = set(bundle.namelist())

        self.assertIn(SUMMARY_START, summary_text)
        self.assertIn(SUMMARY_END, summary_text)
        self.assertIn("run_id=42", summary_text)
        self.assertIn("account=*********0016", summary_text)
        self.assertIn("money_candidates=", summary_text)
        self.assertIn("daily=1(2026-07-06", summary_text)
        self.assertIn("monthly=1(2026-06", summary_text)
        self.assertIn("publish=publisher=mqtt", summary_text)

        payload = json.loads(summary_json_text)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["run_id"], 42)
        self.assertEqual(payload["debug"]["browser_runtime_count"], 1)
        self.assertIn("browser-runtime.json", bundle_names)
        self.assertIn("Mozilla/5.0 Test", browser_runtime_text)

        combined = (
            summary_text
            + summary_json_text
            + fields_text
            + browser_runtime_text
        )
        self.assertNotIn("1234567890016", combined)
        self.assertNotIn("13800138000", combined)
        self.assertNotIn("plain-password", combined)
        self.assertNotIn("secret-token", combined)
        self.assertNotIn("sk-secret", combined)
        self.assertNotIn("password", combined.lower())
        self.assertNotIn("token", combined.lower())
        self.assertNotIn("api_key", combined.lower())

    def test_debug_bundle_keeps_unknown_shape_but_redacts_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DiagnosticCollector(trigger_type="manual", output_dir=temp_dir)
            collector.set_run_id(43)
            scope = CaptureScope.create("账户余额", "1234567890123")
            collector.record_observations([
                Observation(
                    source="network",
                    scope_id=scope.id,
                    scope_label=scope.label,
                    account_no=scope.account_no,
                    payload={
                        "data": {
                            "accountNo": "1234567890123",
                            "realAccountValue": "36.27",
                            "phone": "13800138000",
                            "token": "secret-token",
                        }
                    },
                    metadata={"url": "https://95598.cn/api/balance?token=secret"},
                )
            ])
            collector.record_decisions([
                ParserDecision(
                    source="network",
                    scope_id=scope.id,
                    scope_label=scope.label,
                    account_no=scope.account_no,
                    status="rejected",
                    reason="unknown structured payload",
                )
            ])
            collector.emit("success")

            latest = Path(temp_dir) / "latest"
            observations = (latest / "observations.redacted.json").read_text()
            candidates = (latest / "candidates.redacted.json").read_text()
            decisions = (latest / "parser-decisions.json").read_text()
            self.assertIn("realAccountValue", observations)
            self.assertIn("realAccountValue", candidates)
            self.assertNotIn("1234567890123", observations)
            self.assertNotIn("13800138000", observations)
            self.assertNotIn("secret-token", observations)
            self.assertIn("unknown structured payload", decisions)
            self.assertTrue((latest / "sgcc-debug-bundle.zip").is_file())
            self.assertEqual(stat.S_IMODE(Path(temp_dir).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(latest.stat().st_mode), 0o700)
            for path in latest.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_record_observations_deduplicates_same_capture(self):
        collector = DiagnosticCollector(trigger_type="manual")
        observation = Observation(
            source="network",
            scope_id="scope-1",
            scope_label="账户余额",
            payload={"data": {"sumMoney": "12.34"}},
        )

        collector.record_observations([observation])
        collector.record_observations([observation])

        self.assertEqual(len(collector.observations), 1)
        self.assertEqual(len(collector.shapes), 1)

    def test_redaction_masks_unknown_numeric_identity_and_common_pii(self):
        payload = redact_structure({
            "unknownNumber": 1234567890123,
            "largeNumericId": 250101123456789012301,
            "embeddedAccountId": "prefix250101123456789012301suffix",
            "mysteryText": "110101199001011234",
            "contactValue": "person@example.com",
            "message": "failed at https://95598.cn/api/query?token=secret-token&accountNo=1234567890123",
        })

        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("1234567890123", text)
        self.assertNotIn("250101123456789012301", text)
        self.assertNotIn("110101199001011234", text)
        self.assertNotIn("person@example.com", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("<redacted-numeric-id>", text)
        self.assertIn("<redacted-email>", text)

    def test_redaction_uses_label_context_for_generic_value_fields(self):
        payload = redact_structure({
            "rows": [
                {"label": "用电地址", "value": "福建省福州市测试路 1 号"},
                {"title": "客户姓名", "text": "张三"},
                {"fieldName": "手机号", "fieldValue": "not-a-phone-shaped-value"},
                {"label": "账户余额", "value": "86.44元"},
            ]
        })

        self.assertEqual(payload["rows"][0]["value"], "<redacted>")
        self.assertEqual(payload["rows"][1]["text"], "<redacted>")
        self.assertEqual(payload["rows"][2]["fieldValue"], "<redacted>")
        self.assertEqual(payload["rows"][3]["value"], "86.44元")


if __name__ == "__main__":
    unittest.main()
