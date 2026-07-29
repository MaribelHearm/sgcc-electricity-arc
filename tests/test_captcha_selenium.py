import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from sgcc_ha_bridge.captcha_selenium import (
    TENCENT_SELECTORS,
    _extract_main_url,
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


if __name__ == "__main__":
    unittest.main()
