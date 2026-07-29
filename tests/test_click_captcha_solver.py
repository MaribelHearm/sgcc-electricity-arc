import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from sgcc_ha_bridge.click_captcha_solver import ClickCaptchaSolver


class ClickCaptchaSolverTextReferenceTestCase(unittest.TestCase):
    def test_graphic_reference_keeps_single_model_call(self):
        ref = io.BytesIO()
        Image.new("RGB", (90, 30), "white").save(ref, format="PNG")
        main = io.BytesIO()
        Image.new("RGB", (330, 236), "white").save(main, format="PNG")

        solver = ClickCaptchaSolver(api_key="test")
        solver._download = Mock(side_effect=[ref.getvalue(), main.getvalue()])
        solver._find_all_icons = Mock(return_value=[(10, 20), (30, 40), (50, 60)])

        coords = solver.solve(
            "data:image/png;base64,ref",
            "data:image/png;base64,main",
            330,
            236,
        )

        self.assertEqual(coords, [(10, 20), (30, 40), (50, 60)])
        self.assertEqual(solver._find_all_icons.call_count, 1)
        self.assertNotIn(
            "reference_text",
            solver._find_all_icons.call_args.kwargs,
        )

    def test_text_reference_skips_reference_image_download(self):
        main = io.BytesIO()
        Image.new("RGB", (330, 236), "white").save(main, format="PNG")

        solver = ClickCaptchaSolver(api_key="test")
        solver._download = Mock(return_value=main.getvalue())
        solver._find_all_icons = Mock(return_value=[(10, 20), (30, 40), (50, 60)])

        coords = solver.solve(
            None,
            "data:image/png;base64,main",
            330,
            236,
            reference_text="爱 诧 畅",
        )

        self.assertEqual(coords, [(10, 20), (30, 40), (50, 60)])
        solver._download.assert_called_once_with("data:image/png;base64,main")
        self.assertEqual(
            solver._find_all_icons.call_args.kwargs["reference_text"],
            ["爱", "诧", "畅"],
        )
        self.assertEqual(solver._find_all_icons.call_count, 3)

    def test_text_reference_consensus_ignores_one_outlier(self):
        samples = [
            [(100, 100), (200, 100), (300, 100)],
            [(102, 98), (201, 102), (298, 101)],
            [(20, 20), (40, 40), (60, 60)],
        ]

        self.assertEqual(
            ClickCaptchaSolver._select_consensus_coordinates(samples, 330, 236),
            [(101, 99), (200, 101), (299, 100)],
        )

    def test_text_reference_without_consensus_is_rejected(self):
        samples = [
            [(20, 20), (40, 40), (60, 60)],
            [(100, 100), (120, 120), (140, 140)],
            [(200, 200), (220, 200), (240, 200)],
        ]

        self.assertEqual(
            ClickCaptchaSolver._select_consensus_coordinates(samples, 330, 236),
            [],
        )

    def test_text_coordinates_are_refined_to_colored_glyph_centers(self):
        image = Image.new("RGB", (330, 236), (120, 170, 200))
        draw = ImageDraw.Draw(image)
        glyphs = [
            (100, 60, 144, 90),
            (247, 158, 292, 188),
            (197, 60, 243, 91),
        ]
        for box in glyphs:
            draw.rectangle(box, fill=(255, 180, 0))
        raw = io.BytesIO()
        image.save(raw, format="PNG")

        refined = ClickCaptchaSolver._refine_text_coordinates(
            raw.getvalue(),
            [(119, 76), (271, 144), (214, 75)],
        )

        self.assertEqual(refined, [(122, 75), (270, 173), (220, 76)])

    @patch("sgcc_ha_bridge.click_captcha_solver.OpenAI")
    def test_text_reference_is_sent_in_order(self, openai):
        response = Mock()
        response.choices = [Mock(message=Mock(content='{"coords":[[0.1,0.2],[0.3,0.4],[0.5,0.6]]}'))]
        openai.return_value.chat.completions.create.return_value = response

        solver = ClickCaptchaSolver(api_key="test")
        coords = solver._find_all_icons(
            [],
            "data:image/png;base64,main",
            300,
            200,
            reference_text=["爱", "诧", "畅"],
        )

        self.assertEqual(coords, [(30, 40), (90, 80), (150, 120)])
        request = openai.return_value.chat.completions.create.call_args.kwargs
        prompt = request["messages"][1]["content"][-1]["text"]
        self.assertLess(prompt.index("“爱”"), prompt.index("“诧”"))
        self.assertLess(prompt.index("“诧”"), prompt.index("“畅”"))


if __name__ == "__main__":
    unittest.main()
