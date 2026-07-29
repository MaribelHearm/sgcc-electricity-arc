import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from sgcc_ha_bridge.captcha_selenium import (
    TENCENT_SELECTORS,
    _extract_click_reference_text,
    _extract_main_url,
    _extract_reference,
    _find_visible_element,
    _wait_for_captcha,
    has_captcha_in_browser,
)


class CaptchaVisibilityTestCase(unittest.TestCase):
    def test_visible_widget_is_detected(self):
        driver = Mock()
        def execute(script, selectors):
            self.assertIn("rect.top < viewportHeight", script)
            self.assertIn("rect.left < viewportWidth", script)
            return True

        driver.execute_script.side_effect = execute

        self.assertTrue(has_captcha_in_browser(driver))

    def test_detection_failure_is_treated_as_no_widget(self):
        driver = Mock()
        driver.execute_script.side_effect = RuntimeError("page changed")

        self.assertFalse(has_captcha_in_browser(driver))

    def test_offscreen_widget_is_not_treated_as_visible(self):
        driver = Mock()
        driver.execute_script.return_value = False

        self.assertFalse(has_captcha_in_browser(driver))

    @patch(
        "sgcc_ha_bridge.captcha_selenium.has_captcha_in_browser",
        side_effect=[False, True],
    )
    @patch("sgcc_ha_bridge.captcha_selenium.WebDriverWait")
    def test_wait_for_captcha_waits_for_visible_widget(self, wait, has_captcha):
        def until(predicate):
            self.assertFalse(predicate(Mock()))
            return predicate(Mock())

        wait.return_value.until.side_effect = until

        self.assertTrue(_wait_for_captcha(Mock(), TENCENT_SELECTORS, 1))
        self.assertEqual(has_captcha.call_count, 2)

    def test_main_image_uses_rendered_element_screenshot(self):
        raw = io.BytesIO()
        Image.new("RGB", (320, 180), "white").save(raw, format="PNG")

        element = Mock()
        element.screenshot_as_png = raw.getvalue()
        driver = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.return_value = True

        source, size = _extract_main_url(driver, TENCENT_SELECTORS)

        self.assertTrue(source.startswith("data:image/png;base64,"))
        self.assertEqual(size, (320, 180))

    @patch("sgcc_ha_bridge.captcha_selenium.WebDriverWait")
    def test_text_click_reference_does_not_require_header_image(self, wait):
        header_text = Mock()
        header_text.text = "请依次点击：爱 诧 畅"

        def until(predicate):
            result = predicate(driver)
            if result:
                return result
            raise RuntimeError("not found")

        wait.return_value.until.side_effect = until
        driver = Mock()
        driver.timeouts.implicit_wait = 60

        def find_elements(_, selector):
            if selector == TENCENT_SELECTORS["header_text"]:
                return [header_text]
            return []

        driver.find_elements.side_effect = find_elements
        driver.execute_script.return_value = True

        source, reference_text = _extract_reference(driver, TENCENT_SELECTORS)

        self.assertIsNone(source)
        self.assertEqual(reference_text, "爱 诧 畅")
        self.assertIn(unittest.mock.call(0), driver.implicitly_wait.mock_calls)
        self.assertIn(unittest.mock.call(60.0), driver.implicitly_wait.mock_calls)

    def test_extract_text_click_targets(self):
        self.assertEqual(
            _extract_click_reference_text("请依次点击：爱 诧 畅"),
            "爱 诧 畅",
        )
        self.assertIsNone(_extract_click_reference_text("请依次点击："))

    @patch("sgcc_ha_bridge.captcha_selenium.WebDriverWait")
    def test_visible_lookup_temporarily_disables_implicit_wait(self, wait):
        driver = Mock()
        driver.timeouts.implicit_wait = 60
        element = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.return_value = True
        wait.return_value.until.side_effect = lambda predicate: predicate(driver)

        self.assertIs(_find_visible_element(driver, ".target"), element)
        self.assertEqual(
            driver.implicitly_wait.mock_calls,
            [unittest.mock.call(0), unittest.mock.call(60.0)],
        )


if __name__ == "__main__":
    unittest.main()
