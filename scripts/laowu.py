#!/usr/bin/env python3
"""Portable entrypoint; works without pip install and with Chinese paths."""
from pathlib import Path
import os
import sys
import hashlib
import stat

venv = Path(__file__).resolve().parent.parent / '.venv'
python = venv / 'bin/python'
if python.exists():
    # Codex 0.163.0's command runner can corrupt a Unicode Python executable
    # path. Keep the same venv through an ASCII-only private directory alias.
    # This contains no copied environment, credentials, or project data.
    if not str(python).isascii():
        digest = hashlib.sha256(str(venv.resolve()).encode()).hexdigest()[:20]
        alias = Path('/tmp') / f'laowu-python-{os.getuid()}-{digest}'
        alias.mkdir(mode=0o700, exist_ok=True)
        info = alias.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Python 临时入口目录归属或权限不正确')
        target = alias / 'venv'
        try:
            target.symlink_to(venv.resolve(), target_is_directory=True)
        except FileExistsError:
            if not target.is_symlink() or target.resolve() != venv.resolve():
                raise RuntimeError('Python 临时入口冲突，未覆盖')
        python = target / 'bin/python'
    if Path(sys.prefix).resolve() != venv.resolve() or not sys.executable.isascii():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laowu.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
