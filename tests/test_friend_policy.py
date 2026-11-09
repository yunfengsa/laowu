"""Local policy reads reject links, special files and unbounded input."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from laowu.friend_policy import MAX_POLICY_BYTES, policy


class FriendPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'project'
        self.root.mkdir()
        self.config = self.root / 'config'
        self.path = self.config / 'friend-policy.json'

    def test_missing_directory_or_policy_defaults_closed(self):
        self.assertEqual(policy(self.root), {'enabled': False, 'templates': {}})
        self.config.mkdir()
        self.assertEqual(policy(self.root), {'enabled': False, 'templates': {}})

    def test_regular_policy_at_limit_and_oversized_policy(self):
        self.config.mkdir()
        data = json.dumps({'enabled': True, 'templates': {}}).encode()
        self.path.write_bytes(data.ljust(MAX_POLICY_BYTES, b' '))
        self.assertEqual(policy(self.root), {'enabled': True, 'templates': {}})
        self.path.write_bytes(data.ljust(MAX_POLICY_BYTES + 1, b' '))
        with self.assertRaisesRegex(ValueError, '过大'):
            policy(self.root)

    def test_null_or_invalid_json_does_not_count_as_missing_policy(self):
        self.config.mkdir()
        for content in ('null', '[]', '{}', '{'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ValueError):
                    policy(self.root)

    def test_policy_growth_after_stat_still_has_a_bounded_read(self):
        self.config.mkdir()
        data = b'{"enabled":true,"templates":{}}'
        self.path.write_bytes(data.ljust(MAX_POLICY_BYTES + 1, b' '))
        # Simulate a file growing between fstat and read; the second bound must
        # reject it even when the original reported size was within the limit.
        initial = SimpleNamespace(st_mode=self.path.stat().st_mode, st_size=len(data))
        with patch('laowu.friend_policy.os.fstat', return_value=initial):
            with self.assertRaisesRegex(ValueError, '过大'):
                policy(self.root)

    def test_policy_symlink_rejected_including_dangling_target(self):
        self.config.mkdir()
        target = self.root.parent / 'external-policy.json'
        self.path.symlink_to(target)
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    target.write_text('{"enabled":true,"templates":{}}')
                with self.assertRaises(ValueError):
                    policy(self.root)

    def test_config_symlink_rejected_even_when_policy_is_missing(self):
        target = self.root.parent / 'external-config'
        self.config.symlink_to(target, target_is_directory=True)
        for state in ('dangling', 'empty', 'with-policy'):
            with self.subTest(state=state):
                if state == 'empty':
                    target.mkdir()
                if state == 'with-policy':
                    (target / self.path.name).write_text('{"enabled":true,"templates":{}}')
                with self.assertRaises(ValueError):
                    policy(self.root)

    def test_special_policy_files_rejected_without_blocking(self):
        self.config.mkdir()
        self.path.mkdir()
        with self.assertRaisesRegex(ValueError, '普通文件'):
            policy(self.root)
        self.path.rmdir()
        os.mkfifo(self.path)
        real_open = os.open

        def checked_open(path, flags, **kwargs):
            if path == self.path.name:
                # Fail before opening if a regression would hang on the FIFO.
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertIn('dir_fd', kwargs)
            return real_open(path, flags, **kwargs)

        with patch('laowu.friend_policy.os.open', side_effect=checked_open):
            with self.assertRaisesRegex(ValueError, '普通文件'):
                policy(self.root)

    def test_non_directory_config_is_rejected(self):
        self.config.write_text('ordinary file')
        with self.assertRaises(ValueError):
            policy(self.root)
        self.config.unlink()
        os.mkfifo(self.config)
        with self.assertRaises(ValueError):
            policy(self.root)


if __name__ == '__main__':
    unittest.main()
