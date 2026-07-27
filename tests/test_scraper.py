import importlib.util
import os
import sys
import time
import types
import unittest
from unittest.mock import Mock, call, patch


def _install_selenium_stub_if_missing():
    if importlib.util.find_spec("selenium") is not None:
        return

    class TimeoutException(Exception):
        pass

    class By:
        XPATH = "xpath"
        CSS_SELECTOR = "css selector"
        CLASS_NAME = "class name"
        TAG_NAME = "tag name"

    class Keys:
        ESCAPE = "\ue00c"

    class WebDriverWait:
        def __init__(self, driver, timeout, poll_frequency=0.5):
            self.driver = driver
            self.timeout = timeout
            self.poll_frequency = poll_frequency

        def until(self, method):
            deadline = time.monotonic() + max(0, self.timeout)
            while True:
                value = method(self.driver)
                if value:
                    return value
                if time.monotonic() >= deadline:
                    raise TimeoutException()
                time.sleep(max(0, self.poll_frequency))

    def element_to_be_clickable(locator):
        def predicate(driver):
            elements = driver.find_elements(*locator)
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
            return False
        return predicate

    module_names = [
        "selenium",
        "selenium.common",
        "selenium.common.exceptions",
        "selenium.webdriver",
        "selenium.webdriver.common",
        "selenium.webdriver.common.by",
        "selenium.webdriver.common.keys",
        "selenium.webdriver.support",
        "selenium.webdriver.support.expected_conditions",
        "selenium.webdriver.support.ui",
    ]
    for name in module_names:
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["selenium.common.exceptions"].TimeoutException = TimeoutException
    sys.modules["selenium.webdriver.common.by"].By = By
    sys.modules["selenium.webdriver.common.keys"].Keys = Keys
    sys.modules["selenium.webdriver.support.expected_conditions"].element_to_be_clickable = element_to_be_clickable
    sys.modules["selenium.webdriver.support.ui"].WebDriverWait = WebDriverWait


_install_selenium_stub_if_missing()

from sgcc_ha_bridge.model import Account, AccountData
from sgcc_ha_bridge.observation import CaptureScope
from sgcc_ha_bridge.scraper import AccountOption, Scraper


class FetchAllAccountEnumerationTestCase(unittest.TestCase):
    def test_fetch_all_uses_stable_option_identity_and_adds_current_account(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        calls = []
        scraper._navigate = lambda url, label: None
        scraper._visible_account_options = lambda: [
            AccountOption(index=0, account_no="1234567891703"),
            AccountOption(index=1, account_no="1234567891704"),
        ]

        def fake_fetch_account(selection=None):
            calls.append(selection)
            account_no = {
                None: "1234567891705",
                "1234567891703": "1234567891703",
                "1234567891704": "1234567891704",
            }[selection.account_no if selection else None]
            return AccountData(account=Account(account_no=account_no))

        scraper._fetch_account = fake_fetch_account

        results = scraper.fetch_all()

        self.assertEqual(calls, [
            None,
            AccountOption(index=0, account_no="1234567891703"),
            AccountOption(index=1, account_no="1234567891704"),
        ])
        self.assertEqual([item.account.account_no for item in results], [
            "1234567891705",
            "1234567891703",
            "1234567891704",
        ])

    def test_fetch_all_deduplicates_current_account_when_selector_includes_it(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        calls = []
        scraper._navigate = lambda url, label: None
        scraper._visible_account_options = lambda: [
            AccountOption(index=0, account_no="1234567891705"),
            AccountOption(index=1, account_no="1234567891703"),
        ]

        def fake_fetch_account(selection=None):
            calls.append(selection)
            account_no = {
                None: "1234567891705",
                "1234567891703": "1234567891703",
            }[selection.account_no if selection else None]
            return AccountData(account=Account(account_no=account_no))

        scraper._fetch_account = fake_fetch_account

        results = scraper.fetch_all()

        self.assertEqual([item.account.account_no for item in results], [
            "1234567891705",
            "1234567891703",
        ])
        self.assertEqual(calls, [
            None,
            AccountOption(index=1, account_no="1234567891703"),
        ])

    def test_visible_account_options_reads_vue_option_values(self):
        class FakeElement:
            def __init__(self, text, account_no):
                self.text = text
                self.account_no = account_no

            def get_attribute(self, name):
                return ""

            def is_displayed(self):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [
                    FakeElement("民用", "1234567891703"),
                    FakeElement("民用", "1234567891704"),
                ]

            def find_element(self, by, value):
                raise Exception("no body")

            def execute_script(self, script, element):
                return element.account_no

        scraper = Scraper(driver=FakeDriver(), wait_seconds=1, settle_seconds=0)
        scraper._open_account_selector = lambda: True

        self.assertEqual(scraper._visible_account_options(), [
            AccountOption(index=0, account_no="1234567891703"),
            AccountOption(index=1, account_no="1234567891704"),
        ])

    def test_fetch_selected_account_reselects_by_account_number_when_route_changes(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        selected = []
        scraper._navigate = lambda url, label: None
        scraper._select_account = lambda account_no="", fallback_index=None: selected.append(
            (account_no, fallback_index)
        ) or True
        scraper._click_tab = lambda text: True
        scraper._expand_daily_range_to_30_days = lambda: True
        current_accounts = iter([
            "1234567891704",
        ])
        scraper._current_account_no = lambda: next(current_accounts)
        snapshots = iter([
            AccountData(account=Account(account_no="1234567891703")),
            AccountData(account=Account(account_no="1234567891703")),
            AccountData(account=Account(account_no="1234567891703")),
        ])
        scraper._parse_current_page = lambda *args, **kwargs: next(snapshots)

        data = scraper._fetch_selected_account(
            AccountOption(index=1, account_no="1234567891703")
        )

        self.assertEqual(data.account.account_no, "1234567891703")
        self.assertEqual(selected, [
            ("1234567891703", 1),
            ("1234567891703", None),
        ])

    def test_fetch_account_skips_cross_route_identity_mismatch(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        scraper._navigate = lambda url, label: None
        scraper._select_account = lambda account_no="", fallback_index=None: True
        scraper._click_tab = lambda text: True
        scraper._expand_daily_range_to_30_days = lambda: True
        scraper._current_account_no = lambda: "1234567899314"
        snapshots = iter([
            AccountData(account=Account(account_no="1234567899314")),
            AccountData(account=Account(account_no="1234567897325")),
        ])
        scraper._parse_current_page = lambda *args, **kwargs: next(snapshots)

        data = scraper._fetch_current_account()

        self.assertIsNone(data)

    def test_fetch_all_marks_partial_selector_failures_non_authoritative(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        scraper._navigate = lambda url, label: None
        scraper._visible_account_options = lambda: [
            AccountOption(index=0, account_no="1234567897325"),
        ]
        scraper._fetch_current_account = lambda: AccountData(
            account=Account(account_no="1234567899314")
        )
        scraper._fetch_selected_account = lambda option: None

        results = scraper.fetch_all()

        self.assertEqual(
            [item.account.account_no for item in results],
            ["1234567899314"],
        )
        self.assertFalse(scraper.account_set_authoritative)

    def test_fetch_all_marks_selector_open_failure_non_authoritative(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        scraper._navigate = lambda url, label: None
        scraper._open_account_selector = lambda: False
        scraper._fetch_current_account = lambda: AccountData(
            account=Account(account_no="1234567899314")
        )

        results = scraper.fetch_all()

        self.assertEqual(len(results), 1)
        self.assertFalse(scraper.account_set_authoritative)


class DailyRangeWaitTestCase(unittest.TestCase):
    def test_daily_count_looks_expanded_accepts_30_day_or_more_than_7(self):
        self.assertTrue(Scraper._daily_count_looks_expanded(31, previous_count=7))
        self.assertTrue(Scraper._daily_count_looks_expanded(8, previous_count=7))
        self.assertFalse(Scraper._daily_count_looks_expanded(7, previous_count=7))
        self.assertFalse(Scraper._daily_count_looks_expanded(8, previous_count=8))

    def test_wait_for_daily_range_expansion_polls_until_count_grows(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        counts = iter([7, 7, 31])
        scraper._current_daily_count = lambda: next(counts)

        self.assertTrue(
            scraper._wait_for_daily_range_expansion(
                previous_count=7,
                min_expected_count=20,
                timeout_seconds=1,
                poll_frequency=0,
            )
        )

    def test_expand_daily_range_is_best_effort_when_button_missing(self):
        scraper = Scraper(driver=object(), wait_seconds=1, settle_seconds=0)
        scraper._current_daily_count = lambda: 7
        scraper._click_daily_range = lambda text: False

        self.assertFalse(scraper._expand_daily_range_to_30_days())


class AccountSwitchRefreshTestCase(unittest.TestCase):
    def test_balance_signature_ignores_synthetic_observed_at(self):
        class FakeDriver:
            current_url = "https://95598.cn/osgweb/userAcc"

        scraper = Scraper(driver=FakeDriver(), wait_seconds=1, settle_seconds=0)
        scraper._snapshot = Mock(side_effect=[
            {
                "store": {
                    "state": {
                        "balance": {
                            "consNo": "1234567890698",
                            "accountBalance": "21.0",
                        }
                    }
                },
                "components": [],
            },
            {
                "store": {
                    "state": {
                        "balance": {
                            "consNo": "1234567890698",
                            "accountBalance": "21.0",
                        }
                    }
                },
                "components": [],
            },
        ])

        first = scraper._business_signature("账户余额")
        second = scraper._business_signature("账户余额")

        self.assertEqual(first, "(21.0, None, None)")
        self.assertEqual(second, first)

    def test_select_account_waits_for_balance_payload_refresh(self):
        class FakeElement:
            def get_attribute(self, name):
                return ""

            def is_displayed(self):
                return True

        class FakeDriver:
            current_url = "https://95598.cn/osgweb/userAcc"

            def find_elements(self, by, value):
                return [FakeElement()]

            def execute_script(self, script, element):
                return None

            def implicitly_wait(self, value):
                return None

        scraper = Scraper(driver=FakeDriver(), wait_seconds=1, settle_seconds=0)
        scraper._open_account_selector = Mock(return_value=True)
        scraper._account_option_no = Mock(return_value="1234567892485")
        scraper._wait_for_selected_account = Mock(return_value=True)
        scraper._business_signature = Mock(return_value="(21.0, 21.0, 0.0)")
        scraper._wait_for_business_ready = Mock()

        with patch("sgcc_ha_bridge.scraper.time.sleep", return_value=None):
            selected = scraper._select_account(account_no="1234567892485")

        self.assertTrue(selected)
        scraper._wait_for_business_ready.assert_called_once_with(
            "账户余额",
            "(21.0, 21.0, 0.0)",
        )


class SnapshotModeTestCase(unittest.TestCase):
    def test_debug_full_snapshot_is_recorded_but_not_parsed(self):
        class FakeDriver:
            current_url = "https://95598.cn/osgweb/userAcc"

        diagnostic = Mock()
        scraper = Scraper(
            driver=FakeDriver(),
            diagnostic=diagnostic,
            wait_seconds=1,
            settle_seconds=0,
        )
        production = {"store": {}, "components": [], "url": FakeDriver.current_url}
        debug_only = {
            "store": {},
            "components": [{
                "data": {
                    "unknownProvinceWrapper": {
                        "consNo": "1234567890123",
                        "accountBalance": "66.6",
                    }
                }
            }],
            "url": FakeDriver.current_url,
        }
        scraper._snapshot = Mock(side_effect=[production, debug_only])
        scraper._dom_snapshot = Mock(return_value=[])

        with patch.dict(os.environ, {"SGCC_DEBUG": "true"}):
            data = scraper._parse_current_page(
                "账户余额",
                CaptureScope.create("账户余额", "1234567890123"),
            )

        self.assertIsNone(data.balance)
        self.assertEqual(
            scraper._snapshot.call_args_list,
            [call(wide_debug=False), call(wide_debug=True)],
        )
        diagnostic.record_page.assert_called_once()

    def test_debug_readiness_probe_uses_light_snapshot(self):
        class FakeDriver:
            current_url = "https://95598.cn/osgweb/electricityCharge"

        scraper = Scraper(driver=FakeDriver(), wait_seconds=1, settle_seconds=0)
        with (
            patch.dict(os.environ, {"SGCC_DEBUG": "true"}),
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_store_snapshot",
                return_value={"state": {}},
            ),
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_vue_data",
                return_value=[],
            ) as light,
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_vue_debug_data",
                return_value=[],
            ) as wide,
        ):
            scraper._snapshot()

        light.assert_called_once_with(scraper.driver, include_diag_fields=False)
        wide.assert_not_called()

    def test_debug_parse_snapshot_uses_complete_bounded_component_data(self):
        class FakeDriver:
            current_url = "https://95598.cn/osgweb/electricityCharge"

        scraper = Scraper(driver=FakeDriver(), wait_seconds=1, settle_seconds=0)
        with (
            patch.dict(os.environ, {"SGCC_DEBUG": "true"}),
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_store_snapshot",
                return_value={"state": {}},
            ),
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_vue_data",
                return_value=[],
            ) as light,
            patch(
                "sgcc_ha_bridge.scraper.vue_state.selected_vue_debug_data",
                return_value=[],
            ) as wide,
        ):
            scraper._snapshot(wide_debug=True)

        wide.assert_called_once_with(scraper.driver)
        light.assert_not_called()


if __name__ == "__main__":
    unittest.main()
