"""Versioned friend profiles; executable capabilities are checked before activation."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .common import atomic_write, json_text, now, valid_id


class Registry:
    def __init__(self, store):
        self.store = store
        self.root = store.root
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS friend_registry (
                id TEXT PRIMARY KEY, version INTEGER NOT NULL, active INTEGER NOT NULL,
                digest TEXT NOT NULL, spec_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS friend_versions (
                friend_id TEXT NOT NULL, version INTEGER NOT NULL, spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(friend_id,version))""")

    def _validate(self, spec: dict) -> dict:
        if not isinstance(spec, dict):
            raise ValueError("朋友配置必须是对象")
        ident = valid_id(spec.get("id", ""))
        name = str(spec.get("name", "")).strip()
        description = str(spec.get("description", "")).strip()
        instructions = str(spec.get("instructions", "")).strip()
        if not all((name, description, instructions)):
            raise ValueError("朋友必须有 id、name、description、instructions")
        if len(instructions) > 32000:
            raise ValueError("朋友指令过长，请拆成技能")
        tools = spec.get("tools", ["memory", "checkpoint"])
        allowed = {"read", "write", "shell", "search", "web", "memory", "checkpoint"}
        if not isinstance(tools, list) or set(tools) - allowed:
            raise ValueError("未知工具能力；允许 read/write/shell/search/web/memory/checkpoint")
        can_write = spec.get("can_write", False)
        if not isinstance(can_write, bool) or not isinstance(spec.get("active", True), bool):
            raise ValueError("can_write / active 必须是布尔值")
        if "write" in tools and not can_write:
            raise ValueError("write 能力需要 can_write=true")
        skills = []
        for skill in spec.get("skills", []):
            slug = valid_id(skill if isinstance(skill, str) else skill["name"])
            file = self.root / ".agents" / "skills" / slug / "SKILL.md"
            if not file.is_file():
                raise ValueError(f"技能未安装：{slug}")
            content = file.read_text(encoding="utf-8")
            if not content.startswith("---\n") or "description:" not in content.split("---", 2)[1]:
                raise ValueError(f"技能缺少合法元数据：{slug}")
            skills.append({"name": slug, "content": content})
        servers = spec.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers 必须是对象")
        for key, server in servers.items():
            valid_id(key)
            if key == "laowu":
                raise ValueError("laowu MCP 由运行器注入，不得覆盖")
            if not isinstance(server, dict):
                raise ValueError("MCP 配置必须是对象")
            command = server.get("command")
            if command and not shutil.which(command):
                raise ValueError(f"MCP 命令不可用：{key}")
            if not command and not str(server.get("url", "")).startswith(("https://", "http://127.0.0.1:")):
                raise ValueError(f"MCP 缺少可用命令或 URL：{key}")
            if "env" in server:
                raise ValueError("朋友 MCP 不保存明文 env；请使用客户端现有环境变量")
        return {"id": ident, "name": name, "description": description,
                "instructions": instructions, "skills": skills, "mcp_servers": servers,
                "tools": list(dict.fromkeys(tools)), "can_write": can_write,
                "active": spec.get("active", True)}

    def ensure(self, spec: dict, write_files: bool = True) -> dict:
        profile = self._validate(spec)
        encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        folder = self.root / "friends" / profile["id"]
        self.store._safe(folder)
        manifest = {k: v for k, v in profile.items() if k not in {"instructions", "version"}}
        manifest["instructions_file"] = "AGENTS.md"
        manifest["skills"] = [s["name"] for s in profile["skills"]]
        contents = {"AGENTS.md": profile["instructions"] + "\n", "friend.json": json_text(manifest) + "\n"}
        # Stage all files before activation; a disk error must not activate an unusable profile.
        with tempfile.TemporaryDirectory(prefix="friend-stage-", dir=self.store.private) as temporary:
            stage = Path(temporary)
            if write_files:
                for name, content in contents.items():
                    atomic_write(stage / name, content, 0o644)
            previous, replaced = {}, []
            try:
                with self.store.transaction() as db:
                    old = db.execute("SELECT * FROM friend_registry WHERE id=?", (profile["id"],)).fetchone()
                    version = old["version"] if old and old["digest"] == digest else (old["version"] + 1 if old else 1)
                    profile["version"] = version
                    snapshot = json_text(profile)
                    if write_files:
                        folder.mkdir(parents=True, exist_ok=True)
                        for name in contents:
                            target = folder / name
                            self.store._safe(target)
                            previous[name] = target.read_bytes() if target.exists() else None
                            os.replace(stage / name, target)
                            replaced.append(name)
                    db.execute("INSERT OR IGNORE INTO friend_versions VALUES (?,?,?,?)",
                               (profile["id"], version, snapshot, now()))
                    db.execute("INSERT OR REPLACE INTO friend_registry VALUES (?,?,?,?,?,?)",
                               (profile["id"], version, int(profile["active"]), digest, snapshot, now()))
            except BaseException:
                for name in replaced:
                    target = folder / name
                    if previous[name] is None:
                        target.unlink(missing_ok=True)
                    else:
                        restored = stage / name
                        restored.write_bytes(previous[name])
                        os.replace(restored, target)
                raise
        return {"id": profile["id"], "name": profile["name"], "version": version, "active": profile["active"]}

    def sync(self) -> list[dict]:
        results = []
        for manifest in sorted((self.root / "friends").glob("*/friend.json")):
            spec = json.loads(manifest.read_text(encoding="utf-8"))
            if spec.get("id") != manifest.parent.name:
                raise ValueError(f"朋友目录与 ID 不符：{manifest.parent.name}")
            file = (manifest.parent / spec.get("instructions_file", "AGENTS.md")).resolve()
            if not file.is_relative_to(manifest.parent.resolve()):
                raise ValueError("朋友指令不能越出自己的目录")
            spec["instructions"] = file.read_text(encoding="utf-8")
            results.append(self.ensure(spec, write_files=False))
        return results

    def get(self, friend_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT spec_json,active FROM friend_registry WHERE id=?", (friend_id,)).fetchone()
        if not row:
            raise ValueError(f"朋友不存在：{friend_id}")
        if not row["active"]:
            raise ValueError(f"朋友已停用：{friend_id}")
        return json.loads(row["spec_json"])

    def list(self) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("SELECT id,version,active,spec_json FROM friend_registry ORDER BY id").fetchall()
        return [{"id": r["id"], "version": r["version"], "active": bool(r["active"]),
                 "name": json.loads(r["spec_json"])["name"],
                 "description": json.loads(r["spec_json"])["description"]} for r in rows]
