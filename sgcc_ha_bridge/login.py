import base64
import logging
import os
import random
import time
from typing import Optional

from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common.exceptions import TimeoutException

from .captcha_selenium import has_captcha_in_browser, solve_captcha_in_browser
from .login_guard import LoginFailure, classify_login_failure, env_bool
from .config import FetcherConfig
from .const import LOGIN_URL, get_data_dir
from .error_watcher import ErrorWatcher
from .login_interaction import build_login_interaction, read_sms_code
from .redact import mask_secret, redact_text


ACCOUNT_LOGIN_SELECTOR = "#login_box .account-login"
PASSWORD_FORM_SELECTOR = "#login_box .account-login .password_form"
PASSWORD_TAB_SELECTOR = "#login_box .selectlogin-type .password_login"
PASSWORD_TERMS_SELECTOR = (
    "#login_box .account-login .password_form .checked-box.un-checked"
)
PHONE_CODE_REQUEST_SELECTOR = "#login_box a.yanzheng"


class SgccLogin:
    def __init__(
        self,
        driver,
        username: str,
        password: str,
        config: FetcherConfig,
        diagnostic=None,
    ):
        self.driver = driver
        self._username = username
        self._password = password
        self.config = config
        self.diagnostic = diagnostic

    def _record_debug_event(
        self,
        event: str,
        *,
        capture_browser: bool = False,
        **details,
    ) -> None:
        diagnostic = getattr(self, "diagnostic", None)
        if diagnostic is None:
            return
        try:
            diagnostic.record_timeline(f"login_{event}", **details)
            if capture_browser:
                from .browser import collect_browser_runtime

                diagnostic.record_browser_runtime(
                    f"login_{event}",
                    collect_browser_runtime(
                        self.driver,
                        stage=f"login_{event}",
                    ),
                )
        except Exception as error:
            logging.warning(f"记录登录 Debug 事件失败: {redact_text(error)}")

    @staticmethod
    def is_logged_in_page(driver) -> bool:
        authenticated, _ = SgccLogin.auth_evidence(driver)
        return authenticated

    @staticmethod
    def auth_evidence(driver) -> tuple[bool, str]:
        try:
            try:
                if driver.execute_script("return !!sessionStorage.getItem('accessToken')"):
                    return True, "token"
            except Exception:
                pass

            if driver.execute_script("""
                return !!(
                    document.querySelector('.el-dropdown') ||
                    document.querySelector('.userName') ||
                    document.body.innerText.includes('安全退出')
                );
            """):
                return True, "dom"
            return False, "none"
        except Exception:
            return False, "error"

    @ErrorWatcher.watch
    def login(self, phone_code=False, allow_fallback: bool = True, fallback_methods: Optional[list[str]] = None) -> bool:
        driver = self.driver
        self._record_debug_event(
            "started",
            method="phone-code" if phone_code else "password",
            fallback_enabled=allow_fallback,
        )
        try:
            self._safe_get(driver, LOGIN_URL, "登录页面")
            self._record_debug_event("page_loaded", capture_browser=True)
            if self.is_logged_in_page(driver):
                self._record_debug_event("already_authenticated")
                logging.info(f"打开登录页后检测到已登录态: {driver.current_url}")
                return True
            try:
                WebDriverWait(driver, self.config.DRIVER_IMPLICITY_WAIT_TIME * 3).until(
                    EC.visibility_of_element_located((By.CLASS_NAME, "user"))
                )
            except Exception as wait_error:
                ErrorWatcher.instance().capture("login_page_load_failed", wait_error)
                logging.error(f"登录页面加载失败: {LOGIN_URL}")
                raise LoginFailure("page_load_failed", "登录页面加载失败")
        except Exception as e:
            ErrorWatcher.instance().capture("login_page_open_failed", e)
            logging.error(f"登录页面加载失败: {LOGIN_URL}")
            raise LoginFailure("page_load_failed", "登录页面加载失败")
        logging.info(f"打开登录页面: {LOGIN_URL}。\r")
        time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT*2)
        # swtich to username-password login page
        # 临时关闭隐式等待，避免与 WebDriverWait 叠加导致超时
        driver.implicitly_wait(0)
        try:
            WebDriverWait(driver, 10).until(
                EC.invisibility_of_element_located((By.CLASS_NAME, 'el-loading-mask')))
        finally:
            driver.implicitly_wait(self.config.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

        element = WebDriverWait(driver, self.config.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'user')))
        logging.info("已找到 'user' 元素，准备切换账号登录。\r")
        self._ensure_password_login_form(driver, element)
        time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 点击同意按钮
        self._click_button(driver, By.CSS_SELECTOR, PASSWORD_TERMS_SELECTOR)
        logging.info("已点击同意选项。\r")
        time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT)
        if phone_code:
            return self._phone_code_login(driver, "已显式配置短信验证码登录")
        # 增加判空校验便于测试备用方案
        elif self._password is not None and len(self._password) > 0:
            # 输入用户名和密码
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            self._type_text(input_elements[0], self._username)
            logging.info(f"已输入用户名: {mask_secret(self._username)}\r")
            self._type_text(input_elements[1], self._password)
            logging.info("已输入密码: ***MASKED***\r")

            # 点击登录按钮
            self._click_button(driver, By.CLASS_NAME, "el-button.el-button--primary")
            self._record_debug_event("password_submitted", capture_browser=True)
            time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("已点击登录按钮。\r")

            # 快速检查：如果已经跳转离开登录页，说明无需验证码，直接成功
            if driver.current_url != LOGIN_URL:
                logging.info("无需验证码登录成功 (已被重定向)。\r")
                return True

            # 会出现点击登录直接失败（账号被限制登录）
            error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
            if error is None:
                # 处理腾讯点击验证码
                self._record_debug_event("password_captcha_started")
                captcha_passed = solve_captcha_in_browser(driver, max_retries=self.config.RETRY_TIMES_LIMIT)
                self._record_debug_event(
                    "password_captcha_finished",
                    capture_browser=True,
                    passed=captcha_passed,
                )
                if captcha_passed:
                    time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT)
                    if driver.current_url != LOGIN_URL:
                        logging.info("通过点击验证码登录成功。\r")
                        return True
                    else:
                        error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                        if error:
                            logging.info(f"验证码通过但登录失败: [{error}]\r")
                        else:
                            error = "验证码已通过但仍停留在登录页面。"
                            logging.error(error)
                        category = classify_login_failure(error, captcha_passed=True)
                        ErrorWatcher.instance().capture(f"login_failed_{category}", error)
                        if (
                            allow_fallback
                            and self._fallback_allowed_for(category)
                            and self._fallback_login(driver, error, fallback_methods)
                        ):
                            return True
                        raise LoginFailure(category, error)
                else:
                    error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span") or "点击验证码识别在所有重试后均失败。"
                    logging.error("点击验证码识别在所有重试后均失败。")
                    category = classify_login_failure(error, captcha_failed=True)
                    ErrorWatcher.instance().capture(f"login_failed_{category}", error)
                    if (
                        allow_fallback
                        and self._fallback_allowed_for(category)
                        and self._fallback_login(driver, error, fallback_methods)
                    ):
                        return True
                    raise LoginFailure(category, error)
            else:
                self._record_debug_event(
                    "password_rejected",
                    capture_browser=True,
                    category=classify_login_failure(error),
                    error=error,
                )
                logging.error(f"登录失败: [{error}]\r")
                category = classify_login_failure(error)
                ErrorWatcher.instance().capture(f"login_failed_{category}", error)
                if (
                    allow_fallback
                    and self._fallback_allowed_for(category)
                    and self._fallback_login(driver, error, fallback_methods)
                ):
                    return True
                raise LoginFailure(category, error)
        raise LoginFailure("login_failed", "登录失败")

    def _safe_get(self, driver, url: str, label: str = "页面", fast: bool = False):
        """Navigate with a bounded page-load timeout.

        95598 pages may keep long-polling or hold subresources open. Selenium's
        default get() waits for full document load and can block the whole fetch
        job. For post-login SPA pages, use JS navigation and stop loading after
        the route/DOM becomes observable.
        """
        logging.info(f"正在打开{label}: {url}")
        if fast:
            old_wait = self.config.DRIVER_IMPLICITY_WAIT_TIME
            try:
                driver.implicitly_wait(0)
                driver.execute_script("window.location.href = arguments[0];", url)
                deadline = int(os.getenv("FAST_NAV_WAIT", 20))
                WebDriverWait(driver, deadline).until(
                    lambda d: url.split('/osgweb')[-1] in (d.current_url or '')
                    or (d.execute_script("return document.readyState") in ("interactive", "complete"))
                )
            except TimeoutException as e:
                logging.warning(f"快速打开{label}等待超时，执行 window.stop() 后继续: {e}")
            except Exception as e:
                logging.warning(f"快速打开{label}异常，继续使用当前页面: {e}")
            finally:
                try:
                    driver.execute_script("window.stop();")
                except Exception as stop_error:
                    logging.warning(f"{label} window.stop() 失败: {stop_error}")
                driver.implicitly_wait(old_wait)
            return

        try:
            driver.get(url)
        except TimeoutException as e:
            logging.warning(f"打开{label}超时({self.config.PAGE_LOAD_TIMEOUT}s)，执行 window.stop() 后继续: {e}")
            try:
                driver.execute_script("window.stop();")
            except Exception as stop_error:
                logging.warning(f"{label} window.stop() 失败: {stop_error}")
        except Exception as e:
            logging.warning(f"打开{label}异常，继续使用当前页面: {e}")

    def _click_button(self, driver, button_search_type, button_search_key):
        '''封装点击函数，仅在元素可点击时点击'''
        click_element = driver.find_element(button_search_type, button_search_key)
        WebDriverWait(driver, self.config.DRIVER_IMPLICITY_WAIT_TIME).until(EC.element_to_be_clickable(click_element))
        self._click_element(driver, click_element)
        time.sleep(random.uniform(0.1, 0.5))

    def _ensure_password_login_form(self, driver, user_element) -> None:
        """Switch from QR view and verify the password form actually became visible."""
        initial_state = self._login_ui_state(driver)
        self._record_debug_event("ui_state_before_account_switch", **initial_state)
        if initial_state["password_form_visible"]:
            logging.info("密码登录表单已显示，无需重复切换登录标签。\r")
            self._record_debug_event(
                "password_form_ready",
                switch_method="already-visible",
            )
            return

        if not initial_state["account_login_visible"]:
            switch_method = self._click_until_visible(
                driver,
                user_element,
                ACCOUNT_LOGIN_SELECTOR,
                label="账号登录入口",
            )
            if switch_method is None:
                state = self._login_ui_state(driver)
                self._record_debug_event(
                    "account_switch_failed",
                    capture_browser=True,
                    **state,
                )
                raise LoginFailure(
                    "login_ui_failed",
                    "点击账号登录入口后密码登录面板未显示",
                )
            logging.info(f"账号登录面板已显示，点击方式={switch_method}。\r")
            self._record_debug_event(
                "account_switch_succeeded",
                switch_method=switch_method,
            )

        if not self._visible_css(driver, PASSWORD_FORM_SELECTOR):
            try:
                password_tab = WebDriverWait(driver, 5).until(
                    EC.visibility_of_element_located(
                        (By.CSS_SELECTOR, PASSWORD_TAB_SELECTOR)
                    )
                )
            except TimeoutException as error:
                self._record_debug_event(
                    "password_tab_missing",
                    capture_browser=True,
                    **self._login_ui_state(driver),
                )
                raise LoginFailure(
                    "login_ui_failed",
                    "账号登录面板已显示但未找到密码登录标签",
                ) from error
            switch_method = self._click_until_visible(
                driver,
                password_tab,
                PASSWORD_FORM_SELECTOR,
                label="密码登录标签",
            )
            if switch_method is None:
                self._record_debug_event(
                    "password_tab_switch_failed",
                    capture_browser=True,
                    **self._login_ui_state(driver),
                )
                raise LoginFailure(
                    "login_ui_failed",
                    "点击密码登录标签后密码表单未显示",
                )

        self._record_debug_event(
            "password_form_ready",
            **self._login_ui_state(driver),
        )

    def _click_until_visible(
        self,
        driver,
        element,
        target_selector: str,
        *,
        label: str,
    ) -> Optional[str]:
        attempts = (
            (
                "native",
                lambda: ActionChains(driver).move_to_element(element).pause(
                    random.uniform(0.08, 0.25)
                ).click().perform(),
            ),
            ("webdriver", element.click),
            (
                "javascript",
                lambda: driver.execute_script("arguments[0].click();", element),
            ),
        )
        for method, action in attempts:
            try:
                action()
            except Exception as error:
                error_detail = redact_text(error)[:500]
                logging.warning(
                    f"{label}点击失败，方式={method}, "
                    f"error={type(error).__name__}: {error_detail}"
                )
                self._record_debug_event(
                    "ui_click_attempt",
                    label=label,
                    method=method,
                    result="error",
                    error_type=type(error).__name__,
                    error=error_detail,
                )
                continue
            if self._wait_for_visible_css(driver, target_selector, timeout=2):
                self._record_debug_event(
                    "ui_click_attempt",
                    label=label,
                    method=method,
                    result="target-visible",
                )
                return method
            logging.warning(
                f"{label}点击后页面状态未切换，准备回退下一点击方式: {method}"
            )
            self._record_debug_event(
                "ui_click_attempt",
                label=label,
                method=method,
                result="no-transition",
            )
        return None

    @staticmethod
    def _visible_css(driver, selector: str) -> bool:
        try:
            return bool(driver.execute_script("""
                const element = document.querySelector(arguments[0]);
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    Number(style.opacity || 1) !== 0 &&
                    rect.width > 0 &&
                    rect.height > 0;
            """, selector))
        except Exception:
            return False

    def _wait_for_visible_css(self, driver, selector: str, timeout: float) -> bool:
        try:
            WebDriverWait(driver, timeout, poll_frequency=0.2).until(
                lambda current_driver: self._visible_css(
                    current_driver,
                    selector,
                )
            )
            return True
        except TimeoutException:
            return False

    def _login_ui_state(self, driver) -> dict[str, bool]:
        return {
            "account_login_visible": self._visible_css(
                driver,
                ACCOUNT_LOGIN_SELECTOR,
            ),
            "password_form_visible": self._visible_css(
                driver,
                PASSWORD_FORM_SELECTOR,
            ),
            "password_tab_visible": self._visible_css(
                driver,
                PASSWORD_TAB_SELECTOR,
            ),
        }

    @staticmethod
    def _click_element(driver, element) -> None:
        """Prefer a pointer action so the page receives a normal mouse event chain."""
        try:
            ActionChains(driver).move_to_element(element).pause(
                random.uniform(0.08, 0.25)
            ).click().perform()
            return
        except Exception as action_error:
            logging.warning(f"原生鼠标点击失败，回退 WebElement.click(): {action_error}")
        element.click()

    @staticmethod
    def _type_text(element, value: str) -> None:
        """Type one character at a time instead of injecting a full value instantly."""
        try:
            min_delay = float(os.getenv("SGCC_TYPE_DELAY_MIN_SECONDS", "0.04"))
        except (TypeError, ValueError):
            min_delay = 0.04
        try:
            max_delay = float(os.getenv("SGCC_TYPE_DELAY_MAX_SECONDS", "0.12"))
        except (TypeError, ValueError):
            max_delay = 0.12
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay
        for character in value or "":
            element.send_keys(character)
            time.sleep(random.uniform(max(0.0, min_delay), max(0.0, max_delay)))

    def _get_error_message(self, driver, path) -> Optional[str]:
        """获取错误信息，如果不存在则返回 None"""
        # 关闭隐式等待
        driver.implicitly_wait(0)
        try:
            element = driver.find_element(By.XPATH, path)
            return element.text
        except Exception:
            return None
        finally:
            driver.implicitly_wait(self.config.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

    def _wait_for_login_submit_state(self, driver, timeout: int = 15) -> tuple[str, Optional[str]]:
        """Wait for one decisive result after submitting a login form."""

        def observe(d):
            if self.is_logged_in_page(d):
                return ("authenticated", None)
            if has_captcha_in_browser(d):
                return ("captcha", None)
            error = self._get_error_message(d, "//div[@class='errmsg-tip']//span")
            if error:
                return ("error", error)
            return False

        try:
            result = WebDriverWait(driver, timeout).until(observe)
            if result:
                return result
        except Exception:
            pass

        if self.is_logged_in_page(driver):
            return ("authenticated", None)
        if has_captcha_in_browser(driver):
            return ("captcha", None)
        error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
        if error:
            return ("error", error)
        return ("unknown", None)

    def _fallback_login(self, driver, reason: str, methods: Optional[list[str]] = None) -> bool:
        """Try explicitly configured interactive login methods in order."""
        if methods is None:
            methods = self._fallback_methods()
        for method in methods:
            try:
                if method == "phone-code":
                    if self._phone_code_login(driver, reason):
                        return True
                elif method == "qrcode":
                    if self._qr_login(driver, reason):
                        return True
            except LoginFailure as fallback_error:
                logging.warning(
                    f"登录兜底方式 {method} 执行失败: {redact_text(fallback_error)}"
                )
                if not self._fallback_allowed_for(fallback_error.category):
                    raise
            except Exception as fallback_error:
                logging.warning(
                    f"登录兜底方式 {method} 执行失败，继续尝试下一方式: "
                    f"{redact_text(fallback_error)}"
                )
        return False

    @staticmethod
    def _fallback_allowed_for(category: str) -> bool:
        return category != "risk_blocked" or env_bool("SGCC_RISK_FALLBACK_OVERRIDE", False)

    @staticmethod
    def _fallback_methods() -> list[str]:
        raw = os.getenv("SGCC_LOGIN_FALLBACK_METHODS") or os.getenv("LOGIN_FALLBACK", "")
        methods = []
        for value in raw.replace("+", ",").split(","):
            method = value.strip().lower().replace("_", "-")
            if method in {"sms", "phone", "phonecode"}:
                method = "phone-code"
            if method in {"phone-code", "qrcode"} and method not in methods:
                methods.append(method)
        return methods

    def _phone_code_login(self, driver, reason: str) -> bool:
        logging.info("短信验证码登录开始。")
        self._record_debug_event("phone_code_started")
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
        time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT)
        input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
        if len(input_elements) < 4:
            raise LoginFailure("phone_code_page_failed", "短信验证码登录页面输入框不完整")
        input_elements[2].clear()
        self._type_text(input_elements[2], self._username)
        logging.info(f"已输入用户名: {mask_secret(self._username)}\r")
        request_state = self._request_phone_code(driver)
        self._record_debug_event(
            "phone_code_requested",
            request_status=request_state.get("status", ""),
            auth_time=request_state.get("auth_time", 0),
        )

        interaction = build_login_interaction()
        code = read_sms_code(interaction, reason)
        if not code:
            interaction.notify_result("phone-code", False, "未在有效时间内收到短信验证码")
            raise LoginFailure("phone_code_timeout", "未在有效时间内收到短信验证码")
        self._type_text(input_elements[3], code)
        code = None
        logging.info("已输入手机验证码。\r")
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span')
        self._record_debug_event("phone_code_submitted", capture_browser=True)
        state, error = self._wait_for_login_submit_state(driver)
        self._record_debug_event(
            "phone_code_state",
            capture_browser=True,
            state=state,
            error=error or "",
        )

        if state == "captcha":
            logging.info("短信验证码提交后出现腾讯人机验证，开始处理。")
            self._record_debug_event("phone_code_captcha_started")
            captcha_passed = solve_captcha_in_browser(
                driver,
                max_retries=self.config.RETRY_TIMES_LIMIT,
            )
            self._record_debug_event(
                "phone_code_captcha_finished",
                capture_browser=True,
                passed=captcha_passed,
            )
            if captcha_passed:
                state, error = self._wait_for_login_submit_state(driver)
                self._record_debug_event(
                    "phone_code_post_captcha_state",
                    capture_browser=True,
                    state=state,
                    error=error or "",
                )
            else:
                error = (
                    self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                    or "短信验证码提交后的腾讯人机验证未通过"
                )
                interaction.notify_result("phone-code", False, error)
                raise LoginFailure(
                    classify_login_failure(error, captcha_failed=True),
                    error,
                )

        success = state == "authenticated"
        interaction.notify_result(
            "phone-code",
            success,
            "登录态已确认"
            if success
            else (
                error
                or "短信验证码及人机验证提交后仍未检测到登录态"
            ),
        )
        if success:
            logging.info("短信验证码登录成功。")
            return True
        error = (
            error
            or self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
            or "短信验证码及人机验证提交后仍未登录"
        )
        raise LoginFailure(classify_login_failure(error), error)

    def _request_phone_code(self, driver) -> dict:
        """Click the visible send-code control and require a decisive frontend state."""
        attempts = (
            (
                "native",
                lambda element: ActionChains(driver).move_to_element(element).pause(
                    random.uniform(0.08, 0.25)
                ).click().perform(),
            ),
            ("webdriver", lambda element: element.click()),
            (
                "javascript",
                lambda element: driver.execute_script(
                    "arguments[0].click();",
                    element,
                ),
            ),
        )

        for method, action in attempts:
            element = self._visible_phone_code_request_element(driver)
            if element is None:
                raise LoginFailure(
                    "phone_code_request_failed",
                    "未找到可见的获取验证码按钮",
                )
            try:
                action(element)
            except Exception as error:
                logging.warning(
                    "获取短信验证码点击失败，方式=%s, error=%s: %s",
                    method,
                    type(error).__name__,
                    redact_text(error)[:500],
                )
                continue

            state = self._wait_for_phone_code_request_state(driver, timeout=3.0)
            if state["status"] == "sent":
                logging.info("国网前端已确认短信验证码发送。")
                return state
            if state["status"] == "error":
                raise LoginFailure(
                    "phone_code_request_failed",
                    state.get("error") or "国网前端返回短信验证码发送失败",
                )
            if state["status"] == "captcha":
                logging.info("发送短信验证码前出现腾讯人机验证，开始处理。")
                self._record_debug_event("phone_code_request_captcha_started")
                passed = solve_captcha_in_browser(
                    driver,
                    max_retries=self.config.RETRY_TIMES_LIMIT,
                )
                self._record_debug_event(
                    "phone_code_request_captcha_finished",
                    capture_browser=True,
                    passed=passed,
                )
                if not passed:
                    raise LoginFailure(
                        "captcha_failed",
                        "发送短信验证码前的腾讯人机验证未通过",
                    )
                state = self._wait_for_phone_code_request_state(
                    driver,
                    timeout=10.0,
                )
                if state["status"] == "sent":
                    logging.info("人机验证通过，国网前端已确认短信验证码发送。")
                    return state
                if state["status"] == "error":
                    raise LoginFailure(
                        "phone_code_request_failed",
                        state.get("error") or "人机验证通过后短信验证码发送失败",
                    )

            logging.warning(
                "获取验证码点击后前端未确认发送，准备回退下一点击方式: %s",
                method,
            )

        raise LoginFailure(
            "phone_code_request_failed",
            "点击获取验证码后前端未确认短信已发送",
        )

    @staticmethod
    def _visible_phone_code_request_element(driver):
        elements = driver.find_elements(By.CSS_SELECTOR, PHONE_CODE_REQUEST_SELECTOR)
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
        return None

    def _wait_for_phone_code_request_state(
        self,
        driver,
        *,
        timeout: float,
    ) -> dict:
        deadline = time.monotonic() + timeout
        latest = {"status": "pending", "auth_time": 0, "error": ""}
        while time.monotonic() < deadline:
            latest = self._phone_code_request_state(driver)
            if latest["status"] in {"sent", "error"}:
                return latest
            if has_captcha_in_browser(driver):
                return {**latest, "status": "captcha"}
            time.sleep(0.2)
        return latest

    @staticmethod
    def _phone_code_request_state(driver) -> dict:
        try:
            state = driver.execute_script("""
                const seen = new Set();
                let authTime = 0;
                let sendAuthCode = null;
                let codeMsg = '';
                let validError = '';
                let sendError = '';
                let success = false;
                let errorVisible = false;

                for (const element of document.querySelectorAll('*')) {
                    let vm = element.__vue__;
                    while (vm) {
                        if (!seen.has(vm)) {
                            seen.add(vm);
                            const data = vm.$data || {};
                            if (Number(data.auth_time || 0) > authTime) {
                                authTime = Number(data.auth_time || 0);
                            }
                            if (typeof data.sendAuthCode === 'boolean') {
                                sendAuthCode = data.sendAuthCode;
                            }
                            if (typeof data.codeMsg === 'string' && data.codeMsg) {
                                codeMsg = data.codeMsg;
                            }
                            const message = data.passLoginErrMsg;
                            if (message && typeof message === 'object') {
                                validError = String(message.validErr || validError || '');
                                sendError = String(message.sendErr || sendError || '');
                                success = Boolean(message.isSucess || success);
                                errorVisible = Boolean(
                                    message.isShow || message.isShowCode || errorVisible
                                );
                            }
                        }
                        vm = vm.$parent;
                    }
                }

                const sent = authTime > 0
                    || sendAuthCode === false
                    || success
                    || /验证码已发送|Verification Code has been sent/i.test(validError);
                const failed = !sent && errorVisible && Boolean(sendError || validError);
                return {
                    status: sent ? 'sent' : failed ? 'error' : 'pending',
                    auth_time: authTime,
                    code_msg: codeMsg,
                    error: failed ? (sendError || validError) : '',
                };
            """)
            if isinstance(state, dict):
                return {
                    "status": state.get("status", "pending"),
                    "auth_time": int(state.get("auth_time") or 0),
                    "code_msg": str(state.get("code_msg") or ""),
                    "error": str(state.get("error") or ""),
                }
        except Exception as error:
            logging.warning(
                "读取短信验证码发送状态失败[%s]: %s",
                type(error).__name__,
                redact_text(error)[:500],
            )
        return {"status": "pending", "auth_time": 0, "code_msg": "", "error": ""}

    def _qr_login(self, driver, reason: str) -> bool:
        logging.info("二维码登录开始")
        # 切换验证码
        element = WebDriverWait(driver, self.config.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'qr_code')))
        self._click_element(driver, element)
        logging.info("已切换到二维码模式")

        time.sleep(self.config.RETRY_WAIT_TIME_OFFSET_UNIT)
        qrElement = self._wait_for_fresh_qr_image(driver)
        logging.info("已找到二维码图片元素")

        try:
            img_src = qrElement.get_dom_attribute("src") or ""
        except Exception:
            img_src = qrElement.get_attribute("src") or ""
        if not isinstance(img_src, str):
            img_src = qrElement.get_attribute("src") or ""

        if img_src and img_src.startswith('data:image'):
            base64_data = img_src.split(',')[1]
            img_screenshot = base64.b64decode(base64_data)
        else:
          logging.info('二维码图片源不是 base64 格式')
          img_screenshot = qrElement.screenshot_as_png

        qr_path = os.path.join(get_data_dir(), "login_qr_code.png")
        with open(qr_path, "wb") as f:
            f.write(img_screenshot)
        try:
            os.chmod(qr_path, 0o600)
        except OSError:
            pass
        logging.info(f"已临时保存登录二维码到 {qr_path}")

        interaction = build_login_interaction()
        try:
            try:
                interaction.send_qr_code(img_screenshot, reason)
            except Exception as notify_error:
                logging.warning(f"二维码通知失败，继续等待本地扫码: {notify_error}")
            for i in range(1, self.config.QR_CODE_LOGIN_WAIT_COUNT + 1):
                logging.info(f'二维码登录等待检查[{self.config.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT}] 次数[{i}]')
                time.sleep(self.config.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT)
                if driver.current_url != LOGIN_URL:
                    try:
                        WebDriverWait(
                            driver,
                            self.config.DRIVER_IMPLICITY_WAIT_TIME,
                        ).until(self.is_logged_in_page)
                    except TimeoutException:
                        logging.warning("二维码扫码后页面已跳转，但未确认有效登录态。")
                    if self.is_logged_in_page(driver):
                        logging.info("二维码登录成功")
                        interaction.notify_result("qrcode", True, "登录态已确认")
                        return True
                    interaction.notify_result("qrcode", False, "扫码后仍未确认登录态")
                    return False
                error = self._get_error_message(driver, "//div[@class='sweepCodePic']//div[@class='erwBg']//p")
                if error is not None:
                    logging.error(f'二维码登录错误[{error}]')
                    interaction.notify_result("qrcode", False, error)
                    return False

            logging.warning("二维码登录超时")
            interaction.notify_result("qrcode", False, "等待扫码超时")
            return False
        finally:
            try:
                os.remove(qr_path)
                logging.info("登录二维码临时文件已删除。")
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                logging.warning(f"删除登录二维码临时文件失败: {cleanup_error}")

    def _wait_for_fresh_qr_image(self, driver):
        """Wait for a QR image and refresh the expired placeholder once."""
        refresh_clicked = False
        stale_sources = set()

        def image_source(image):
            try:
                src = image.get_dom_attribute("src") or ""
            except Exception:
                src = image.get_attribute("src") or ""
            if not isinstance(src, str):
                src = image.get_attribute("src") or ""
            return src

        def locate_or_refresh(d):
            nonlocal refresh_clicked
            expired_visible = False
            for container in d.find_elements(By.CSS_SELECTOR, ".sweepCodePic"):
                try:
                    text = (container.text or "").strip()
                    expired_visible = container.is_displayed() and any(
                        marker in text for marker in ("二维码失效", "重新获取")
                    )
                    if expired_visible and not refresh_clicked:
                        for image in d.find_elements(
                            By.CSS_SELECTOR,
                            ".sweepCodePic img",
                        ):
                            source = image_source(image)
                            if source:
                                stale_sources.add(source)
                        logging.info("二维码已失效，点击页面重新获取")
                        refresh_target = container
                        for overlay in d.find_elements(
                            By.CSS_SELECTOR,
                            ".sweepCodePic .erwBg",
                        ):
                            if overlay.is_displayed():
                                refresh_target = overlay
                                break
                        self._click_element(d, refresh_target)
                        refresh_clicked = True
                except Exception as refresh_error:
                    logging.warning(f"刷新失效二维码失败: {refresh_error}")
            if expired_visible:
                return False

            for image in d.find_elements(By.CSS_SELECTOR, ".sweepCodePic img"):
                try:
                    src = image_source(image)
                    loaded = d.execute_script(
                        "return Boolean(arguments[0].complete) && "
                        "Number(arguments[0].naturalWidth || 0) > 0;",
                        image,
                    )
                    if (
                        image.is_displayed()
                        and src
                        and src not in stale_sources
                        and loaded
                    ):
                        return image
                except Exception:
                    continue
            return False

        return WebDriverWait(
            driver,
            self.config.DRIVER_IMPLICITY_WAIT_TIME,
        ).until(locate_or_refresh)

    def _random_delay(self, min_seconds=0.5, max_seconds=3.0):
        """添加随机延迟，使自动化操作更难被检测。"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)
