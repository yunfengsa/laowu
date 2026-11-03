"""Small deterministic utilities; never inspect or copy client credentials."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".laowu-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def process_identity(pid: int) -> str | None:
    """Linux PID start ticks prevent PID reuse from granting run ownership."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except (OSError, ValueError, IndexError):
        return None


def process_alive(pid: int | None, identity: str | None) -> bool:
    return bool(pid and identity and process_identity(pid) == identity)


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("ID 必须使用小写字母、数字、下划线或短横线，最多 64 字符")
    return value


def project_root(value: str | Path | None = None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    elif os.environ.get("LAOWU_ROOT"):
        root = Path(os.environ["LAOWU_ROOT"]).resolve()
    else:
        root = Path(__file__).resolve().parent.parent
    return root
