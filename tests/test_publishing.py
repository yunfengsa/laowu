"""Release checks use synthetic blobs and paths, never the live project state."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import check_publish


class PublishingTests(unittest.TestCase):
    def test_ignore_policy_keeps_source_examples_and_sql_but_excludes_local_files(self):
        private = [
            '.laowu/state.sqlite3', 'nested/.laowu/queue.json', '.codex/config.toml',
            '.codex/hooks.json', 'config/features.json', 'config/friend-policy.json', 'nested/credentials/tokens.json',
            'nested/.env', 'nested/.env.production', 'nested/.aws/credentials',
            'nested/secrets.json', 'nested/slack-credentials.json', 'nested/id_ed25519',
            '.git-credentials', 'nested/.git-credentials', '.config/git/credentials',
            'nested/.config/git/credentials',
            'nested/signing.pem', 'nested/state.db', 'nested/state.sqlite3-wal',
            'nested/state.sqlite3-shm', 'nested/logs/service.txt', 'nested/backups/data.zip',
            'nested/.venv/pyvenv.cfg', 'nested/.cache/value', 'nested/build/package.whl',
            'docs/acceptance.md', 'docs/slack-context-acceptance.md',
            'docs/client-error-investigation.md', 'docs/phase1-final-manifest.json',
            'docs/phase1-second-review.md', 'docs/phase1-skills/engineering.md',
            'docs/phase1-skills.patch',
        ]
        public = [
            'README.md', 'AGENTS.md', 'laowu/store.py', 'scripts/laowu.py',
            '.agents/skills/engineering/SKILL.md', 'friends/engineer/friend.json',
            'tests/fixtures/phase1_schema2.sql', 'config/features.example.json', 'config/friend-policy.example.json',
            'config/runtime-policy.json', 'config/slack-manifest.json',
            'nested/.env.example', 'nested/.env.production.example',
            'nested/credentials.example.json', 'nested/secrets.example.json',
            'docs/verification.md', 'docs/slack-recovery.md', 'docs/publishing.md',
        ]
        # Use the real ignore rules in an empty temporary repo. No staging or commits.
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / '.gitignore').write_bytes((Path(__file__).resolve().parents[1] / '.gitignore').read_bytes())
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            result = subprocess.run(
                ['git', '-C', str(root), '-c', 'core.excludesFile=/dev/null',
                 'check-ignore', '--no-index', '-z', '--stdin'],
                input=('\0'.join(private + public) + '\0').encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            )
        ignored = set(result.stdout.decode().rstrip('\0').split('\0'))
        self.assertEqual(ignored, set(private))
        for path in private:
            with self.subTest(path=path):
                self.assertIsNotNone(check_publish.path_rule(path))
        for path in public:
            with self.subTest(path=path):
                self.assertIsNone(check_publish.path_rule(path))
        self.assertIsNotNone(check_publish.path_rule('credentials/.env.example'))

    def test_index_private_paths_symlinks_and_conflicts_are_rejected_before_blob_read(self):
        paths = [('.laowu/private.json', '100644', '0'),
                 ('docs/local-acceptance.md', '100644', '0'),
                 ('outside-link', '120000', '0'), ('conflict.py', '100644', '2')]
        entries = b''.join(f'{mode} {"a" * 40} {stage}\t{path}\0'.encode()
                           for path, mode, stage in paths)
        with patch.object(check_publish, 'git', return_value=entries) as git:
            count, findings = check_publish.inspect_index()
        git.assert_called_once_with('ls-files', '--stage', '-z')
        self.assertEqual(count, 4)
        self.assertEqual({path for path, _, _ in findings}, {p for p, _, _ in paths})

    def test_scan_reads_staged_blob_and_never_prints_matching_value(self):
        secret = 'ghp_' + 'A' * 36  # Deliberately synthetic, assembled only in memory.
        oid = 'b' * 40
        calls = []

        def git(*args):
            calls.append(args)
            if args == ('ls-files', '--stage', '-z'):
                return f'100644 {oid} 0\tREADME.md\0'.encode()
            self.assertEqual(args, ('cat-file', 'blob', oid))
            return ('first line\n' + secret + '\n').encode()

        output = io.StringIO()
        with patch.object(check_publish, 'git', side_effect=git), contextlib.redirect_stdout(output):
            self.assertEqual(check_publish.main(), 1)
        report = output.getvalue()
        self.assertNotIn(secret, report)
        self.assertEqual(json.loads(report.splitlines()[0]),
                         {'path': 'README.md', 'line': 2, 'rule': 'github-token'})
        self.assertEqual(len(calls), 2)

    def test_signatures_detect_secrets_and_runtime_metadata_without_keyword_false_positives(self):
        examples = {
            'private-key': '-----BEGIN ' + 'PRIVATE KEY-----\n' + 'A' * 48,
            'slack-token': 'xoxb-' + '1' * 12 + '-' + '2' * 12 + '-' + 'A' * 24,
            'github-token': 'github_pat_' + 'A' * 70,
            'api-key': 'sk-proj-' + 'a' * 48,
            'aws-access-key': 'AKIA' + 'A' * 16,
            'private-runtime-id': 'task_' + 'f' * 32,
            'session-uuid': '-'.join(('abcdefab', 'abcd', '4abc', '8abc', 'abcdefabcdef')),
            'local-home-path': '/' + 'home/' + 'example-user/project',
        }
        for rule, value in examples.items():
            with self.subTest(rule=rule):
                self.assertIn((1, rule), check_publish.content_findings(value.encode()))
        safe = b'token = "xoxb-offline-test"\nTASK_ID run_test context_read(ref, cursor)\n'
        safe += b'-----BEGIN ' + b'PRIVATE KEY-----'  # A header alone has no key material.
        self.assertEqual(check_publish.content_findings(safe), [])
        self.assertEqual(check_publish.content_findings(b'SQLite format 3\x00'), [(1, 'sqlite-content')])

    def test_safe_example_filename_does_not_bypass_content_check(self):
        oid = 'c' * 40
        with patch.object(check_publish, 'git', side_effect=[
            f'100644 {oid} 0\tcredentials.example.json\0'.encode(),
            ('{"key":"sk-' + 'A' * 30 + '"}').encode(),
        ]):
            _, findings = check_publish.inspect_index()
        self.assertEqual(findings, [('credentials.example.json', 1, 'api-key')])

    def test_empty_index_and_git_error_cannot_claim_pass(self):
        with patch.object(check_publish, 'git', return_value=b''), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_publish.main(), 2)
        output = io.StringIO()
        with patch.object(check_publish, 'git', side_effect=subprocess.CalledProcessError(
                1, 'git', stderr=b'potentially private error text')), contextlib.redirect_stderr(output):
            self.assertEqual(check_publish.main(), 2)
        self.assertNotIn('potentially private error text', output.getvalue())

    def test_example_flags_match_runtime_defaults(self):
        from laowu.features import DEFAULTS
        example = Path(__file__).resolve().parents[1] / 'config/features.example.json'
        self.assertEqual(json.loads(example.read_text()), DEFAULTS)
        self.assertFalse(any(DEFAULTS.values()))


if __name__ == '__main__':
    unittest.main()
