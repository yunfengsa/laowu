"""Named Codex filesystem profiles with an actual no-model isolation probe.

The outer Codex process keeps its login. Only its executed commands receive the
restricted environment and filesystem. Probe files contain random test data,
never copies of existing private files; no user/global config is modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .adapters import _toml


PROFILE = "laowu_slack"
_LOCK = threading.RLock()
_CACHE = {}
_SUCCESS_TTL = 300
_FAILURE_TTL = 30


def _paths(root, workspace):
    root, workspace = Path(root).expanduser().resolve(), Path(workspace).expanduser().resolve()
    home = Path.home().resolve()
    raw_codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser().absolute()
    codex_home = raw_codex.resolve()
    private = root / ".laowu"
    base = [private, raw_codex, codex_home, home / ".codex", home / ".ssh", home / ".aws", home / ".config"]
    protected = list(dict.fromkeys([*base, *(path.resolve() for path in base)]))
    broad = {Path("/"), home, Path("/tmp"), Path("/var/tmp"), Path("/usr"), Path("/var"),
             Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev")}
    for path in (root, workspace):
        if not path.is_dir() or path in broad or home.is_relative_to(path):
            raise PermissionError("Slack 工程工作目录必须是具体项目，不能是系统根目录或用户主目录")
        if any(path == p or path.is_relative_to(p) for p in protected):
            raise PermissionError("Slack 工程目录不能位于记忆或凭据目录内")
    # A project root owns its .laowu directory by design; only reject the
    # credentials trees here, otherwise every ordinary project would be invalid.
    external = [p for p in protected if p != private and p != private.resolve()]
    if any(p.is_relative_to(path) for p in external for path in (root, workspace)):
        raise PermissionError("Slack 工程工作目录不能包含用户凭据目录")
    for sensitive in (private, home / ".ssh", home / ".aws", home / ".config"):
        if codex_home.is_relative_to(sensitive.resolve()):
            raise PermissionError("Codex 安装目录与私人目录重叠，无法安全隔离")
    return root, workspace, home, raw_codex, codex_home, protected


def profile_overrides(root, workspace, can_write):
    """Return per-launch ``-c`` overrides; do not combine with ``--sandbox``.

    ``can_write=False`` still allows project/workspace reads, but never changes
    the secret carve-outs or the command environment. Network access is off.
    """
    if not isinstance(can_write, bool):
        raise ValueError("can_write 必须为布尔值")
    root, workspace, home, raw_codex, codex_home, protected = _paths(root, workspace)
    filesystem = {":root": "deny", ":minimal": "read"}
    # An overridden CODEX_HOME can differ from the actual installed executable.
    # Keep only the installation packages readable under either standard home.
    for path in (raw_codex / "packages", codex_home / "packages", home / ".codex/packages"):
        filesystem[str(path)] = "read"
    filesystem[str(root)] = "read"
    filesystem[str(workspace)] = "write" if can_write else "read"
    credential_homes = {raw_codex, codex_home, home / ".codex", (home / ".codex").resolve()}
    for path in protected:
        if path in credential_homes:
            # Explicit parent denies also veto nested package grants in Codex
            # 0.163.0. Default deny covers future children; add exact denies for
            # every existing non-package child, including symlink targets.
            if path.is_dir():
                for child in path.iterdir():
                    if child.name != "packages":
                        filesystem[str(child)] = "deny"
                        filesystem[str(child.resolve())] = "deny"
            for name in ("auth.json", "config.toml", "sessions", "archived_sessions", "history.jsonl"):
                filesystem[str(path / name)] = "deny"
        else:
            filesystem[str(path)] = "deny"
    filesystem["/proc"] = "deny"
    return {
        "default_permissions": PROFILE,
        "permissions." + PROFILE + ".filesystem": filesystem,
        "permissions." + PROFILE + ".network.enabled": False,
        "shell_environment_policy": {
            "inherit": "none", "set": {
                "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                "HOME": str(workspace), "PYTHONPATH": str(root),
            },
        },
    }


def _probe_environment():
    # The probe uses the real configuration stack/version but no provider key.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home()), "LANG": "C.UTF-8"}
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    return env


def _identity(path):
    try:
        info = Path(path).stat()
        return [str(Path(path).resolve()), info.st_dev, info.st_ino]
    except OSError:
        return [str(Path(path).resolve()), None, None]


def _config_identity(root, workspace, codex_home):
    # Per-launch overrides merge with user/project policy. A policy edit must
    # invalidate acceptance even when the generated profile itself is unchanged.
    paths = {codex_home / "config.toml", Path("/etc/codex/config.toml"),
             Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml")}
    for anchor in (root, workspace):
        paths.update(parent / ".codex/config.toml" for parent in (anchor, *anchor.parents))
    result = []
    for path in sorted(paths, key=str):
        try:
            info = path.stat()
            result.append([str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
        except FileNotFoundError:
            result.append([str(path), None])
    return result


def _version(executable, env):
    try:
        result = subprocess.run([executable, "--version"], env=env, capture_output=True,
                                text=True, timeout=10, check=False)
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.]+)?\b", result.stdout)
        if result.returncode or not match:
            return None
        info = Path(executable).resolve().stat()
        return [match.group(), str(Path(executable).resolve()), info.st_size, info.st_mtime_ns]
    except (OSError, subprocess.TimeoutExpired):
        return None


_PROBE_SCRIPT = r'''
import errno, json, os, sqlite3, sys
from pathlib import Path
p = json.loads(sys.argv[1])
denied = {errno.EACCES, errno.EPERM, errno.ENOENT}
failures = 0
if Path(p['source']).read_bytes() != b'laowu-permission-probe':
    failures += 1
target = Path(p['write'])
try:
    target.write_bytes(b'probe-write')
    if target.read_bytes() != b'probe-write': failures += 1
except OSError:
    failures += 1
for path in p['denied']:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno not in denied: failures += 1
    else:
        # An unexpected successful open fails the probe WITHOUT reading data.
        os.close(fd)
        failures += 1
for name in ('LAOWU_PROBE_CREDENTIAL', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
    if os.environ.get(name): failures += 1
# Codex exposes proc for the sandbox's own process and wrapper. They are not
# host model processes; check our synthetic environment contains no credentials.
for name in ('self', str(os.getppid())):
    path = '/proc/' + name + '/environ'
    try:
        with open(path, 'rb') as stream:
            data = stream.read(1048576)
        if b'probe-only-not-a-secret' in data: failures += 1
    except OSError as exc:
        if exc.errno not in denied: failures += 1
print('laowu-sandbox-probe:ok' if failures == 0 else 'laowu-sandbox-probe:failed')
sys.exit(0 if failures == 0 else 1)
'''


def probe(root, workspace):
    """Check exact host filesystem/environment enforcement without a model.

    True is cached for five minutes, False for thirty seconds. Every lookup
    checks the Codex version/binary and profile/path identity. The probe checks
    protected random markers and symlinks, host-parent /proc opens, project reads
    and a real workspace write. The sandbox's own wrapper proc may remain
    readable; only its synthetic test environment is inspected. Nothing private
    is ever read or printed.
    """
    with _LOCK:
        try:
            config = profile_overrides(root, workspace, True)
            root, workspace, home, raw_codex, codex_home, protected = _paths(root, workspace)
            executable = shutil.which("codex")
            if not executable:
                return False
            env = _probe_environment()
            version = _version(executable, env)
            if not version:
                return False
            key_data = [version, config, _config_identity(root, workspace, codex_home),
                        os.getuid(), _identity(root), _identity(workspace),
                        [_identity(path) for path in protected]]
            key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached = _CACHE.get(key)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            result = _run_probe(executable, root, workspace, protected, config, env)
            if len(_CACHE) >= 128:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic() + (_SUCCESS_TTL if result else _FAILURE_TTL), result)
            return result
        except (OSError, ValueError, PermissionError, subprocess.TimeoutExpired):
            return False


def _run_probe(executable, root, workspace, protected, config, env):
    markers = []
    source = None
    try:
        # The private project directory must already exist. Never manufacture
        # ~/.ssh, ~/.aws or any credential directory merely to run a probe.
        if not (root / ".laowu").is_dir():
            return False
        for directory in dict.fromkeys(p.resolve() for p in protected if p.is_dir()):
            fd, filename = tempfile.mkstemp(prefix=".laowu-permission-probe-", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"laowu-permission-probe")
            markers.append(Path(filename))
        if len(markers) < 2:
            return False
        fd, source_name = tempfile.mkstemp(prefix=".laowu-source-probe-", dir=root)
        source = Path(source_name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"laowu-permission-probe")
        with tempfile.TemporaryDirectory(prefix=".laowu-sandbox-probe-", dir=workspace) as temporary:
            directory = Path(temporary)
            denied = list(map(str, markers)) + ["/proc/" + str(os.getpid()) + "/environ"]
            for index, target in enumerate(denied.copy()):
                link = directory / ("denied-link-" + str(index))
                link.symlink_to(target)
                denied.append(str(link))
            data = {"source": str(source), "write": str(directory / "write-test"), "denied": denied}
            argv = [executable, "--no-daemon", "sandbox", "-P", PROFILE, "-C", str(workspace)]
            for name, value in config.items():
                argv += ["-c", name + "=" + _toml(value)]
            # Isolated Python prevents workspace/PYTHONPATH modules from
            # replacing stdlib functions and forging an isolation result.
            argv += ["--", str(Path(sys.executable).resolve()), "-I", "-c", _PROBE_SCRIPT,
                     json.dumps(data, ensure_ascii=False)]
            env = {**env, "LAOWU_PROBE_CREDENTIAL": "probe-only-not-a-secret",
                   "OPENAI_API_KEY": "probe-only-not-a-secret", "ANTHROPIC_API_KEY": "probe-only-not-a-secret"}
            result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True,
                                    text=True, timeout=30, check=False)
            return result.returncode == 0 and result.stdout.strip() == "laowu-sandbox-probe:ok"
    finally:
        for path in [*markers, *([source] if source else [])]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
