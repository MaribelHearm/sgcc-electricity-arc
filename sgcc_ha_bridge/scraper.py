"""Path B scraper: read decrypted SGCC data from live Vue/Vuex state.

The caller owns the Selenium driver.  This module never quits/closes the driver
and assumes the browser is already authenticated.
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .const import BALANCE_URL, ELECTRIC_USAGE_URL
from .model import AccountData, mask_account_no
from .parser import merge_account_data, parse_account_data
from .cache_validity import has_useful_account_data
from .diag import debug_enabled
from .extractor import extract_account_data
from .observation import CaptureScope, Observation
from . import vue_state


@dataclass(frozen=True)
class AccountOption:
    """Stable account selector identity captured from the live Vue option."""

    index: int
    account_no: str = ""


class Scraper:
    """Scrape AccountData from an attached, logged-in Selenium driver."""

    def __init__(
        self,
        driver,
        wait_seconds: int = 12,
        settle_seconds: Optional[float] = None,
        diagnostic: Any = None,
        network_recorder: Any = None,
    ):
        self.driver = driver
        self.wait_seconds = wait_seconds
        self.settle_seconds = self._settle_seconds_from_env() if settle_seconds is None else settle_seconds
        self.diagnostic = diagnostic
        self.network_recorder = network_recorder
        self.account_set_authoritative = False
        self._selector_enumeration_failed = False
        self._active_scope: Optional[CaptureScope] = None

    @staticmethod
    def _settle_seconds_from_env() -> float:
        try:
            return max(0.0, float(os.getenv("SCRAPER_SETTLE_SECONDS", "3.0")))
        except (TypeError, ValueError):
            return 3.0

    def fetch_all(self, max_accounts: Optional[int] = None) -> list[AccountData]:
        """Navigate balance/usage views and return one AccountData per account.

        Multi-account handling is deliberately inclusive:

        * scrape the currently selected/default account first;
        * then iterate every visible selector option by stable account number;
        * de-duplicate parsed accounts by account number.

        Some SGCC/Element UI account selectors only list accounts that can be
        switched to, excluding the currently selected account; others include
        the current account.  Display text is not an identity source: current
        Element UI pages expose the real number through option Vue props.
        """
        self._navigate(BALANCE_URL, "账户余额")
        self._selector_enumeration_failed = False
        account_options = self._visible_account_options()
        self.account_set_authoritative = not self._selector_enumeration_failed
        selector_option_count = len(account_options)
        logging.info(f"Path B 账户候选: 当前账户 + {selector_option_count} 个下拉选项。")
        if self.diagnostic is not None:
            self.diagnostic.record_selector_options(selector_option_count)

        results: list[AccountData] = []
        seen_accounts: set[str] = set()

        current = self._fetch_current_account()
        if current is None or not self._account_identity(current):
            self.account_set_authoritative = False
        self._append_unique_result(results, seen_accounts, current)
        if max_accounts is not None and len(results) >= max_accounts:
            if account_options:
                self.account_set_authoritative = False
            return results

        current_identity = self._account_identity(current) if current is not None else None
        seen_option_accounts: set[str] = set()
        for option in account_options:
            if option.account_no:
                if option.account_no == current_identity:
                    logging.info(f"Path B 跳过下拉中的当前账户: {mask_account_no(option.account_no)}")
                    continue
                if option.account_no in seen_option_accounts:
                    logging.info(f"Path B 跳过重复账户选项: {mask_account_no(option.account_no)}")
                    continue
                seen_option_accounts.add(option.account_no)

            data = self._fetch_selected_account(option)
            if data is None or not self._account_identity(data):
                self.account_set_authoritative = False
            self._append_unique_result(results, seen_accounts, data)
            if max_accounts is not None and len(results) >= max_accounts:
                if option != account_options[-1]:
                    self.account_set_authoritative = False
                break
        return results

    def fetch_one(self) -> AccountData:
        """Convenience wrapper for the current/default account."""
        data = self._fetch_current_account()
        return data if data is not None else AccountData(account=parse_account_data().account)

    def _fetch_current_account(self) -> Optional[AccountData]:
        return self._fetch_account(selection=None)

    def _fetch_selected_account(self, option: AccountOption) -> Optional[AccountData]:
        return self._fetch_account(selection=option)

    def _fetch_account(self, selection: Optional[AccountOption]) -> Optional[AccountData]:
        partials: list[AccountData] = []

        balance_scope = self._begin_scope(
            "账户余额",
            selection.account_no if selection is not None else "",
        )
        try:
            self._navigate(BALANCE_URL, "账户余额")
            if selection is not None and not self._select_account(
                account_no=selection.account_no,
                fallback_index=selection.index,
            ):
                logging.warning(f"Path B 无法选择账户下拉第 {selection.index + 1} 项，已跳过该候选。")
                return None
            partials.append(self._parse_current_page("账户余额", balance_scope))
        finally:
            self._end_scope(balance_scope)
        target_account_no = partials[-1].account.account_no
        if selection is not None and selection.account_no and target_account_no != selection.account_no:
            logging.warning(
                "Path B 账户余额页选择后身份校验失败: "
                f"expected={mask_account_no(selection.account_no)}, "
                f"actual={mask_account_no(target_account_no)}；已跳过该候选。"
            )
            return None
        if selection is not None and not target_account_no:
            logging.warning("Path B 账户余额页选择后未解析到户号，已跳过该候选。")
            return None

        monthly_scope = self._begin_scope("月度电费", target_account_no)
        try:
            self._navigate(ELECTRIC_USAGE_URL, "电量电费查询")
            if target_account_no:
                current_account_no = self._current_account_no()
                if target_account_no and current_account_no == target_account_no:
                    logging.info(f"Path B 电量电费页已保持账户: {mask_account_no(target_account_no)}")
                elif not self._select_account(account_no=target_account_no):
                    logging.warning(
                        f"Path B 电量电费页无法按户号重新选择 {mask_account_no(target_account_no)}，已跳过该候选。"
                    )
                    return None
            self._click_tab("月度电费")
            monthly = self._parse_current_page("月度电费", monthly_scope)
        finally:
            self._end_scope(monthly_scope)
        if not self._page_matches_target_account(monthly, target_account_no, "月度电费"):
            return None
        partials.append(monthly)
        daily_scope = self._begin_scope("日用电量", target_account_no)
        try:
            self._click_tab("日用电量")
            self._expand_daily_range_to_30_days()
            daily = self._parse_current_page("日用电量", daily_scope)
        finally:
            self._end_scope(daily_scope)
        if not self._page_matches_target_account(daily, target_account_no, "日用电量"):
            return None
        partials.append(daily)

        try:
            return merge_account_data(*partials)
        except ValueError as exc:
            logging.warning(f"Path B 跨页面账户身份不一致，已跳过该候选: {exc}")
            return None

    @staticmethod
    def _page_matches_target_account(data: AccountData, target_account_no: str, label: str) -> bool:
        page_account_no = data.account.account_no if data and data.account else ""
        if not target_account_no or not page_account_no or page_account_no == target_account_no:
            return True
        logging.warning(
            f"Path B {label}页账户身份与余额页不一致: "
            f"target={mask_account_no(target_account_no)}, "
            f"actual={mask_account_no(page_account_no)}。"
        )
        return False

    def _append_unique_result(
        self,
        results: list[AccountData],
        seen_accounts: set[str],
        data: Optional[AccountData],
    ) -> bool:
        if data is None:
            return False
        identity = self._account_identity(data)
        if identity and identity in seen_accounts:
            logging.info(f"Path B 跳过重复账户候选: {mask_account_no(data.account.account_no)}")
            return False
        if identity:
            seen_accounts.add(identity)
        results.append(data)
        return True

    @staticmethod
    def _account_identity(data: AccountData) -> Optional[str]:
        account_no = data.account.account_no if data and data.account else ""
        return account_no or None

    def _current_account_no(self) -> str:
        try:
            snapshot = self._snapshot()
            data = parse_account_data(store=snapshot.get("store"), components=snapshot.get("components"))
            return data.account.account_no
        except Exception:
            return ""

    def _parse_current_page(
        self,
        label: str = "当前页",
        scope: Optional[CaptureScope] = None,
    ) -> AccountData:
        scope = scope or self._active_scope or CaptureScope.create(
            label,
            url=self._safe_current_url(),
        )
        # Production observations must be identical with Debug on or off.
        snapshot = self._snapshot(wide_debug=False)
        observations = self._observations_for_scope(scope, snapshot)
        extraction = extract_account_data(scope, observations)
        data = extraction.data
        if not has_useful_account_data(data) and not any(
            observation.source == "dom" for observation in observations
        ):
            dom = self._dom_snapshot()
            if dom:
                observations.append(Observation(
                    source="dom",
                    scope_id=scope.id,
                    scope_label=scope.label,
                    account_no=scope.account_no,
                    payload=dom,
                    metadata={"url": self._safe_current_url()},
                ))
                extraction = extract_account_data(scope, observations)
                data = extraction.data
        logging.info(
            "Path B 当前页解析摘要: "
            f"account={'yes' if data.account.account_no else 'no'}, "
            f"balance={'yes' if data.balance else 'no'}, "
            f"monthly={len(data.monthly)}, daily={len(data.daily)}, "
            f"yearly={'yes' if data.yearly else 'no'}"
        )
        if self.diagnostic is not None:
            try:
                diagnostic_snapshot = (
                    self._snapshot(wide_debug=True)
                    if debug_enabled()
                    else snapshot
                )
                self.diagnostic.record_page(label, diagnostic_snapshot, data)
                self.diagnostic.record_observations(observations)
                self.diagnostic.record_decisions(extraction.decisions)
            except Exception as diag_error:
                logging.warning(f"SGCC DIAG 记录页面诊断失败，已忽略: {diag_error}")
        return data

    def _snapshot(self, *, wide_debug: bool = False) -> dict[str, Any]:
        """Capture a Vue snapshot.

        Readiness/account probes stay on the bounded field-aware component
        snapshot even when Debug is enabled.  The complete bounded ``$data``
        capture is intentionally reserved for the final parse point so a
        polling loop cannot repeatedly serialize the whole component tree.
        """
        store = {}
        try:
            if hasattr(vue_state, "selected_store_snapshot"):
                store = vue_state.selected_store_snapshot(self.driver) or {}
            else:
                store = {"state": vue_state.selected_store_state(self.driver)}
        except Exception as exc:
            store = {}
            logging.warning(f"Path B Vuex 快照失败，已降级: {type(exc).__name__}")
            if self.diagnostic is not None:
                self.diagnostic.record_error(exc, stage="snapshot:vuex")
        try:
            if wide_debug and debug_enabled():
                components = vue_state.selected_vue_debug_data(self.driver) or []
            else:
                components = vue_state.selected_vue_data(
                    self.driver,
                    include_diag_fields=False,
                ) or []
        except Exception as exc:
            components = []
            logging.warning(f"Path B Vue Component 快照失败，已降级: {type(exc).__name__}")
            if self.diagnostic is not None:
                self.diagnostic.record_error(exc, stage="snapshot:component")
        return {"store": store, "components": components, "url": self._safe_current_url()}

    def _observations_for_scope(
        self,
        scope: CaptureScope,
        snapshot: dict[str, Any],
    ) -> list[Observation]:
        observations: list[Observation] = []
        if self.network_recorder is not None:
            try:
                self.network_recorder.flush(scope_id=scope.id)
                observations.extend(self.network_recorder.observations(scope.id))
            except Exception:
                pass
        observations.append(Observation(
            source="vuex",
            scope_id=scope.id,
            scope_label=scope.label,
            account_no=scope.account_no,
            payload=snapshot.get("store") or {},
            metadata={"url": snapshot.get("url") or ""},
        ))
        observations.append(Observation(
            source="component",
            scope_id=scope.id,
            scope_label=scope.label,
            account_no=scope.account_no,
            payload=snapshot.get("components") or [],
            metadata={"url": snapshot.get("url") or ""},
        ))
        return observations

    def _dom_snapshot(self) -> list[dict[str, Any]]:
        try:
            return vue_state.dom_semantic_snapshot(self.driver) or []
        except Exception:
            return []

    def _begin_scope(self, label: str, account_no: str = "") -> CaptureScope:
        scope = CaptureScope.create(
            label,
            account_no=account_no,
            url=self._safe_current_url(),
        )
        self._active_scope = scope
        if self.network_recorder is not None:
            try:
                self.network_recorder.set_scope(scope)
            except Exception:
                pass
        if self.diagnostic is not None:
            self.diagnostic.record_timeline(
                "scope_started",
                scope_id=scope.id,
                label=label,
                account_no=account_no,
                url=scope.url,
            )
        return scope

    def _end_scope(self, scope: CaptureScope) -> None:
        if self.diagnostic is not None:
            self.diagnostic.record_timeline(
                "scope_finished",
                scope_id=scope.id,
                label=scope.label,
                account_no=scope.account_no,
                url=self._safe_current_url(),
            )
        if self.network_recorder is not None:
            try:
                self.network_recorder.set_scope(None)
            except Exception:
                pass
        if self._active_scope == scope:
            self._active_scope = None

    def _safe_current_url(self) -> str:
        try:
            return str(self.driver.current_url or "")
        except Exception:
            return ""

    def _navigate(self, url: str, label: str) -> None:
        target_path = url.split("/osgweb", 1)[-1]
        current_url = self.driver.current_url or ""
        previous_signature = self._business_signature(label) if target_path in current_url else None
        logging.info(f"Path B 导航到 {label}: {url}")
        self.driver.execute_script("window.location.href = arguments[0];", url)
        try:
            WebDriverWait(self.driver, self.wait_seconds).until(
                lambda d: target_path in (d.current_url or "")
                or d.execute_script("return document.readyState") in ("interactive", "complete")
            )
        except TimeoutException:
            logging.warning(f"Path B 等待 {label} URL/readyState 超时，继续尝试读取页面状态。")
        self._wait_for_business_ready(label, previous_signature)
        try:
            self.driver.execute_script("window.stop();")
        except Exception:
            pass
        logging.info(f"Path B {label} 页面当前 URL: {self.driver.current_url}")

    def _click_tab(self, tab_text: str) -> bool:
        xpaths = [
            f"//div[contains(@class,'el-tabs__item') and contains(normalize-space(.), '{tab_text}')]",
            f"//*[contains(normalize-space(.), '{tab_text}')]",
        ]
        with self._optional_probe():
            for xpath in xpaths:
                try:
                    element = WebDriverWait(self.driver, 4).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    previous_signature = self._business_signature(tab_text)
                    self.driver.execute_script("arguments[0].click();", element)
                    self._wait_for_business_ready(tab_text, previous_signature)
                    return True
                except Exception:
                    continue
        return False

    def _expand_daily_range_to_30_days(self, min_expected_count: int = 20) -> bool:
        """Best-effort switch from the default 7-day daily view to 近30天.

        SGCC defaults the 日用电量 tab to a short range.  Clicking 近30天
        updates Vue component state in-place; failures must not abort the whole
        scrape, because the current 7-day data is still better than no data.
        """
        try:
            before_count = self._current_daily_count()
            if before_count >= min_expected_count:
                logging.info(f"Path B 日用电量已包含 {before_count} 条，无需切换近30天。")
                return True

            if not self._click_daily_range("近30天"):
                logging.warning("Path B 未找到/无法点击日用电量近30天控件，保留当前日用电量数据。")
                return False

            expanded = self._wait_for_daily_range_expansion(before_count, min_expected_count)
            after_count = self._current_daily_count()
            if expanded:
                logging.info(f"Path B 日用电量近30天已生效: {before_count} -> {after_count} 条。")
                return True
            logging.warning(f"Path B 等待日用电量近30天数据超时: {before_count} -> {after_count} 条，保留当前数据。")
            return False
        except Exception as e:
            logging.warning(f"Path B 切换日用电量近30天失败，保留当前数据: {e}")
            return False

    def _click_daily_range(self, range_text: str) -> bool:
        xpaths = [
            f"//label[contains(@class,'el-radio-button') and contains(normalize-space(.), '{range_text}')]",
            f"//label[contains(@class,'el-radio') and contains(normalize-space(.), '{range_text}')]",
            f"//button[contains(normalize-space(.), '{range_text}')]",
            f"//span[contains(normalize-space(.), '{range_text}')]/ancestor::*[self::label or self::button or self::div[contains(@class,'el-radio')]][1]",
            f"//*[contains(normalize-space(.), '{range_text}') and string-length(normalize-space(.)) <= 20]",
        ]
        with self._optional_probe():
            for xpath in xpaths:
                try:
                    elements = self.driver.find_elements(By.XPATH, xpath)
                except Exception:
                    continue
                for element in elements:
                    try:
                        if not self._is_enabled_visible(element):
                            continue
                        self.driver.execute_script("arguments[0].click();", element)
                        return True
                    except Exception:
                        continue
        return False

    def _wait_for_daily_range_expansion(
        self,
        previous_count: int,
        min_expected_count: int = 20,
        timeout_seconds: Optional[float] = None,
        poll_frequency: float = 0.2,
    ) -> bool:
        timeout = self.wait_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        while True:
            count = self._current_daily_count()
            if self._daily_count_looks_expanded(count, previous_count, min_expected_count):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.0, poll_frequency))

    @staticmethod
    def _daily_count_looks_expanded(count: int, previous_count: int, min_expected_count: int = 20) -> bool:
        return count >= min_expected_count or (previous_count <= 7 and count > 7)

    def _current_daily_count(self) -> int:
        try:
            snapshot = self._snapshot()
            data = parse_account_data(store=snapshot.get("store"), components=snapshot.get("components"))
            return len(data.daily)
        except Exception:
            return 0

    def _wait_for_business_ready(self, label: str, previous_signature: Optional[str] = None) -> None:
        """Bounded replacement for the old fixed settle sleep.

        The upper bound is exactly ``settle_seconds``: if SGCC business data or
        the relevant tab/container is visible sooner, continue immediately; if
        not, WebDriverWait times out and preserves the old worst-case wait.
        """
        if self.settle_seconds <= 0:
            return
        try:
            WebDriverWait(self.driver, self.settle_seconds, poll_frequency=0.2).until(
                lambda d: self._business_ready_signal(d, label, previous_signature)
            )
        except TimeoutException:
            pass

    def _business_ready_signal(self, driver, label: str, previous_signature: Optional[str]) -> bool:
        signature = self._business_signature(label)
        if not signature:
            return False
        if previous_signature is not None and signature == previous_signature:
            return False
        return True

    def _business_signature(self, label: str) -> Optional[str]:
        snapshot = self._snapshot()
        components = snapshot.get("components") or []
        try:
            parsed: Optional[AccountData] = parse_account_data(store=snapshot.get("store"), components=components)
        except Exception:
            parsed = None

        if label == "账户余额":
            if parsed is None or parsed.balance is None:
                return None
            # ``observed_at`` is generated at snapshot time when SGCC omits a
            # business timestamp, so including the whole dataclass makes an
            # unchanged/stale balance look refreshed on every poll.  Only use
            # the actual amount fields when waiting for an account switch to
            # finish updating the balance component.
            return repr((
                parsed.balance.balance_cny,
                parsed.balance.prepay_balance_cny,
                parsed.balance.arrears_cny,
            ))
        if label == "电量电费查询":
            if self._has_visible_text("月度电费") or self._has_visible_text("日用电量"):
                return f"usage-tabs:{self.driver.current_url}"
            return None
        if label == "月度电费":
            if parsed is not None and parsed.monthly:
                return repr(parsed.monthly)
            if self._component_data_matches_label(components, label):
                return self._component_signature(components, label)
            return None
        if label == "日用电量":
            if parsed is not None and parsed.daily:
                return repr(parsed.daily)
            if self._component_data_matches_label(components, label):
                return self._component_signature(components, label)
            return None
        if self._component_data_matches_label(components, label):
            return self._component_signature(components, label)
        return None

    def _component_data_matches_label(self, components: list[dict[str, Any]], label: str) -> bool:
        values_by_key: dict[str, Any] = {}
        for component in components:
            data = component.get("data") if isinstance(component, dict) else None
            if isinstance(data, dict):
                values_by_key.update(data)

        if label == "账户余额":
            return self._has_business_payload(values_by_key, "mixinGetYuEdata", "consInfoobj", "consInfo")
        if label == "电量电费查询":
            return self._has_business_payload(
                values_by_key,
                "powerData",
                "mothData",
                "tableData",
                "billNumberList",
                "BillList",
                "billList",
                "activeName",
            )
        if label == "月度电费":
            return self._has_business_payload(values_by_key, "powerData", "mothData", "tableData", "BillList", "billList")
        if label == "日用电量":
            return self._has_business_payload(values_by_key, "sevenEleList", "sevenEleList_t", "new_sevenEleList", "tableData_t")
        return any(self._has_payload(value) for value in values_by_key.values())

    def _has_business_payload(self, values_by_key: dict[str, Any], *keys: str) -> bool:
        return any(self._has_payload(values_by_key.get(key)) for key in keys)

    def _component_signature(self, components: list[dict[str, Any]], label: str) -> str:
        values_by_key: dict[str, Any] = {}
        for component in components:
            data = component.get("data") if isinstance(component, dict) else None
            if isinstance(data, dict):
                values_by_key.update(data)
        if label == "月度电费":
            keys = ("powerData", "mothData", "tableData", "BillList", "billList")
        elif label == "日用电量":
            keys = ("sevenEleList", "sevenEleList_t", "new_sevenEleList", "tableData_t")
        elif label == "账户余额":
            keys = ("mixinGetYuEdata", "consInfoobj", "consInfo")
        else:
            keys = tuple(sorted(values_by_key))
        return repr({key: values_by_key.get(key) for key in keys if self._has_payload(values_by_key.get(key))})

    def _has_payload(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (int, float, bool)):
            return True
        if isinstance(value, list):
            return bool(value) and any(self._has_payload(item) for item in value)
        if isinstance(value, dict):
            return bool(value) and any(self._has_payload(item) for item in value.values())
        return True

    def _has_visible_text(self, text: str) -> bool:
        with self._optional_probe():
            elements = self.driver.find_elements(By.XPATH, f"//*[contains(normalize-space(.), '{text}')]")
        return any(self._is_displayed(element) for element in elements)

    def _is_displayed(self, element) -> bool:
        try:
            return element.is_displayed()
        except Exception:
            return False

    def _visible_account_options(self) -> list[AccountOption]:
        """Return visible selector options with stable Vue-backed identities."""
        with self._optional_probe():
            if not self._open_account_selector():
                self._selector_enumeration_failed = True
                return []
            time.sleep(1)
            options = self.driver.find_elements(
                By.XPATH,
                "//ul[contains(@class,'el-dropdown-menu')]//li"
                " | //div[contains(@class,'el-select-dropdown')]//li",
            )
        result: list[AccountOption] = []
        for option in options:
            try:
                klass = option.get_attribute("class") or ""
                if option.is_displayed() and "disabled" not in klass and "is-disabled" not in klass:
                    result.append(AccountOption(
                        index=len(result),
                        account_no=self._account_option_no(option),
                    ))
            except Exception:
                continue
        self._close_popups()
        return result

    def _select_account(self, account_no: str = "", fallback_index: Optional[int] = None) -> bool:
        current_url = self._safe_current_url()
        on_balance_page = BALANCE_URL.split("/osgweb", 1)[-1] in current_url
        previous_balance_signature = (
            self._business_signature("账户余额")
            if account_no and on_balance_page
            else None
        )
        with self._optional_probe():
            if not self._open_account_selector():
                return False
            time.sleep(1)
            options = [
                option
                for option in self.driver.find_elements(
                    By.XPATH,
                    "//ul[contains(@class,'el-dropdown-menu')]//li"
                    " | //div[contains(@class,'el-select-dropdown')]//li",
                )
                if self._is_enabled_visible(option)
            ]
        selected = None
        if account_no:
            selected = next(
                (option for option in options if self._account_option_no(option) == account_no),
                None,
            )
        elif fallback_index is not None and fallback_index < len(options):
            selected = options[fallback_index]

        if selected is None:
            self._close_popups()
            return False
        self.driver.execute_script("arguments[0].click();", selected)
        if not account_no:
            time.sleep(self.settle_seconds)
            return True
        if not self._wait_for_selected_account(account_no):
            return False
        if on_balance_page:
            # The selector identity changes before the asynchronously loaded
            # balance does.  Waiting for the amount payload to change prevents
            # the previous household's balance from being assigned to the new
            # account and gives SGCC time to persist the switch before the
            # scraper navigates to the usage route.
            self._wait_for_business_ready("账户余额", previous_balance_signature)
        return True

    def _account_option_no(self, option) -> str:
        try:
            value = self.driver.execute_script(
                """
                const vm = arguments[0].__vue__;
                const command = vm && vm.$props ? vm.$props.command : null;
                const values = [
                  vm && vm.$props ? vm.$props.value : null,
                  vm && vm._props ? vm._props.value : null,
                  command ? command.consNo_dst : null,
                  command ? command.consNoSrc : null,
                  command ? command.consNo : null,
                  arguments[0].getAttribute('value')
                ];
                for (const value of values) {
                  const text = String(value || '').trim();
                  if (/^\\d{13}$/.test(text)) return text;
                }
                return '';
                """,
                option,
            )
        except Exception:
            value = ""
        text = str(value or "").strip()
        return text if len(text) == 13 and text.isdigit() else ""

    def _wait_for_selected_account(self, expected_account_no: str) -> bool:
        timeout = max(1.0, self.settle_seconds)
        deadline = time.monotonic() + timeout
        while True:
            if self._current_account_no() == expected_account_no:
                return True
            if time.monotonic() >= deadline:
                self._close_popups()
                return False
            time.sleep(0.2)

    def _open_account_selector(self) -> bool:
        selectors = [
            (By.XPATH, "//span[contains(normalize-space(.), '切换用户')]"),
            (By.CSS_SELECTOR, ".houseNum .el-select .el-input__inner"),
            (By.CSS_SELECTOR, ".houseNum .el-select .el-input__suffix"),
            (By.CSS_SELECTOR, ".houseNum .el-select"),
        ]
        for by, value in selectors:
            try:
                elements = self.driver.find_elements(by, value)
                for element in elements:
                    if element.is_displayed():
                        self.driver.execute_script("arguments[0].click();", element)
                        return True
            except Exception:
                continue
        return False

    def _is_enabled_visible(self, element) -> bool:
        try:
            klass = element.get_attribute("class") or ""
            return element.is_displayed() and "disabled" not in klass and "is-disabled" not in klass
        except Exception:
            return False

    def _close_popups(self) -> None:
        try:
            with self._optional_probe():
                self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass

    @contextmanager
    def _optional_probe(self):
        """Avoid multiplying the global implicit wait for optional selectors."""
        original = None
        try:
            timeouts = getattr(self.driver, "timeouts", None)
            original = getattr(timeouts, "implicit_wait", None)
            self.driver.implicitly_wait(0)
        except Exception:
            original = None
        try:
            yield
        finally:
            if original is not None:
                try:
                    self.driver.implicitly_wait(original)
                except Exception:
                    pass


def redact_account_data(data: AccountData) -> dict[str, Any]:
    """Return a log-safe dict for live verification output."""
    raw = asdict(data)
    account_no = data.account.account_no
    masked = mask_account_no(account_no)
    raw["account"]["account_no"] = masked
    raw["account"]["display_name"] = ""
    raw["account"]["address"] = ""
    if raw.get("balance"):
        raw["balance"]["account_no"] = masked
    for key in ("yearly",):
        if raw.get(key):
            raw[key]["account_no"] = masked
    for key in ("monthly", "daily"):
        for row in raw.get(key) or []:
            row["account_no"] = masked
    return raw
