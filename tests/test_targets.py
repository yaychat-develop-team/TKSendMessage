import unittest

from tiktok_live.errors import SafeOperationError
from tiktok_live.targets import parse_targets


class ParseTargetsTests(unittest.TestCase):
    def test_normalizes_and_deduplicates_targets_in_first_seen_order(self):
        self.assertEqual(
            parse_targets(" @Alpha ; beta;;alpha；@GAMMA "),
            ("Alpha", "beta", "GAMMA"),
        )

    def test_rejects_invalid_target_lists_with_safe_error(self):
        invalid_values = (
            "",
            ";;;",
            "../x",
            "https://example.com/@alpha",
            "alpha beta",
            ".",
            "..",
            "alpha;bad/name",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(SafeOperationError) as caught:
                    parse_targets(value)
                self.assertEqual(caught.exception.classification, "invalid_targets")
                if value:
                    self.assertNotIn(value, str(caught.exception))

    def test_safe_error_explains_username_and_semicolon_input(self):
        value = "private-invalid-value"
        with self.assertRaises(SafeOperationError) as caught:
            parse_targets(value)

        message = str(caught.exception)
        self.assertIn("用户名", message)
        self.assertIn("分号", message)
        self.assertNotIn(value, message)


if __name__ == "__main__":
    unittest.main()
