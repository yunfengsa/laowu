import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest

from laowu.hooks import handle
from laowu.service import Service
from laowu.store import Store


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="老五钩子-")
        self.root = Path(self.temp.name)
        self.service = Service(self.root)
        self.host = "host-one"
        self.service.call("bootstrap", {"host_session_id": self.host})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def hook(self, event, **extra):
        return handle(self.root, "codex", event, {"session_id": self.host, **extra})

    def test_start_binds_explicit_session_without_recording_transcript(self):
        result = self.hook("SessionStart", transcript_path="/a/private/transcript")
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.host, text)
        self.assertNotIn("/a/private/transcript", text)

    def test_stop_reminds_once_and_checkpoint_satisfies_turn(self):
        self.hook("UserPromptSubmit", prompt="不要额外保存这段聊天原文")
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        self.service.call("session_checkpoint", {"summary": "本轮没有需要长期记住的新内容"})
        self.assertEqual(self.hook("Stop"), {})
        self.hook("UserPromptSubmit", prompt="第二轮")
        self.assertEqual(self.hook("Stop")["decision"], "block")
        with self.service.store.connect() as db:
            self.assertNotIn("不要额外保存这段聊天原文", " ".join(db.iterdump()))

    def test_end_only_closes_matching_host(self):
        other = Service(self.root)
        try:
            other.call("bootstrap", {"host_session_id": "host-two"})
            self.hook("SessionEnd")
            with self.service.store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM sessions WHERE id=?", (self.service.manager.id,)).fetchone()[0], "closed")
                self.assertEqual(db.execute("SELECT status FROM sessions WHERE id=?", (other.manager.id,)).fetchone()[0], "active")
        finally:
            other.close()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="老五协议-")
        self.root = Path(self.temp.name)
        script = Path(__file__).resolve().parents[1] / "scripts" / "laowu.py"
        self.process = subprocess.Popen([sys.executable, str(script), "--root", str(self.root), "mcp"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.request_id = 0

    def tearDown(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdout.close()
        self.process.stderr.close()
        self.temp.cleanup()

    def rpc(self, method, params=None):
        self.request_id += 1
        data = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params or {}}
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        self.process.stdin.flush()
        self.assertTrue(self.selector.select(timeout=5), "MCP 未及时回复")
        response = json.loads(self.process.stdout.readline())
        self.assertEqual(response["id"], self.request_id)
        return response

    def tool(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def test_initialize_roundtrip_persistence_and_no_extra_stdout(self):
        initialized = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("memory_upsert", names)
        saved = self.tool("memory_upsert", {"content": "测试用户喜欢清晨散步"})
        self.assertFalse(saved["isError"])
        found = self.tool("memory_search", {"query": "清晨"})
        self.assertIn("清晨散步", found["content"][0]["text"])
        self.assertTrue(self.tool("memory_search", {"actor": "confidant"})["isError"])
        self.assertEqual(self.rpc("ping")["result"], {})
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.assertEqual(self.process.returncode, 0)
        self.assertEqual(len(Store(self.root).memory_search("清晨")), 1)
        with Store(self.root).connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE status='active'").fetchone()[0], 0)

    def test_live_mcp_bootstrap_recovers_manager_closed_by_session_hook(self):
        initial = json.loads(self.tool('bootstrap', {'host_session_id': 'recovery-host'})['content'][0]['text'])
        handle(self.root, 'codex', 'SessionEnd', {'session_id': 'recovery-host'})
        # Same stdio process still answers, but must establish a new manager.
        recovered = self.tool('bootstrap', {})
        self.assertFalse(recovered['isError'])
        data = json.loads(recovered['content'][0]['text'])
        self.assertNotEqual(data['session_id'], initial['session_id'])
        self.assertEqual(data['manager_recovery']['previous_status'], 'closed')
        again = json.loads(self.tool('bootstrap', {})['content'][0]['text'])
        self.assertEqual(again['session_id'], data['session_id'])
        with Store(self.root).connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE status='active'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
