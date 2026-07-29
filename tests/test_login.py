import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from sgcc_ha_bridge.error_watcher import ErrorWatcher
from sgcc_ha_bridge.login import SgccLogin
from sgcc_ha_bridge.login_guard import LoginFailure


class LoginFallbackTestCase(unittest.TestCase):
    def test_fallback_method_aliases_are_normalized_and_ordered(self):
        with patch.dict(os.environ, {
            "SGCC_LOGIN_FALLBACK_METHODS": "sms,qrcode,phone_code,qrcode",
        }, clear=True):
            self.assertEqual(SgccLogin._fallback_methods(), ["phone-code", "qrcode"])

    def test_fallback_tries_next_method_after_sms_failure(self):
        login = SgccLogin.__new__(SgccLogin)
        login._phone_code_login = Mock(side_effect=LoginFailure("phone_code_timeout", "timeout"))
        login._qr_login = Mock(return_value=True)

        with self.assertLogs(level=logging.WARNING) as logs:
            result = login._fallback_login(object(), "RK001", ["phone-code", "qrcode"])

        self.assertTrue(result)
        login._phone_code_login.assert_called_once()
        login._qr_login.assert_called_once()
        self.assertIn("phone-code", "\n".join(logs.output))

    def test_fallback_stops_when_sms_hits_risk_block(self):
        login = SgccLogin.__new__(SgccLogin)
        login._phone_code_login = Mock(
            side_effect=LoginFailure("risk_blocked", "网络连接超时（RK001）")
        )
        login._qr_login = Mock(return_value=True)

        with patch.dict(os.environ, {}, clear=True), self.assertLogs(
            level=logging.WARNING
        ), self.assertRaises(LoginFailure) as raised:
            login._fallback_login(object(), "password failed", ["phone-code", "qrcode"])

        self.assertEqual(raised.exception.category, "risk_blocked")
        login._qr_login.assert_not_called()

    def test_risk_override_allows_next_fallback_after_sms_risk_block(self):
        login = SgccLogin.__new__(SgccLogin)
        login._phone_code_login = Mock(
            side_effect=LoginFailure("risk_blocked", "网络连接超时（RK001）")
        )
        login._qr_login = Mock(return_value=True)

        with patch.dict(
            os.environ, {"SGCC_RISK_FALLBACK_OVERRIDE": "true"}, clear=True
        ), self.assertLogs(level=logging.WARNING):
            result = login._fallback_login(
                object(), "password failed", ["phone-code", "qrcode"]
            )

        self.assertTrue(result)
        login._qr_login.assert_called_once()

    def test_error_route_without_token_or_authenticated_dom_is_not_logged_in(self):
        driver = Mock()
        driver.current_url = "https://95598.cn/osgweb/callback-error"
        driver.execute_script.side_effect = [False, False]

        self.assertFalse(SgccLogin.is_logged_in_page(driver))

    def test_password_form_ready_skips_redundant_account_switch(self):
        login = SgccLogin.__new__(SgccLogin)
        login.diagnostic = None
        login._login_ui_state = Mock(return_value={
            "account_login_visible": True,
            "password_form_visible": True,
            "password_tab_visible": True,
        })
        user_element = Mock()

        login._ensure_password_login_form(Mock(), user_element)

        user_element.click.assert_not_called()

    def test_account_switch_falls_back_when_native_click_does_not_change_state(self):
        login = SgccLogin.__new__(SgccLogin)
        login.diagnostic = Mock()
        login._login_ui_state = Mock(return_value={
            "account_login_visible": False,
            "password_form_visible": False,
            "password_tab_visible": False,
        })
        login._wait_for_visible_css = Mock(side_effect=[False, True])
        login._visible_css = Mock(return_value=True)
        driver = Mock()
        user_element = Mock()
        action_chain = Mock()
        action_chain.move_to_element.return_value = action_chain
        action_chain.pause.return_value = action_chain
        action_chain.click.return_value = action_chain

        with patch("sgcc_ha_bridge.login.ActionChains", return_value=action_chain):
            login._ensure_password_login_form(driver, user_element)

        action_chain.perform.assert_called_once()
        user_element.click.assert_called_once()
        driver.execute_script.assert_not_called()
        login.diagnostic.record_timeline.assert_any_call(
            "login_ui_click_attempt",
            label="账号登录入口",
            method="native",
            result="no-transition",
        )
        login.diagnostic.record_timeline.assert_any_call(
            "login_ui_click_attempt",
            label="账号登录入口",
            method="webdriver",
            result="target-visible",
        )

    def test_account_switch_failure_raises_non_generic_login_failure(self):
        login = SgccLogin.__new__(SgccLogin)
        login.diagnostic = None
        login._login_ui_state = Mock(return_value={
            "account_login_visible": False,
            "password_form_visible": False,
            "password_tab_visible": False,
        })
        login._wait_for_visible_css = Mock(return_value=False)
        driver = Mock()
        user_element = Mock()
        action_chain = Mock()
        action_chain.move_to_element.return_value = action_chain
        action_chain.pause.return_value = action_chain
        action_chain.click.return_value = action_chain

        with patch(
            "sgcc_ha_bridge.login.ActionChains",
            return_value=action_chain,
        ), self.assertRaises(LoginFailure) as raised:
            login._ensure_password_login_form(driver, user_element)

        self.assertEqual(raised.exception.category, "login_ui_failed")
        user_element.click.assert_called_once()
        driver.execute_script.assert_called_once_with(
            "arguments[0].click();",
            user_element,
        )

    @patch("sgcc_ha_bridge.login.build_login_interaction")
    @patch("sgcc_ha_bridge.login.read_sms_code")
    def test_phone_code_requires_confirmed_authenticated_state(self, read_code, build_interaction):
        driver = Mock()
        driver.find_elements.return_value = [Mock(), Mock(), Mock(), Mock()]
        login = SgccLogin.__new__(SgccLogin)
        login.driver = driver
        login._username = "13800000000"
        login.config = SimpleNamespace(RETRY_WAIT_TIME_OFFSET_UNIT=0)
        login._click_button = Mock()
        login._type_text = Mock()
        login._wait_for_login_submit_state = Mock(
            return_value=("error", "验证码错误")
        )
        interaction = Mock()
        build_interaction.return_value = interaction
        read_code.return_value = "123456"

        with self.assertRaises(LoginFailure):
            login._phone_code_login(driver, "RK001")

        interaction.notify_result.assert_called_once_with(
            "phone-code",
            False,
            "验证码错误",
        )
        self.assertNotIn("123456", str(interaction.mock_calls))

    @patch("sgcc_ha_bridge.login.solve_captcha_in_browser", return_value=True)
    @patch("sgcc_ha_bridge.login.build_login_interaction")
    @patch("sgcc_ha_bridge.login.read_sms_code")
    def test_phone_code_solves_secondary_captcha_before_success(
        self,
        read_code,
        build_interaction,
        solve_captcha,
    ):
        driver = Mock()
        driver.find_elements.return_value = [Mock(), Mock(), Mock(), Mock()]
        login = SgccLogin.__new__(SgccLogin)
        login.driver = driver
        login._username = "13800000000"
        login.config = SimpleNamespace(
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            RETRY_TIMES_LIMIT=3,
        )
        login._click_button = Mock()
        login._type_text = Mock()
        login._wait_for_login_submit_state = Mock(
            side_effect=[("captcha", None), ("authenticated", None)]
        )
        interaction = Mock()
        build_interaction.return_value = interaction
        read_code.return_value = "123456"

        self.assertTrue(login._phone_code_login(driver, "RK001"))

        solve_captcha.assert_called_once_with(driver, max_retries=3)
        interaction.notify_result.assert_called_once_with(
            "phone-code",
            True,
            "登录态已确认",
        )

    @patch("sgcc_ha_bridge.login.solve_captcha_in_browser", return_value=False)
    @patch("sgcc_ha_bridge.login.build_login_interaction")
    @patch("sgcc_ha_bridge.login.read_sms_code")
    def test_phone_code_reports_secondary_captcha_failure(
        self,
        read_code,
        build_interaction,
        solve_captcha,
    ):
        driver = Mock()
        driver.find_elements.return_value = [Mock(), Mock(), Mock(), Mock()]
        login = SgccLogin.__new__(SgccLogin)
        login.driver = driver
        login._username = "13800000000"
        login.config = SimpleNamespace(
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            RETRY_TIMES_LIMIT=2,
            DRIVER_IMPLICITY_WAIT_TIME=0,
        )
        login._click_button = Mock()
        login._type_text = Mock()
        login._wait_for_login_submit_state = Mock(return_value=("captcha", None))
        login._get_error_message = Mock(return_value=None)
        interaction = Mock()
        build_interaction.return_value = interaction
        read_code.return_value = "123456"

        with self.assertRaises(LoginFailure) as raised:
            login._phone_code_login(driver, "RK001")

        self.assertEqual(raised.exception.category, "captcha_failed")
        solve_captcha.assert_called_once_with(driver, max_retries=2)
        interaction.notify_result.assert_called_once_with(
            "phone-code",
            False,
            "短信验证码提交后的腾讯人机验证未通过",
        )

    @patch("sgcc_ha_bridge.login.build_login_interaction")
    @patch("sgcc_ha_bridge.login.WebDriverWait")
    def test_qrcode_requires_confirmed_authenticated_state(self, wait, build_interaction):
        driver = Mock()
        driver.current_url = "https://95598.cn/osgweb/callback-error"
        qr_element = Mock()
        qr_element.get_dom_attribute.return_value = "data:image/png;base64,cG5n"
        qr_element.get_attribute.return_value = "data:image/png;base64,cG5n"
        wait.return_value.until.side_effect = [Mock(), qr_element, TimeoutException()]

        login = SgccLogin.__new__(SgccLogin)
        login.driver = driver
        login.config = SimpleNamespace(
            DRIVER_IMPLICITY_WAIT_TIME=1,
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            QR_CODE_LOGIN_WAIT_COUNT=1,
            QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT=0,
        )
        interaction = Mock()
        build_interaction.return_value = interaction

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "sgcc_ha_bridge.login.get_data_dir", return_value=tmpdir
        ), patch.object(
            SgccLogin, "is_logged_in_page", return_value=False
        ), patch("sgcc_ha_bridge.login.time.sleep"):
            self.assertFalse(login._qr_login(driver, "test"))

        interaction.notify_result.assert_called_once_with(
            "qrcode",
            False,
            "扫码后仍未确认登录态",
        )

    @patch("sgcc_ha_bridge.login.WebDriverWait")
    def test_expired_qrcode_is_refreshed_before_returning_image(self, wait):
        driver = Mock()
        expired = Mock()
        expired.text = "二维码失效，点击图片重新获取"
        expired.is_displayed.return_value = True
        fresh_container = Mock()
        fresh_container.text = ""
        fresh_container.is_displayed.return_value = True
        stale_element = Mock()
        stale_element.is_displayed.return_value = True
        stale_element.get_dom_attribute.return_value = "data:image/png;base64,b2xk"
        refresh_overlay = Mock()
        refresh_overlay.is_displayed.return_value = True
        qr_element = Mock()
        qr_element.is_displayed.return_value = True
        qr_element.get_dom_attribute.return_value = "data:image/png;base64,cG5n"
        driver.execute_script.return_value = True
        container_calls = 0
        image_calls = 0

        def find_elements(by, selector):
            nonlocal container_calls
            nonlocal image_calls
            if selector == ".sweepCodePic":
                container_calls += 1
                return [expired] if container_calls == 1 else [fresh_container]
            if selector == ".sweepCodePic img":
                image_calls += 1
                return [stale_element] if image_calls <= 2 else [qr_element]
            if selector == ".sweepCodePic .erwBg":
                return [refresh_overlay]
            return []

        driver.find_elements.side_effect = find_elements
        login = SgccLogin.__new__(SgccLogin)
        login.config = SimpleNamespace(DRIVER_IMPLICITY_WAIT_TIME=1)
        login._click_element = Mock()

        def until(predicate):
            self.assertFalse(predicate(driver))
            self.assertFalse(predicate(driver))
            return predicate(driver)

        wait.return_value.until.side_effect = until

        self.assertIs(login._wait_for_fresh_qr_image(driver), qr_element)
        login._click_element.assert_called_once_with(driver, refresh_overlay)
        stale_element.is_displayed.assert_called_once()

    @patch("sgcc_ha_bridge.login.WebDriverWait")
    def test_qrcode_waits_until_async_image_source_is_loaded(self, wait):
        driver = Mock()
        blank = Mock()
        blank.is_displayed.return_value = True
        blank.get_dom_attribute.return_value = ""
        loaded = Mock()
        loaded.is_displayed.return_value = True
        loaded.get_dom_attribute.return_value = "data:image/png;base64,cG5n"
        driver.execute_script.return_value = True
        driver.find_elements.side_effect = [[], [blank], [], [loaded]]

        login = SgccLogin.__new__(SgccLogin)
        login.config = SimpleNamespace(DRIVER_IMPLICITY_WAIT_TIME=1)
        login._click_element = Mock()

        def until(predicate):
            self.assertFalse(predicate(driver))
            return predicate(driver)

        wait.return_value.until.side_effect = until

        self.assertIs(login._wait_for_fresh_qr_image(driver), loaded)
        login._click_element.assert_not_called()

    def test_risk_blocked_skips_interactive_fallback(self):
        login = SgccLogin.__new__(SgccLogin)
        login.driver = Mock()
        login._username = "13800000000"
        login._password = "password"
        login.config = SimpleNamespace(
            DRIVER_IMPLICITY_WAIT_TIME=0,
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            RETRY_TIMES_LIMIT=1,
        )
        login._safe_get = Mock()
        login._click_button = Mock()
        login._get_error_message = Mock(return_value="网络连接超时（RK001）,请重试！")
        login._fallback_login = Mock(return_value=True)
        login.driver.find_elements.return_value = [Mock(), Mock()]
        login.driver.current_url = "https://95598.cn/osgweb/login"

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {}, clear=True
        ), patch.object(
            ErrorWatcher, "_instance", ErrorWatcher(root_dir=tmpdir, driver=login.driver)
        ), patch.object(SgccLogin, "is_logged_in_page", return_value=False), patch(
            "sgcc_ha_bridge.login.WebDriverWait"
        ) as wait, patch("sgcc_ha_bridge.login.time.sleep"):
            wait.return_value.until.return_value = Mock()
            with self.assertRaises(LoginFailure) as raised:
                login.login(allow_fallback=True, fallback_methods=["phone-code"])

        self.assertEqual(raised.exception.category, "risk_blocked")
        login._fallback_login.assert_not_called()

    @patch("sgcc_ha_bridge.login.solve_captcha_in_browser", return_value=True)
    def test_risk_override_allows_fallback_after_captcha_passes(self, solve_captcha):
        login = SgccLogin.__new__(SgccLogin)
        login.driver = Mock()
        login._username = "13800000000"
        login._password = "password"
        login.config = SimpleNamespace(
            DRIVER_IMPLICITY_WAIT_TIME=0,
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            RETRY_TIMES_LIMIT=1,
        )
        login._safe_get = Mock()
        login._click_button = Mock()
        login._get_error_message = Mock(
            side_effect=[None, "网络连接超时（RK001）,请重试！"]
        )
        login._fallback_login = Mock(return_value=True)
        login.driver.find_elements.return_value = [Mock(), Mock()]
        login.driver.current_url = "https://95598.cn/osgweb/login"

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"SGCC_RISK_FALLBACK_OVERRIDE": "true"}, clear=True
        ), patch.object(
            ErrorWatcher, "_instance", ErrorWatcher(root_dir=tmpdir, driver=login.driver)
        ), patch.object(SgccLogin, "is_logged_in_page", return_value=False), patch(
            "sgcc_ha_bridge.login.WebDriverWait"
        ) as wait, patch("sgcc_ha_bridge.login.time.sleep"):
            wait.return_value.until.return_value = Mock()
            self.assertTrue(
                login.login(allow_fallback=True, fallback_methods=["phone-code"])
            )

        solve_captcha.assert_called_once()
        login._fallback_login.assert_called_once_with(
            login.driver,
            "网络连接超时（RK001）,请重试！",
            ["phone-code"],
        )

    @patch("sgcc_ha_bridge.login.solve_captcha_in_browser", return_value=True)
    def test_failed_risk_override_fallback_preserves_risk_category(self, solve_captcha):
        login = SgccLogin.__new__(SgccLogin)
        login.driver = Mock()
        login._username = "13800000000"
        login._password = "password"
        login.config = SimpleNamespace(
            DRIVER_IMPLICITY_WAIT_TIME=0,
            RETRY_WAIT_TIME_OFFSET_UNIT=0,
            RETRY_TIMES_LIMIT=1,
        )
        login._safe_get = Mock()
        login._click_button = Mock()
        login._get_error_message = Mock(
            side_effect=[None, "网络连接超时（RK001）,请重试！"]
        )
        login._fallback_login = Mock(return_value=False)
        login.driver.find_elements.return_value = [Mock(), Mock()]
        login.driver.current_url = "https://95598.cn/osgweb/login"

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"SGCC_RISK_FALLBACK_OVERRIDE": "true"}, clear=True
        ), patch.object(
            ErrorWatcher, "_instance", ErrorWatcher(root_dir=tmpdir, driver=login.driver)
        ), patch.object(SgccLogin, "is_logged_in_page", return_value=False), patch(
            "sgcc_ha_bridge.login.WebDriverWait"
        ) as wait, patch("sgcc_ha_bridge.login.time.sleep"):
            wait.return_value.until.return_value = Mock()
            with self.assertRaises(LoginFailure) as raised:
                login.login(allow_fallback=True, fallback_methods=["phone-code"])

        self.assertEqual(raised.exception.category, "risk_blocked")
        solve_captcha.assert_called_once()
        login._fallback_login.assert_called_once()


if __name__ == "__main__":
    unittest.main()
