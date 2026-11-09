"""Bidirectional protocol checks without a provider/model call."""
import json
import contextlib
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from laowu.slack_codex import SlackCodexError, SlackCodexSession


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, limit):
        return self.lines.get(timeout=5)

    def emit(self, message):
        self.lines.put(json.dumps(message, ensure_ascii=False) + "\n")

    def close(self):
        self.lines.put("")


class FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, value):
        message = json.loads(value)
        self.process.sent.append(message)
        if "method" in message and "id" in message:
            if self.process.handle:
                result = self.process.handle(message)
                if result is not None:
                    self.process.stdout.emit({"id": message["id"], "result": result})
            else:
                if message["method"] == "initialize":
                    result = {"userAgent": "fake"}
                elif message["method"] in {"thread/start", "thread/resume"}:
                    result = {"thread": {"id": "native-thread"}}
                elif message["method"] == "turn/start":
                    result = {"turn": {"id": "native-turn"}}
                else:
                    result = {}
                self.process.stdout.emit({"id": message["id"], "result": result})
        return len(value)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    pid = None

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.sent = []
        self.handle = None
        self.exited = False

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.exited = True
        self.stdout.lines.put("")

    kill = terminate

    def wait(self, timeout=None):
        return 0


class SlackCodexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="slack-codex-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "老五 测试"
        self.root.mkdir()
        for relative in ("AGENTS.md", "docs/persona.md", ".agents/skills/laowu-memory/SKILL.md",
                         ".agents/skills/laowu-delegate/SKILL.md", ".agents/skills/laowu-background/SKILL.md",
                         ".agents/skills/laowu-maintain/SKILL.md"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("完整规则 " + relative, encoding="utf-8")
        self.process = None

    def create(self, **kwargs):
        def spawn(*args, **options):
            self.process = FakeProcess(*args, **options)
            return self.process
        session = SlackCodexSession(self.root, self.root / "principal.secret", popen=spawn,
                                    executable="/fake/codex", inventory=lambda: {"unexpected", "laowu"}, **kwargs)
        self.addCleanup(session.close)
        return session

    def test_turn_admission_precedes_transport_and_releases_before_response_wait(self):
        session = self.create()
        session.start()
        sent = []
        @contextlib.contextmanager
        def admission():
            yield
            # Response is produced only when the guard has exited. Holding the
            # lock during request.wait would time out instead of returning.
            self.process.stdout.emit({'id': sent[0]['id'], 'result': {'turn': {'id': 'admitted'}}})
        session.turn_admission = admission
        self.process.handle = lambda message: sent.append(message)
        self.assertEqual(session.start_turn('synthetic', 'guarded'), 'admitted')
        self.assertEqual(len(sent), 1)
        @contextlib.contextmanager
        def blocked():
            from laowu.processing import ProcessingPaused
            raise ProcessingPaused('forget_pending')
            yield
        session.turn_admission = blocked
        with self.assertRaisesRegex(RuntimeError, 'forget_pending'):
            session.start_event({'id': 'blocked-event'})
        self.assertEqual(len(sent), 1)

    def test_start_isolated_persistent_with_complete_identity_and_filtered_environment(self):
        session = self.create()
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-secret", "LAOWU_SLACK_CREDENTIALS": "private",
                                    "ODD_TOKEN": "xapp-also-secret", "LAOWU_ROLE": "worker",
                                    "PROVIDER_API_KEY": "provider-kept"}):
            self.assertEqual(session.start(), "native-thread")
        argv = self.process.args[0]
        self.assertEqual(argv[:5], ["/fake/codex", "--no-daemon", "app-server", "--strict-config", "--stdio"])
        self.assertNotIn("--ephemeral", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertIn("features.default_mode_request_user_input=true", argv)
        self.assertIn("features.code_mode_host=true", argv)
        for feature in ("shell_tool", "plugins", "apps", "hooks", "memories", "view_image"):
            self.assertIn("features." + feature + "=false", argv)
        role = next(value for value in argv if value.startswith("developer_instructions="))
        self.assertIn("完整规则 docs/persona.md", role)
        self.assertIn("完整规则 .agents/skills/laowu-background/SKILL.md", role)
        mcp = next(value for value in argv if value.startswith("mcp_servers="))
        self.assertIn('"unexpected" = {"enabled" = false}', mcp)
        self.assertIn("--slack-token-file", mcp)
        self.assertIn('"required" = true', mcp)
        self.assertIn("principal.secret", mcp)
        env = self.process.kwargs["env"]
        self.assertEqual(env["PROVIDER_API_KEY"], "provider-kept")
        for key in ("SLACK_BOT_TOKEN", "LAOWU_SLACK_CREDENTIALS", "ODD_TOKEN", "LAOWU_ROLE"):
            self.assertNotIn(key, env)
        self.assertTrue(self.process.kwargs["start_new_session"])
        start = next(m for m in self.process.sent if m.get("method") == "thread/start")
        self.assertIs(start["params"]["ephemeral"], False)
        self.assertNotIn("model", start["params"])
        self.assertEqual(session.start(), "native-thread")

    def test_resume_does_not_start_new_thread_or_replay_any_turn(self):
        session = self.create(thread_id="native-thread")
        session.start()
        methods = [m.get("method") for m in self.process.sent]
        self.assertIn("thread/resume", methods)
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)

    def test_turn_input_and_runtime_host_id_do_not_rewrite_user_text(self):
        session = self.create()
        session.start()
        self.assertEqual(session.start_turn("你好 <root>引用</root>", "message-1", "来源资料"), "native-turn")
        message = self.process.sent[-1]["params"]
        self.assertEqual(message["input"][0]["text"], "你好 <root>引用</root>")
        self.assertEqual(message["clientUserMessageId"], "message-1")
        self.assertIn("native-thread", message["additionalContext"]["laowu_runtime"]["value"])
        self.assertEqual(message["additionalContext"]["laowu_context"]["kind"], "untrusted")

    def test_gateway_source_is_application_context_and_history_prompt_is_loaded(self):
        session = self.create()
        session.start()
        session.slack_context = {'channel_id': 'CA', 'reply_thread_ts': '1.000001', 'context': 'normal'}
        session.start_turn('前面说了什么', 'message-context')
        context = self.process.sent[-1]['params']['additionalContext']['slack_source']
        self.assertEqual(context['kind'], 'application')
        self.assertIn('1.000001', context['value'])
        self.assertIn('slack_history', context['value'])
        instructions = next(v for v in self.process.args[0] if v.startswith('developer_instructions='))
        self.assertIn('slack_thread/slack_history', instructions)
        self.assertIn('不可信参考', instructions)

    def test_live_receipt_uses_final_events_before_history_materializes(self):
        done = threading.Event()
        session = self.create(on_notification=lambda event: done.set() if event.get("method") == "turn/completed" else None)
        session.start()
        turn_id = session.start_turn("测试", "message-1")
        session.request = Mock(side_effect=AssertionError("live receipt must not request history"))
        self.assertEqual(session.history_receipt("message-1", turn_id)["status"], "submitted")
        self.process.stdout.emit({"method": "item/completed", "params": {
            "threadId": "native-thread", "turnId": turn_id,
            "item": {"id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "不返回"}}})
        self.process.stdout.emit({"method": "item/completed", "params": {
            "threadId": "native-thread", "turnId": turn_id,
            "item": {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": "最终结果"}}})
        self.process.stdout.emit({"method": "turn/completed", "params": {
            "threadId": "native-thread", "turn": {"id": turn_id, "status": "completed", "items": []}}})
        self.assertTrue(done.wait(1))
        self.assertEqual(session.history_receipt("message-1"),
                         {"status": "completed", "turn_id": turn_id, "final_text": "最终结果"})
        session.request.assert_not_called()

    def test_lost_start_response_still_matches_live_started_event_client_identity(self):
        session = self.create()
        session.start()
        def lost(message):
            self.process.stdout.emit({"method": "turn/started", "params": {
                "threadId": "native-thread", "turn": {"id": "accepted-turn", "status": "inProgress", "items": []}}})
        self.process.handle = lost
        request = session.request
        session.request = lambda method, params=None, timeout=30: request(method, params, timeout=0.03)
        with self.assertRaises(TimeoutError):
            session.start_turn("测试", "message-lost")
        self.assertEqual(session.history_receipt("message-lost"),
                         {"status": "submitted", "turn_id": "accepted-turn", "final_text": ""})
        self.assertEqual(sum(m.get("method") == "turn/start" for m in self.process.sent), 1)

    def test_foreign_thread_notification_cannot_complete_current_turn(self):
        done = threading.Event()
        session = self.create(on_notification=lambda event: done.set())
        session.start()
        turn_id = session.start_turn("测试", "message-1")
        self.process.stdout.emit({"method": "turn/completed", "params": {
            "threadId": "different-thread", "turn": {"id": turn_id, "status": "completed", "items": [
                {"type": "agentMessage", "phase": "final_answer", "text": "不可混入"}]}}})
        self.assertTrue(done.wait(1))
        self.assertEqual(session.history_receipt("message-1")["status"], "submitted")

    def test_unknown_history_rpc_failure_remains_uncertain_not_absent(self):
        session = self.create(thread_id="native-thread")
        session.request = Mock(side_effect=SlackCodexError("client_error", "固定诊断"))
        self.assertEqual(session.history_receipt("lost-message"),
                         {"status": "uncertain", "turn_id": None, "final_text": ""})
        self.assertEqual(session.request.call_count, 1)
        session.request = Mock(side_effect=SlackCodexError("client_authentication", "固定诊断"))
        with self.assertRaises(SlackCodexError):
            session.history_receipt("lost-message")

    def test_background_event_is_retained_tool_output_without_fake_user_message(self):
        session = self.create()
        session.start()
        session.start_event({"id": "notice-1", "topic": "task.completed", "message": "已完成", "context": "root"})
        params = self.process.sent[-1]["params"]
        self.assertEqual(params["input"], [])
        self.assertEqual(params["turnTrigger"], "laowu_event")
        self.assertTrue(params["toolOutput"]["retain"])
        payload = json.loads(params["toolOutput"]["output"])
        self.assertEqual(payload["event_id"], "notice-1")
        self.assertEqual(payload["context"], "root")

    def test_server_request_using_client_request_id_is_separate_and_reply_is_once(self):
        seen, done = [], threading.Event()
        session = self.create()
        def on_request(message):
            seen.append(message)
            self.assertTrue(session.reply(message["id"], {"decision": "decline"}))
            self.assertFalse(session.reply(message["id"], {"decision": "accept"}))
            done.set()
        session.on_request = on_request
        session.start()
        def respond(message):
            self.process.stdout.emit({"id": message["id"], "method": "item/commandExecution/requestApproval", "params": {}})
            return {"notAnApproval": True}
        self.process.handle = respond
        self.assertEqual(session.request("probe"), {"notAnApproval": True})
        self.assertTrue(done.wait(1))
        self.assertEqual(len(seen), 1)
        responses = [m for m in self.process.sent if "result" in m]
        self.assertEqual(responses[-1]["result"], {"decision": "decline"})

    def test_callback_can_call_request_without_blocking_reader(self):
        done = threading.Event()
        session = self.create()
        def callback(message):
            self.assertEqual(session.request("ping"), {})
            done.set()
        session.on_notification = callback
        session.start()
        self.process.stdout.emit({"method": "turn/completed", "params": {"turn": {"id": "t"}}})
        self.assertTrue(done.wait(1))

    def test_concurrent_rpcs_accept_responses_in_reverse_order(self):
        session = self.create()
        session.start()
        messages, results = [], {}
        def collect(message):
            messages.append(message)
            if len(messages) == 2:
                for item in reversed(messages):
                    self.process.stdout.emit({"id": item["id"], "result": {"method": item["method"]}})
        self.process.handle = collect
        workers = [threading.Thread(target=lambda name=name: results.update({name: session.request(name)}))
                   for name in ("one", "two")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, {"one": {"method": "one"}, "two": {"method": "two"}})

    def test_provider_error_notifications_are_classified_without_raw_messages(self):
        messages, done = [], threading.Event()
        def callback(message):
            messages.append(message)
            done.set()
        session = self.create(on_notification=callback)
        session.start()
        self.process.stdout.emit({"method": "error", "params": {
            "threadId": "native-thread", "turnId": "turn-1", "willRetry": False,
            "error": {"message": "Unauthorized xoxb-secret provider body", "httpStatusCode": 401}}})
        self.assertTrue(done.wait(1))
        self.assertNotIn("xoxb-secret", json.dumps(messages))
        self.assertEqual(messages[0]["params"]["error"]["diagnostic"]["category"], "authentication")

    def test_missing_request_handler_fails_closed_instead_of_autoapproving(self):
        session = self.create()
        session.start()
        self.process.stdout.emit({"id": 99, "method": "item/permissions/requestApproval", "params": {}})
        for _ in range(20):
            if any(m.get("id") == 99 and "error" in m for m in self.process.sent):
                break
            threading.Event().wait(0.01)
        self.assertTrue(any(m.get("id") == 99 and "error" in m for m in self.process.sent))

    def test_eof_wakes_pending_requests_and_redacts_provider_error(self):
        session = self.create()
        session.start()
        def reject(message):
            self.process.stdout.emit({"id": message["id"], "error": {"message": "xoxb-private provider body"}})
        self.process.handle = reject
        with self.assertRaises(SlackCodexError) as raised:
            session.request("probe")
        self.assertNotIn("xoxb", str(raised.exception))
        def disconnect(message):
            self.process.stdout.lines.put("")
        self.process.handle = disconnect
        with self.assertRaises(SlackCodexError):
            session.request("probe", timeout=1)

    def test_response_followed_by_eof_remains_a_confirmed_response(self):
        session = self.create()
        session.start()
        def respond_then_close(message):
            self.process.stdout.emit({"id": message["id"], "result": {"confirmed": True}})
            self.process.stdout.lines.put("")
        self.process.handle = respond_then_close
        self.assertEqual(session.request("probe"), {"confirmed": True})

    def test_timeout_is_not_retried_and_late_response_does_not_satisfy_next_request(self):
        session = self.create()
        session.start()
        self.process.handle = lambda message: None
        with self.assertRaises(TimeoutError):
            session.request("notRetried", timeout=0.01)
        old = self.process.sent[-1]
        self.process.stdout.emit({"id": old["id"], "result": {"wrong": True}})
        self.process.handle = lambda message: {"right": True}
        self.assertEqual(session.request("next"), {"right": True})
        self.assertEqual(sum(m.get("method") == "notRetried" for m in self.process.sent), 1)

    def test_invalid_protocol_fails_closed(self):
        session = self.create()
        session.start()
        self.process.handle = lambda message: self.process.stdout.lines.put('{"id": []}\n')
        with self.assertRaises(SlackCodexError):
            session.request("probe", timeout=1)

    def test_receipt_matches_user_client_id_and_only_returns_final(self):
        session = self.create(thread_id="native-thread")
        session.request = Mock(side_effect=[{"data": [], "nextCursor": "second"}, {"data": [{
            "id": "turn-1", "status": "completed", "items": [
                {"type": "userMessage", "clientId": "message-1"},
                {"type": "agentMessage", "phase": "commentary", "text": "内部进度"},
                {"type": "agentMessage", "phase": "final_answer", "text": "最后结果"}]}]}])
        self.assertEqual(session.history_receipt("message-1"),
                         {"status": "completed", "turn_id": "turn-1", "final_text": "最后结果"})
        self.assertEqual(session.request.call_args.args[0], "thread/turns/list")
        self.assertEqual(session.request.call_args.args[1]["cursor"], "second")

    def test_event_receipt_and_unknown_history_are_bounded(self):
        session = self.create(thread_id="native-thread")
        marker = {"type": "functionCallOutput", "name": "background_event", "output": '{"event_id":"n"}'}
        session.request = Mock(return_value={"data": [{"id": "t", "status": "inProgress", "items": [marker]}]})
        self.assertEqual(session.history_receipt("n")["status"], "submitted")
        session.request = Mock(return_value={"data": [], "nextCursor": "keeps-going"})
        self.assertEqual(session.history_receipt("missing")["status"], "uncertain")
        self.assertEqual(session.request.call_count, 10)

    def test_receipt_completed_without_text_is_still_a_completed_turn(self):
        session = self.create(thread_id="native-thread")
        session.request = Mock(return_value={"data": [{"id": "t", "status": "completed", "items": []}]})
        self.assertEqual(session.history_receipt("m", "t"),
                         {"status": "completed", "turn_id": "t", "final_text": ""})

    def test_close_interrupts_process_and_future_requests(self):
        session = self.create()
        session.start()
        session.close()
        self.assertTrue(self.process.exited)
        with self.assertRaises(SlackCodexError):
            session.request("ping")
        self.assertFalse(session.reply("expired", {}))

    def test_missing_persona_never_spawns(self):
        (self.root / "docs/persona.md").unlink()
        session = self.create()
        with self.assertRaises(SlackCodexError):
            session.start()
        self.assertIsNone(self.process)


if __name__ == "__main__":
    unittest.main()
