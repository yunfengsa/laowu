import concurrent.futures
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from laowu.common import process_identity
from laowu.store import Store, tokenize


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "项目"
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chinese_persists_and_ascii_is_casefolded(self):
        self.store.memory_upsert("我喜欢重庆火锅，使用 PostgreSQL 数据库。")
        restarted = Store(self.root)
        self.assertEqual(len(restarted.memory_search("重庆火锅")), 1)
        self.assertEqual(len(restarted.memory_search("postgresql")), 1)
        self.assertEqual(tokenize("火锅 SQL"), ["火", "锅", "火锅", "sql"])
        self.assertEqual(self.root.joinpath(".laowu").stat().st_mode & 0o777, 0o700)

    def test_scope_permissions(self):
        for scope in ("personal", "shared", "tasks", "friend:engineer", "friend:researcher"):
            self.store.memory_upsert(scope, scope=scope)
        self.assertEqual({m["scope"] for m in self.store.memory_search(actor="engineer")},
                         {"shared", "tasks", "friend:engineer"})
        self.assertEqual({m["scope"] for m in self.store.memory_search(actor="confidant")},
                         {"personal", "shared", "tasks"})
        self.assertEqual(len(self.store.memory_search()), 5)
        self.assertNotIn("personal", {m["scope"] for m in self.store.memory_search(actor="laowu")})
        self.assertEqual(self.store.memory_search(scope="personal", actor="engineer"), [])
        with self.assertRaises(PermissionError):
            self.store.memory_upsert("私人", actor="engineer")

    def test_correction_invalidates_old_index_and_derivatives(self):
        fact = self.store.memory_upsert("我的城市是杭州")
        derivative = self.store.memory_upsert("住在杭州的用户", kind="summary", derived_from=[fact["id"]])
        snapshot = Path(self.store.backup()["path"])
        updated = self.store.memory_upsert("我的城市是南京", memory_id=fact["id"])
        self.assertFalse(snapshot.exists())
        self.assertEqual(updated["revision"], 2)
        records = Store(self.root).memory_search()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"], "我的城市是南京")
        self.assertNotIn(derivative["id"], [m["id"] for m in records])
        self.assertEqual(self.store.memory_search("杭州"), [])

    def test_no_derived_privacy_widening_or_cycles(self):
        first = self.store.memory_upsert("心事")
        with self.assertRaises(ValueError):
            self.store.memory_upsert("summary", scope="shared", derived_from=[first["id"]])
        second = self.store.memory_upsert("another", derived_from=[first["id"]])
        with self.assertRaises(ValueError):
            self.store.memory_upsert("cyclic", memory_id=first["id"], derived_from=[second["id"]])

    def test_forget_cascades_purges_snapshots_and_blocks_old_export(self):
        first = self.store.memory_upsert("secret story")
        second = self.store.memory_upsert("summary", derived_from=[first["id"]])
        third = self.store.memory_upsert("summary of summary", derived_from=[second["id"]])
        snapshot = Path(self.store.backup()["path"])
        external = Path(self.tmp.name) / "old.zip"
        shutil.copyfile(snapshot, external)
        result = self.store.memory_forget(first["id"])
        self.assertEqual(result["deleted_count"], 3)
        self.assertFalse(snapshot.exists())
        ledger = self.store.ledger_path.read_text()
        self.assertNotIn("secret story", ledger)
        self.store.restore(external)
        self.assertEqual(Store(self.root).memory_search(), [])
        self.assertIn(third["id"], json.loads(ledger)["ids"])
        with self.assertRaises(ValueError):
            self.store.memory_upsert("revival", memory_id=first["id"])

    def test_snapshot_can_move_to_new_root_and_preserves_unknown_tables(self):
        self.store.memory_upsert("跨项目恢复")
        (self.root / "friends" / "engineer").mkdir(parents=True)
        (self.root / "friends" / "engineer" / "AGENTS.md").write_text("工程师", encoding="utf-8")
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE custom_runtime(id TEXT PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO custom_runtime VALUES('task1','done')")
        snapshot = self.store.backup()["path"]
        other = Store(Path(self.tmp.name) / "new")
        other.restore(snapshot)
        self.assertEqual(other.memory_search()[0]["content"], "跨项目恢复")
        self.assertEqual((other.root / "friends/engineer/AGENTS.md").read_text(), "工程师")
        with other.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM custom_runtime").fetchone()[0], "done")

    def test_restore_without_friend_policy_removes_current_policy_and_defaults_closed(self):
        from laowu.friend_policy import policy
        path = self.root / 'config/friend-policy.json'
        old = Path(self.tmp.name) / 'without-policy.zip'
        shutil.copyfile(self.store.backup()['path'], old)
        with zipfile.ZipFile(old) as archive:
            self.assertNotIn('assets/config/friend-policy.json', archive.namelist())
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"enabled":true,"templates":{}}')
        enabled = Path(self.tmp.name) / 'with-policy.zip'
        shutil.copyfile(self.store.backup()['path'], enabled)
        self.assertTrue(policy(self.root)['enabled'])
        for _ in range(2):
            self.store.restore(old)
            self.assertFalse(path.exists())
            self.assertEqual(policy(self.root), {'enabled': False, 'templates': {}})
            self.store.restore(enabled)
            self.assertTrue(policy(self.root)['enabled'])

    def test_moved_snapshot_can_regenerate_codex_absolute_paths(self):
        from laowu.adapters import render_project_configs
        render_project_configs(self.root)
        snapshot = self.store.backup()["path"]
        other = Store(Path(self.tmp.name) / "new-config-root")
        other.restore(snapshot)
        render_project_configs(other.root)
        restored = (other.root / ".codex/config.toml").read_text()
        self.assertIn(str(other.root), restored)
        self.assertNotIn(str(self.root), restored)
        self.assertTrue((other.root / ".codex/hooks.json").is_file())

    def test_restore_removes_extra_friends_and_skills_but_preserves_extra_docs(self):
        original = self.root / "friends/engineer/AGENTS.md"
        original.parent.mkdir(parents=True)
        original.write_text("original role")
        snapshot = self.store.backup()["path"]
        extra_friend = self.root / "friends/newfriend/AGENTS.md"
        extra_skill = self.root / ".agents/skills/newskill/SKILL.md"
        extra_doc = self.root / "docs/extra.md"
        for path in (extra_friend, extra_skill, extra_doc):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("added after snapshot")
        original.write_text("changed role")
        self.store.restore(snapshot)
        self.assertEqual(original.read_text(), "original role")
        self.assertFalse(extra_friend.exists())
        self.assertFalse(extra_skill.exists())
        self.assertTrue(extra_doc.exists())

    def test_moved_snapshot_remaps_internal_workspace_and_artifact_paths_only(self):
        internal = str(self.root / "coding")
        external = str(Path(self.tmp.name) / "external-project")
        artifact = str(self.root / "coding/result.py")
        natural_text = f"上次在 {internal} 工作"
        context = json.dumps({"summary": natural_text, "artifacts": [artifact, external, "relative.txt"]})
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE tasks(id TEXT,status TEXT,workspace TEXT,checkpoint TEXT,result TEXT)")
            conn.execute("INSERT INTO tasks VALUES('inside','queued',?,?,?)", (internal, context, context))
            conn.execute("INSERT INTO tasks VALUES('outside','queued',?,'{}',NULL)", (external,))
            conn.execute("CREATE TABLE runs(id TEXT,status TEXT,workspace TEXT)")
            conn.execute("INSERT INTO runs VALUES('r','running',?)", (internal,))
        snapshot = self.store.backup()["path"]
        other = Store(Path(self.tmp.name) / "moved-root")
        other.restore(snapshot)
        with other.connect() as conn:
            inside = conn.execute("SELECT * FROM tasks WHERE id='inside'").fetchone()
            self.assertEqual(inside["workspace"], str(other.root / "coding"))
            for field in ("checkpoint", "result"):
                value = json.loads(inside[field])
                self.assertEqual(value["summary"], natural_text)
                self.assertEqual(value["artifacts"], [str(other.root / "coding/result.py"), external, "relative.txt"])
            self.assertEqual(conn.execute("SELECT workspace FROM tasks WHERE id='outside'").fetchone()[0], external)
            self.assertEqual(conn.execute("SELECT workspace FROM runs").fetchone()[0], str(other.root / "coding"))

    def test_failed_asset_restore_preserves_current_database_and_files(self):
        role = self.root / "friends/engineer/AGENTS.md"
        role.parent.mkdir(parents=True)
        role.write_text("snapshot role")
        self.store.memory_upsert("snapshot memory")
        snapshot = self.store.backup()["path"]
        role.write_text("current role")
        extra = self.root / "friends/current-only/AGENTS.md"
        extra.parent.mkdir(parents=True)
        extra.write_text("current only")
        self.store.memory_upsert("current memory")
        actual_replace = os.replace
        failed = False

        def failing_replace(source, target):
            nonlocal failed
            if Path(target) == role and not failed:
                failed = True
                raise OSError("simulated asset disk error")
            return actual_replace(source, target)

        with patch("laowu.store.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                self.store.restore(snapshot)
        self.assertEqual(role.read_text(), "current role")
        self.assertEqual(extra.read_text(), "current only")
        self.assertEqual(len(self.store.memory_search()), 2)

    def test_concurrent_writes_and_safe_sql(self):
        def write(i):
            return Store(self.root).memory_upsert(f"concurrent item number {i}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(30)))
        self.assertEqual(len(self.store.memory_search(limit=100)), 30)
        self.store.memory_search("' OR 1=1; DROP TABLE memory_records; --")
        self.assertEqual(len(self.store.memory_search(limit=100)), 30)

    def test_reject_real_secret_shapes_but_allow_normal_explanations(self):
        for content in ("-----BEGIN PRIVATE KEY-----", "sk-" + "A1" * 20,
                        "api_key=" + "abc123" * 8, "ghp_" + "a" * 36):
            with self.assertRaises(ValueError):
                self.store.memory_upsert(content)
        self.store.memory_upsert("API key 放在环境变量里，不要写入记忆。")
        self.store.memory_upsert("今天想聊一聊自己的秘密和烦恼")
        self.assertEqual(len(self.store.memory_search()), 2)

    def test_reject_path_traversal_and_project_symlinks(self):
        malicious = Path(self.tmp.name) / "bad.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../../escape", "bad")
        with self.assertRaises(ValueError):
            self.store.restore(malicious)
        (self.root / "friends").symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.backup()

    def test_active_process_blocks_restore_but_stale_pid_does_not(self):
        snapshot = self.store.backup()["path"]
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE sessions(id TEXT, status TEXT, owner_pid INTEGER, owner_identity TEXT)")
            conn.execute("INSERT INTO sessions VALUES('session','active',?,?)", (os.getpid(), process_identity(os.getpid())))
        with self.assertRaises(RuntimeError):
            self.store.restore(snapshot)
        with self.store.transaction() as conn:
            conn.execute("UPDATE sessions SET owner_identity='stale'")
        self.assertTrue(self.store.restore(snapshot)["restored"])

    def test_forget_scrubs_runtime_context_and_preserves_status(self):
        memory = self.store.memory_upsert("需要忘记的细节")
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE tasks(id TEXT, status TEXT, request TEXT NOT NULL, checkpoint_json TEXT NOT NULL)")
            conn.execute("INSERT INTO tasks VALUES('task','completed','需要忘记的细节','{}')")
        runtime = self.root / ".laowu/runtime/run1"
        runtime.mkdir(parents=True)
        (runtime / "prompt.txt").write_text("需要忘记的细节")
        self.store.memory_forget(memory["id"])
        self.assertFalse(runtime.exists())
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM tasks").fetchone()
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["request"], "")

    def test_pending_forget_replays_after_interrupted_transaction(self):
        memory = self.store.memory_upsert("unfinished deletion")
        self.store._write_ledger({memory["id"]}, pending=True)
        restarted = Store(self.root)
        self.assertEqual(restarted.memory_search(), [])
        self.assertFalse(json.loads(restarted.ledger_path.read_text())["pending"])

    def test_task_scope_is_limited_to_authenticated_task(self):
        self.store.memory_upsert("当前任务", scope="task:task_a")
        self.store.memory_upsert("另一任务", scope="task:task_b")
        self.assertEqual(self.store.memory_search(actor="engineer"), [])
        result = self.store.memory_search(actor="engineer", task_id="task_a")
        self.assertEqual([m["content"] for m in result], ["当前任务"])
        self.assertEqual(self.store.memory_search(actor="engineer", scope="task:task_b", task_id="task_a"), [])
        with self.assertRaises(PermissionError):
            self.store.memory_upsert("越权", scope="task:task_b", actor="engineer", task_id="task_a")
        self.store.memory_upsert("自己任务", scope="task:task_a", actor="engineer", task_id="task_a")

    def test_forget_cancels_interrupted_tasks_and_keeps_json_valid(self):
        fact = self.store.memory_upsert("尚未完成的敏感任务")
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE tasks(id TEXT, status TEXT, request TEXT NOT NULL, checkpoint TEXT NOT NULL, result TEXT, error TEXT)")
            conn.execute("INSERT INTO tasks VALUES('t','interrupted','秘密','{}','{}',NULL)")
        self.store.memory_forget(fact["id"])
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM tasks").fetchone()
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(row["error"], "forgotten_context")
            self.assertEqual(json.loads(row["checkpoint"]), {})
            self.assertIsNone(row["result"])

    def test_restore_does_not_adopt_source_processes(self):
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE sessions(id TEXT, status TEXT, owner_pid INTEGER, owner_identity TEXT)")
            conn.execute("INSERT INTO sessions VALUES('s','active',?,?)", (os.getpid(), process_identity(os.getpid())))
            conn.execute("CREATE TABLE runs(id TEXT,status TEXT,supervisor_pid INTEGER,supervisor_identity TEXT,child_pid INTEGER,child_identity TEXT,token_hash TEXT NOT NULL)")
            conn.execute("INSERT INTO runs VALUES('r','running',?,?,NULL,NULL,'oldtoken')", (os.getpid(), process_identity(os.getpid())))
            conn.execute("CREATE TABLE tasks(id TEXT,status TEXT)")
            conn.execute("INSERT INTO tasks VALUES('t','queued')")
        snapshot = self.store.backup()["path"]
        other = Store(Path(self.tmp.name) / "restored")
        other.restore(snapshot)
        with other.connect() as conn:
            session = conn.execute("SELECT * FROM sessions").fetchone()
            run = conn.execute("SELECT * FROM runs").fetchone()
            self.assertEqual(session["status"], "interrupted")
            self.assertIsNone(session["owner_pid"])
            self.assertEqual(run["status"], "interrupted")
            self.assertIsNone(run["supervisor_identity"])
            self.assertEqual(run["token_hash"], "")
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "interrupted")

    def test_forget_rejects_starting_run_before_supervisor_is_recorded(self):
        fact = self.store.memory_upsert("memory")
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE sessions(id TEXT,status TEXT,owner_pid INTEGER,owner_identity TEXT)")
            conn.execute("INSERT INTO sessions VALUES('s','active',?,?)", (os.getpid(), process_identity(os.getpid())))
            conn.execute("CREATE TABLE runs(id TEXT,status TEXT,session_id TEXT,supervisor_pid INTEGER,supervisor_identity TEXT)")
            conn.execute("INSERT INTO runs VALUES('r','starting','s',NULL,NULL)")
        with self.assertRaises(RuntimeError):
            self.store.memory_forget(fact["id"])

    def test_exact_phrase_ranks_before_partial_characters(self):
        self.store.memory_upsert("我住在重庆")
        self.store.memory_upsert("我的重任是为庆典做准备")
        self.assertEqual(self.store.memory_search("重庆")[0]["content"], "我住在重庆")


if __name__ == "__main__":
    unittest.main()
