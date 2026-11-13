import unittest

from mango.authoring.go_lock import same_info_json


class GoInfoTests(unittest.TestCase):
    def test_object_order_only_normalization(self):
        proxy = b'{"Version":"v1.2.3","Origin":{"Ref":"tag","Hash":"abc"}}'
        cached = '{"Origin":{"Hash":"abc","Ref":"tag"},"Version":"v1.2.3"}'
        self.assertTrue(same_info_json(proxy, cached, "v1.2.3"))
        self.assertFalse(same_info_json(proxy, cached.replace("abc", "def"), "v1.2.3"))

    def test_duplicate_fields_and_nonfinite_numbers_rejected(self):
        self.assertFalse(
            same_info_json(
                b'{"Version":"wrong","Version":"v1.2.3"}',
                '{"Version":"v1.2.3"}',
                "v1.2.3",
            )
        )
        self.assertFalse(
            same_info_json(
                b'{"Version":"v1.2.3","extra":NaN}',
                '{"Version":"v1.2.3","extra":NaN}',
                "v1.2.3",
            )
        )

    def test_boolean_integer_and_float_are_not_conflated(self):
        for value in ("true", "1.0"):
            self.assertFalse(
                same_info_json(
                    b'{"Version":"v1.2.3","extra":1}',
                    '{"Version":"v1.2.3","extra":' + value + "}",
                    "v1.2.3",
                )
            )
