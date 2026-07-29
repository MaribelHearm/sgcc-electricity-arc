import os
import unittest
from unittest.mock import Mock, patch

from sgcc_ha_bridge.browser import (
    _apply_browser_consistency,
    _browser_service_start,
    _log_browser_runtime,
    collect_browser_runtime,
    release_driver,
)


class BrowserConsistencyTestCase(unittest.TestCase):
    @patch("sgcc_ha_bridge.browser.requests.post")
    def test_browser_service_start_logs_matching_image_revisions(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "revision": "commit-abc123",
            "ready": True,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "VERSION": "commit-abc123",
                    "SGCC_BROWSER_SERVICE_TIMEOUT": "1",
                },
                clear=True,
            ),
            self.assertLogs(level="INFO") as logs,
        ):
            _browser_service_start()

        self.assertIn(
            "app_revision=commit-abc123, browser_revision=commit-abc123, ready=True",
            "\n".join(logs.output),
        )

    @patch("sgcc_ha_bridge.browser.requests.post")
    def test_browser_service_start_warns_on_revision_mismatch(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "revision": "browser-def456",
            "ready": True,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "VERSION": "app-abc123",
                    "SGCC_BROWSER_SERVICE_TIMEOUT": "1",
                },
                clear=True,
            ),
            self.assertLogs(level="WARNING") as logs,
        ):
            _browser_service_start()

        self.assertIn(
            "app/browser 镜像 revision 不一致",
            "\n".join(logs.output),
        )

    def test_consistency_is_applied_before_navigation_for_attached_driver(self):
        driver = Mock()

        _apply_browser_consistency(driver, "zh-CN,zh,en-US,en")

        commands = [call.args[0] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(
            commands,
            [
                "Page.addScriptToEvaluateOnNewDocument",
                "Emulation.setTimezoneOverride",
                "Emulation.setLocaleOverride",
            ],
        )
        script = driver.execute_cdp_cmd.call_args_list[0].args[1]["source"]
        self.assertIn("Navigator.prototype", script)
        self.assertIn("'webdriver'", script)
        self.assertIn('"zh-CN"', script)
        driver.execute_script.assert_called_once_with(script)

    def test_runtime_snapshot_only_runs_in_debug_mode(self):
        driver = Mock()

        with patch.dict(os.environ, {}, clear=True):
            _log_browser_runtime(driver)

        driver.execute_script.assert_not_called()

    def test_runtime_snapshot_collects_browser_and_gpu_without_storage_values(self):
        driver = Mock()
        driver.current_url = "https://95598.cn/osgweb/login?token=secret"
        driver.capabilities = {
            "browserName": "chrome",
            "browserVersion": "138.0.0.0",
            "platformName": "linux",
            "pageLoadStrategy": "normal",
            "chrome": {"chromedriverVersion": "138.0.0.0 (abc)"},
        }
        driver.execute_script.return_value = {
            "userAgent": "Mozilla/5.0 Test",
            "webdriver": None,
            "languages": ["zh-CN", "zh"],
            "webgl": {"available": True, "renderer": "SwiftShader"},
        }
        driver.execute_cdp_cmd.side_effect = [
            {
                "protocolVersion": "1.3",
                "product": "Chrome/138.0.0.0",
                "revision": "r1",
                "userAgent": "Mozilla/5.0 Test",
                "jsVersion": "13.8",
            },
            {
                "modelName": "",
                "modelVersion": "",
                "gpu": {
                    "devices": [{"vendorString": "Google", "deviceString": "SwiftShader"}],
                    "featureStatus": {"webgl": "enabled"},
                },
            },
        ]

        snapshot = collect_browser_runtime(driver, stage="login_page")

        self.assertEqual(snapshot["stage"], "login_page")
        self.assertEqual(snapshot["webdriver"]["browser_version"], "138.0.0.0")
        self.assertEqual(snapshot["page"]["webgl"]["renderer"], "SwiftShader")
        self.assertNotIn("secret", snapshot["current_url"])
        self.assertNotIn("cookie", str(snapshot).lower())
        self.assertNotIn("localstorage", str(snapshot).lower())

    @patch("sgcc_ha_bridge.browser._browser_service_stop")
    def test_browser_service_closes_chrome_on_release_by_default(self, stop):
        driver = Mock()
        driver._sgcc_attached_browser = True
        driver._sgcc_browser_service_mode = True

        with patch.dict(os.environ, {}, clear=True):
            release_driver(driver)

        stop.assert_called_once_with()
        driver.command_executor.close.assert_called_once_with()
        driver.service.stop.assert_called_once_with()
        driver.quit.assert_not_called()

    @patch("sgcc_ha_bridge.browser._browser_service_stop")
    def test_browser_service_can_explicitly_keep_chrome_running(self, stop):
        driver = Mock()
        driver._sgcc_attached_browser = True
        driver._sgcc_browser_service_mode = True

        with patch.dict(
            os.environ,
            {"SGCC_BROWSER_SERVICE_STOP_ON_RELEASE": "false"},
            clear=True,
        ):
            release_driver(driver)

        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
