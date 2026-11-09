"""Versioned friend profiles; executable capabilities are checked before activation."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .common import atomic_write, json_text, new_id, now, valid_id


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
        if ident == "laowu":
            raise PermissionError("laowu 是保留主身份，不能注册为朋友")
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
            self.store._safe(file)
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
                    managed = db.execute("SELECT kind FROM friend_management WHERE friend_id=?", (profile["id"],)).fetchone()
                    if managed and managed['kind'] == 'dynamic':
                        raise PermissionError('文件维护不能覆盖动态朋友')
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
                    db.execute("INSERT OR IGNORE INTO friend_management(friend_id,kind,personal) VALUES(?,'static',?)",
                               (profile['id'], int(profile['id'] == 'confidant')))
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

    def get(self, friend_id: str, *, include_inactive=False, db=None) -> dict:
        if friend_id == 'laowu':
            raise PermissionError('laowu 是保留主身份')
        if db is None:
            with self.store.connect() as conn:
                return self.get(friend_id, include_inactive=include_inactive, db=conn)
        row = db.execute("SELECT spec_json,active FROM friend_registry WHERE id=?", (friend_id,)).fetchone()
        if not row:
            raise ValueError(f"朋友不存在：{friend_id}")
        if not row["active"] and not include_inactive:
            raise ValueError(f"朋友已停用：{friend_id}")
        return json.loads(row["spec_json"])

    @staticmethod
    def memory_policy(db, friend_id):
        if friend_id == 'laowu':
            raise PermissionError('laowu 不能作为 worker')
        row = db.execute('SELECT personal FROM friend_management WHERE friend_id=?', (friend_id,)).fetchone()
        return {'personal': bool(row and row['personal'])}

    def describe(self, friend_id, *, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.describe(friend_id, db=conn)
        spec = self.get(friend_id, include_inactive=True, db=db)
        row = db.execute('SELECT * FROM friend_management WHERE friend_id=?', (friend_id,)).fetchone()
        dynamic = bool(row and row['kind'] == 'dynamic')
        # Management responses and Context friend_definition reconstruction share
        # this allowlist. Execution alone uses get(), including service config.
        fields = ('id', 'name', 'description', 'instructions', 'skills', 'tools',
                  'can_write', 'active', 'version')
        public = {key: spec[key] for key in fields if key in spec}
        return dict(public, dynamic=dynamic, editable=dynamic,
                    template_id=row['template_id'] if dynamic else None,
                    needs_definition=bool(row and row['needs_definition']),
                    permission_ceiling=json.loads(row['ceiling_json']) if dynamic else None)

    def list(self, include_inactive=True, include_templates=False) -> list[dict] | dict:
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM friend_registry WHERE id!='laowu' AND (? OR active=1) ORDER BY id", (include_inactive,))]
            fields = ('id', 'version', 'active', 'name', 'description', 'dynamic', 'editable', 'template_id', 'needs_definition')
            friends = [{k: v for k, v in self.describe(ident, db=db).items() if k in fields} for ident in ids]
        if include_templates:
            from .friend_policy import catalog
            return dict(friends=friends, **catalog(self.root))
        return friends

    @staticmethod
    def _text(value, field, maximum):
        from .store import _SECRET
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or _SECRET.search(value):
            raise ValueError(field + ' 必须为非空限长通用职责文字，不能包含凭据')
        return value.strip()

    def _selection(self, fields, ceiling):
        result = {}
        for key in ('skills', 'tools'):
            value = fields.get(key, ceiling[key])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or not set(value) <= set(ceiling[key]):
                raise PermissionError(key + ' 超出创建时模板上限')
            result[key] = sorted(set(value))
        value = fields.get('can_write', ceiling['can_write'])
        if type(value) is not bool or (value and not ceiling['can_write']):
            raise PermissionError('can_write 超出创建时模板上限')
        result['can_write'] = value
        if 'write' in result['tools'] and not value:
            raise ValueError('write 需要 can_write=true')
        for key, maximum in (('name', 120), ('description', 1000), ('instructions', 32000)):
            result[key] = self._text(fields.get(key), key, maximum)
        return result

    def _save_dynamic(self, db, spec, version):
        spec['version'] = version
        encoded = json_text(spec)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        db.execute("INSERT INTO friend_registry VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,active=excluded.active,digest=excluded.digest,spec_json=excluded.spec_json,updated_at=excluded.updated_at",
                   (spec['id'], version, int(spec['active']), digest, encoded, now()))
        db.execute('INSERT INTO friend_versions VALUES(?,?,?,?)', (spec['id'], version, encoded, now()))

    def create(self, *, template_id, name, description, instructions, idempotency_key, authorize, **choices):
        if set(choices) - {'skills', 'tools', 'can_write'}:
            raise ValueError('不支持的动态朋友字段')
        key = self._text(idempotency_key, 'idempotency_key', 256)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        fields = dict(choices, name=name, description=description, instructions=instructions)
        from .friend_policy import policy
        with self.store.transaction() as db:
            source = authorize(db)
            previous = db.execute('SELECT * FROM friend_creations WHERE key_hash=?', (key_hash,)).fetchone()
            if previous:
                if previous['revoked']:
                    raise ValueError('此创建已被遗忘，幂等键不能复活角色')
                current = self.describe(previous['friend_id'], db=db)
                if template_id != current['template_id']:
                    raise ValueError('幂等键内容冲突')
                ceiling = current['permission_ceiling']
            else:
                approved = policy(self.root)
                if not approved['enabled']:
                    raise PermissionError('自主朋友管理尚未启用')
                if template_id not in approved['templates']:
                    raise PermissionError('模板未获本机批准')
                ceiling = approved['templates'][template_id]
            normalized = self._selection(fields, ceiling)
            request_hash = hashlib.sha256(json_text(dict(normalized, template_id=template_id)).encode()).hexdigest()
            if previous:
                if request_hash != previous['request_hash']:
                    raise ValueError('幂等键内容冲突')
                return current
            ident = new_id('friend')
            if db.execute('SELECT 1 FROM friend_registry WHERE id=?', (ident,)).fetchone():
                raise ValueError('服务端生成的朋友 ID 冲突，请重试')
            spec = self._validate(dict(normalized, id=ident, active=True, mcp_servers={}))
            self._save_dynamic(db, spec, 1)
            db.execute("INSERT INTO friend_management(friend_id,kind,template_id,ceiling_json,personal,origin_json) VALUES(?,'dynamic',?,?,?,?)",
                       (ident, template_id, json_text(ceiling), int(ceiling['personal']), json_text(source)))
            db.execute('INSERT INTO friend_creations(key_hash,request_hash,friend_id) VALUES(?,?,?)', (key_hash, request_hash, ident))
            return self.describe(ident, db=db)

    def _editable(self, db, friend_id, expected_version, authorize):
        authorize(db)
        from .friend_policy import policy
        if not policy(self.root)['enabled']:
            raise PermissionError('自主朋友管理尚未启用')
        spec = self.describe(friend_id, db=db)
        if not spec['dynamic']:
            raise PermissionError('内置及文件维护朋友受保护')
        if type(expected_version) is not int or expected_version < 1 or spec['version'] != expected_version:
            raise ValueError('朋友版本冲突，请重新 friend_get 后决定')
        return spec

    def update(self, *, friend_id, expected_version, authorize, **fields):
        if not fields or set(fields) - {'name', 'description', 'instructions', 'skills', 'tools', 'can_write'}:
            raise ValueError('只能修改通用职责及上限内能力')
        with self.store.transaction() as db:
            current = self._editable(db, friend_id, expected_version, authorize)
            if current['needs_definition'] and not {'name', 'description', 'instructions'} <= fields.keys():
                raise ValueError('遗忘后须重新提供完整通用职责')
            base = dict(current, skills=[s['name'] for s in current['skills']])
            if current['needs_definition']:
                base.update(current['permission_ceiling'])
            selected = self._selection(dict(base, **fields), current['permission_ceiling'])
            spec = self._validate(dict(selected, id=friend_id, active=current['active'], mcp_servers={}))
            self._save_dynamic(db, spec, expected_version + 1)
            db.execute('UPDATE friend_management SET needs_definition=0 WHERE friend_id=?', (friend_id,))
            return self.describe(friend_id, db=db)

    def set_active(self, *, friend_id, expected_version, active, authorize):
        if type(active) is not bool:
            raise ValueError('active 必须为布尔值')
        with self.store.transaction() as db:
            current = self._editable(db, friend_id, expected_version, authorize)
            if active and current['needs_definition']:
                raise ValueError('遗忘后须先重新定义角色')
            spec = self.get(friend_id, include_inactive=True, db=db)
            spec['active'] = active
            self._save_dynamic(db, spec, expected_version + 1)
            return self.describe(friend_id, db=db)
