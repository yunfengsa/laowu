import json
import errno
import sqlite3
import unittest

from laowu.adapters import parse_event
from laowu.diagnostics import ErrorStream, classify_error, classify_exception


class DiagnosticTests(unittest.TestCase):
    def test_local_exception_codes_are_whitelisted_without_messages_or_paths(self):
        db_error = sqlite3.OperationalError('PRIVATE_DIAGNOSTIC')
        db_error.sqlite_errorcode = 5
        self.assertEqual(classify_exception(db_error),
                         {'category': 'database_busy', 'basis': 'structured', 'sqlite_code': 5})
        self.assertEqual(classify_exception(sqlite3.OperationalError('database is locked PRIVATE_DIAGNOSTIC')),
                         {'category': 'database_busy', 'basis': 'message_pattern'})
        self.assertEqual(classify_exception(OSError(errno.EROFS, 'PRIVATE_DIAGNOSTIC', 'PRIVATE_PATH')),
                         {'category': 'filesystem_read_only', 'basis': 'structured', 'os_errno': errno.EROFS})
        self.assertEqual(classify_exception(RuntimeError('PRIVATE_DIAGNOSTIC')),
                         {'category': 'unknown', 'basis': 'unclassified'})

    def test_exec_message_only_errors_are_classified_without_text(self):
        samples = (
            ("unexpected status 401 Unauthorized", "authentication"),
            ("429 Too Many Requests", "rate_limit"),
            ("You have hit your usage limit", "usage_limit"),
            ("stream disconnected before completion: error sending request", "stream"),
            ("error sending request: dns error", "network"),
            ("Read-only file system (os error 30)", "filesystem_read_only"),
            ("Permission denied (os error 13)", "permission"),
            ("503 Service Unavailable", "server"),
            ("unknown field in configuration", "configuration"),
        )
        for message, category in samples:
            with self.subTest(category=category):
                event = parse_event("codex", json.dumps({"type": "turn.failed", "error": {
                    "message": message + " Authorization: Bearer TEST_PRIVATE_VALUE https://private.invalid/path"}}))
                self.assertEqual(event["error"], "client_" + category)
                self.assertTrue(event["terminal"])
                self.assertEqual(event["diagnostic"], {"category": category, "basis": "message_pattern"})
                self.assertNotIn("PRIVATE", json.dumps(event))
                self.assertNotIn("private.invalid", json.dumps(event))

    def test_structured_error_overrides_untrusted_message_and_rejects_extra_fields(self):
        diagnostic = classify_error({"error": {
            "message": "401 token=PRIVATE", "code": "PRIVATE", "request_id": "PRIVATE",
            "codexErrorInfo": {"HttpConnectionFailed": {"httpStatusCode": 503, "url": "PRIVATE"}},
        }})
        self.assertEqual(diagnostic, {"category": "server", "basis": "structured", "http_status": 503,
                                      "variant": "HttpConnectionFailed"})
        for status in (True, "401", -1, 600, {"secret": "PRIVATE"}):
            self.assertEqual(classify_error({"error": {"httpStatusCode": status, "message": "opaque",
                                                      "codexErrorInfo": "PRIVATE"}}),
                             {"category": "unknown", "basis": "unclassified"})

    def test_known_variant_spellings_are_canonicalized_to_allowlist(self):
        for variant in ("HttpConnectionFailed", "httpConnectionFailed", "http_connection_failed"):
            diagnostic = classify_error({"error": {"codex_error_info": {
                variant: {"http_status_code": 429, "secret": "PRIVATE"}}}})
            self.assertEqual(diagnostic, {"category": "rate_limit", "basis": "structured",
                                         "http_status": 429, "variant": "HttpConnectionFailed"})

    def test_unknown_error_never_echoes_opaque_message_or_codes(self):
        event = parse_event("codex", json.dumps({"type": "error", "message": "PRIVATE", "code": "PRIVATE"}))
        self.assertEqual(event["error"], "client_error")
        self.assertFalse(event["terminal"])
        self.assertEqual(event["diagnostic"], {"category": "unknown", "basis": "unclassified"})
        self.assertNotIn("PRIVATE", json.dumps(event))

    def test_stderr_classifies_across_chunks_and_bounds_retained_bytes(self):
        stream = ErrorStream()
        chunks = [b"PRIVATE " * 20000 + b"Read-only file sys", b"tem (os error 30)\n", b"opaque " * 20000]
        for chunk in chunks:
            stream.feed(chunk)
        self.assertEqual(stream.diagnostic, {"category": "filesystem_read_only", "basis": "message_pattern"})
        self.assertEqual(stream.byte_count, sum(map(len, chunks)))
        self.assertLessEqual(len(stream._tail), 256)
        self.assertNotIn("PRIVATE", json.dumps(stream.diagnostic))
