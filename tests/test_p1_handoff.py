"""Bounded parsing of untrusted worker handoffs; no client or service calls."""
import json
import tracemalloc
import unittest
from unittest.mock import patch

from laowu.worker import handoff


class HandoffTests(unittest.TestCase):
    @staticmethod
    def payload(text="done"):
        return dict(result=text, artifacts=["artifact"], validation=["checked"],
                    unfinished=[], memory_candidates=[{"content": "fact"}])

    def test_plain_fenced_and_prose_compatibility(self):
        payload = self.payload("结果 { 花括号 } \\\" and newline\n")
        encoded = json.dumps(payload, ensure_ascii=False)
        for text in (encoded, " \n\t" + encoded + "\n", "\u3000" + encoded + "\u3000",
                     "```json\n" + encoded + "\n```",
                     "交接如下：\n" + encoded, encoded + "\n尾注",
                     '{"other": "object"}\n' + encoded):
            with self.subTest(text=text[:30]):
                self.assertEqual(handoff(text), payload)

    def test_schema_and_non_json_numbers_still_rejected(self):
        valid = self.payload()
        invalid = [None, [], "text", {**valid, "result": 1},
                   {**valid, "artifacts": "file"},
                   {**valid, "validation": [1]},
                   {**valid, "unfinished": [None]},
                   {**valid, "memory_candidates": ["fact"]}]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, "invalid_handoff"):
                    handoff(json.dumps(data))
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    handoff(json.dumps({**valid, "extra": number}))

    def test_nested_handoff_and_extra_fields_preserve_schema(self):
        payload = self.payload()
        self.assertEqual(handoff(json.dumps({"wrapper": payload})), payload)
        self.assertEqual(handoff(json.dumps({**payload, "extra": "ignored"})), payload)

    def test_recovery_limit_has_an_explicit_candidate_boundary(self):
        payload = self.payload()
        encoded = json.dumps(payload)
        self.assertEqual(handoff("prose " + "{}" * 63 + encoded), payload)
        with self.assertRaisesRegex(ValueError, "invalid_handoff"):
            handoff("prose " + "{}" * 64 + encoded)

    def test_recovery_uses_original_string_and_bounded_attempts(self):
        original = "{" * 32768
        decoder = json.JSONDecoder()
        calls = []

        class TrackedDecoder:
            def raw_decode(self, text, idx=0):
                self_test.assertIs(text, original)
                calls.append(idx)
                return decoder.raw_decode(text, idx)

        self_test = self
        with patch("laowu.worker.json.JSONDecoder", return_value=TrackedDecoder()):
            with self.assertRaisesRegex(ValueError, "invalid_handoff"):
                handoff(original)
        self.assertLessEqual(len(calls), 65)
        self.assertEqual(len(calls), len(set(calls)))

    def test_deep_invalid_json_is_rejected_with_a_bounded_parse(self):
        with self.assertRaisesRegex(ValueError, "invalid_handoff"):
            handoff('{"nested":' * 4000)

    def test_large_schema_invalid_nested_objects_have_bounded_attempts(self):
        text = '{"wrapper":' * 100 + json.dumps("x" * 32768) + "}" * 100
        decoder = json.JSONDecoder()
        with patch("laowu.worker.json.JSONDecoder", return_value=decoder), \
                patch.object(decoder, "raw_decode", wraps=decoder.raw_decode) as decode:
            with self.assertRaisesRegex(ValueError, "invalid_handoff"):
                handoff(text)
        self.assertLessEqual(decode.call_count, 65)

    def peak_bytes(self, text, expected=None):
        tracemalloc.start()
        try:
            if expected is not None:
                self.assertEqual(handoff(text)["result"], expected)
            else:
                with self.assertRaisesRegex(ValueError, "invalid_handoff"):
                    handoff(text)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_dense_braces_memory_stays_bounded_at_16k_and_32k(self):
        for valid in (False, True):
            with self.subTest(valid=valid):
                if valid:
                    small_text, large_text = "{" * 16384, "{" * 32768
                    small = self.peak_bytes(json.dumps(self.payload(small_text)), small_text)
                    large = self.peak_bytes(json.dumps(self.payload(large_text)), large_text)
                else:
                    small = self.peak_bytes("{" * 16384)
                    large = self.peak_bytes("{" * 32768)
                self.assertLess(small, 512 * 1024)
                self.assertLess(large, 512 * 1024)
                self.assertLess(large, small * 3 + 32768)

    def test_normal_handoff_memory_grows_linearly_at_16k_and_32k(self):
        small = self.peak_bytes(json.dumps(self.payload("x" * 16384)), "x" * 16384)
        large = self.peak_bytes(json.dumps(self.payload("x" * 32768)), "x" * 32768)
        self.assertLess(small, 512 * 1024)
        self.assertLess(large, 512 * 1024)
        self.assertLess(large, small * 3 + 32768)


if __name__ == "__main__":
    unittest.main()
