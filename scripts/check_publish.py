#!/usr/bin/env python3
"""Read the Git index only. Report paths/lines/rules, never matched values."""
import fnmatch
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PRIVATE_DIRS = {
    '.laowu', '.codex', '.aws', '.ssh', 'credentials', '.credentials',
    'secrets', '.secrets', 'backups', 'backup', '.backups', 'logs',
    '.venv', 'venv', 'env', '.tox', '.nox', '__pycache__', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.cache', 'build', 'dist', 'node_modules', 'htmlcov',
}
PRIVATE_NAMES = (
    '.env', '.env.*', 'credentials.json', 'credentials.*.json',
    '*-credentials.json', '*_credentials.json', 'secrets.json', 'secrets.*.json',
    'auth.json', '.git-credentials', '.netrc', '.npmrc', '.pypirc', '*.pem', '*.key', '*.p12',
    '*.pfx', '*.jks', 'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*',
    '*.sqlite', '*.sqlite3', '*.db', '*.db3', '*.sqlite-*', '*.sqlite3-*',
    '*.db-*', '*.db3-*', '*-wal', '*-shm', '*.log', '*.log.*', '*.bak',
    '*.backup', '*.orig', 'snapshot_*.zip', '*.swp', '*.swo', '*~',
    '*.py[cod]', '.coverage', '.coverage.*', 'coverage.xml', '.DS_Store',
)
PRIVATE_DOCS = (
    'acceptance.md', '*-acceptance.md', 'client-error-investigation.md',
    '*-manifest.json', 'phase1-second-review.md', '*.patch',
)
SAFE_EXAMPLES = ('.env.example', '.env.*.example', 'credentials.example.json', 'secrets.example.json')
CONTENT_RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----(?:\s|\\[nr])+[A-Za-z0-9+/=]{40,}'),
    'slack-token': re.compile(r'\bxox[baprs]-[0-9]{8,}-[A-Za-z0-9-]{16,}\b|\bxapp-\d+-[A-Za-z0-9]+-\d+-[A-Za-z0-9]{20,}\b'),
    'github-token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b'),
    'api-key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
    'aws-access-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private-runtime-id': re.compile(r'\b(?:task|run|session|sess|memory|mem|event)_[0-9a-f]{24,}\b', re.I),
    'session-uuid': re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b', re.I),
    'local-home-path': re.compile(r'/(?:root|home|Users)/[^\s\x27\x22`<>]+'),
}


def path_rule(path):
    """Check paths before reading blobs, even when force-added or already tracked."""
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts or str(p) != path:
        return 'invalid-path'
    if any(part in PRIVATE_DIRS or part.endswith('.egg-info') for part in p.parts[:-1]):
        return 'private-directory'
    if path in {'config/features.json', 'config/friend-policy.json'} or path.endswith('.claude/settings.local.json'):
        return 'local-config'
    if p.parts[-3:] == ('.config', 'git', 'credentials'):
        return 'private-file'
    if p.parts[:2] == ('docs', 'phase1-skills'):
        return 'local-draft'
    if p.parent == PurePosixPath('docs') and any(fnmatch.fnmatchcase(p.name, pat) for pat in PRIVATE_DOCS):
        return 'local-record'
    if any(fnmatch.fnmatchcase(p.name, pat) for pat in SAFE_EXAMPLES):
        return None
    if any(fnmatch.fnmatchcase(p.name, pat) for pat in PRIVATE_NAMES):
        return 'private-file'
    return None


def content_findings(data):
    """Return line numbers and fixed rule names, without copying source text."""
    if data.startswith(b'SQLite format 3\x00'):
        return [(1, 'sqlite-content')]
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError:
        return [(1, 'non-text-review-required')]
    if '\x00' in content:
        return [(1, 'non-text-review-required')]
    return sorted({(content.count('\n', 0, match.start()) + 1, rule)
                   for rule, pattern in CONTENT_RULES.items()
                   for match in pattern.finditer(content)})


def git(*args):
    return subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=Path(__file__).resolve().parents[1]).stdout


def inspect_index():
    findings = []
    entries = [entry for entry in git('ls-files', '--stage', '-z').split(b'\0') if entry]
    for entry in entries:
        meta, raw_path = entry.split(b'\t', 1)
        mode, oid, stage = meta.decode('ascii').split()
        path = raw_path.decode('utf-8', errors='surrogateescape')
        rule = path_rule(path)
        if rule:
            findings.append((path, 0, rule))
            continue
        if stage != '0' or mode not in ('100644', '100755'):
            findings.append((path, 0, 'non-regular-index-entry'))
            continue
        data = git('cat-file', 'blob', oid)
        findings.extend((path, line, rule) for line, rule in content_findings(data))
    return len(entries), findings


def main():
    try:
        count, findings = inspect_index()
    except (OSError, subprocess.CalledProcessError, ValueError):
        print('publish-check: index-read-failed', file=sys.stderr)
        return 2
    for path, line, rule in findings:
        print(json.dumps({'path': path, 'line': line, 'rule': rule}, ensure_ascii=True))
    if not count:
        print('publish-check: empty-index; no release content checked', file=sys.stderr)
        return 2
    print(f'publish-check: {count} index entries, {len(findings)} findings')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
