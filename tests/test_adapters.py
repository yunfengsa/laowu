import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from laowu.adapters import (AdapterError, VERIFIED_CODEX_VERSION, _configured_mcp_names,
                            _probe, _validate_config, detect_clients, parse_event,
                            prepare_worker, render_project_configs)


class AdapterTests(unittest.TestCase):
    def test_non_ascii_venv_interpreter_uses_canonical_executable(self):
        from laowu.adapters import _command
        interpreter = self.root / '解释器'
        interpreter.symlink_to('/usr/bin/python3')
        command = _command(self.root, 'codex', str(interpreter))
        self.assertTrue(command[0].isascii())
        self.assertEqual(command[0], str(Path('/usr/bin/python3').resolve()))
        self.assertEqual(command[1], str(self.root / 'scripts/laowu.py'))
        render_project_configs(self.root, python=str(interpreter))
        hooks = json.loads((self.root / '.codex/hooks.json').read_text())['hooks']
        launch = shlex.split(hooks['SessionStart'][0]['hooks'][0]['command'])
        self.assertEqual(launch[0], command[0])
        self.assertNotIn(str(interpreter), (self.root / '.codex/config.toml').read_text())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "老五 项目 $(不会运行)"
        self.root.mkdir()
        self.workspace = self.root / "任务"
        self.workspace.mkdir()
        self.friend = {"id": "engineer", "name": "工程师", "version": 1,
                       "instructions": "核实后再报告。", "can_write": True,
                       "tools": ["read", "write", "shell", "memory", "checkpoint"],
                       "skills": [{"name": "testing", "content": "完成后运行相关测试。"}],
                       "mcp_servers": {}}
        self.probe = patch("laowu.adapters._probe", return_value={
            "path": "/bin/codex", "version": "1.2.3", "supported": True, "no_daemon": True})

    def launch(self, inherited_names=None, **kwargs):
        values = {"root": self.root, "friend": self.friend, "client": "codex", "run_id": "run_test",
                  "prompt": "PRIVATE PROMPT `no-shell`", "workspace": self.workspace, "worker_token": "test-token"}
        values.update(kwargs)
        with self.probe, patch("laowu.adapters._configured_mcp_names", return_value=inherited_names or set()), \
                patch("laowu.adapters._validate_config"):
            return prepare_worker(**values)

    def test_worker_keeps_login_and_secrets_out_of_launch_files(self):
        with patch.dict(os.environ, {"API_SECRET_EXAMPLE": "do-not-copy", "HOME": "/real-user"}):
            launch = self.launch()
        self.assertEqual(launch.argv[:5], ["/bin/codex", "--no-daemon", "exec", "--strict-config", "--json"])
        self.assertIn("--ephemeral", launch.argv)
        self.assertNotIn("--ignore-user-config", launch.argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", launch.argv)
        self.assertNotIn("HOME", launch.env)
        self.assertNotIn("API_SECRET_EXAMPLE", launch.env)
        self.assertNotIn(launch.stdin, launch.argv)
        self.assertEqual(launch.cwd, self.root / ".laowu/runtime/run_test")
        self.assertEqual(launch.cwd.stat().st_mode & 0o777, 0o700)
        for path in launch.cwd.rglob("*"):
            if path.is_file():
                content = path.read_text()
                self.assertNotIn("do-not-copy", content)
                self.assertNotIn(launch.stdin, content)

    def test_role_skills_mcp_and_authorized_workspace(self):
        launch = self.launch()
        self.assertIn("project_doc_max_bytes=0", launch.argv)
        self.assertIn("agents.enabled=false", launch.argv)
        self.assertIn("features.hooks=false", launch.argv)
        self.assertIn("features.plugins=false", launch.argv)
        self.assertIn("approval_policy=\"never\"", launch.argv)
        self.assertIn("workspace-write", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--add-dir") + 1], str(self.workspace))
        role = (launch.cwd / "LAOWU_FRIEND.md").read_text()
        self.assertIn("LAOWU_MANAGED_WORKER: 1", role)
        self.assertIn("完成后运行相关测试", role)
        mcp = next(item for item in launch.argv if item.startswith("mcp_servers="))
        self.assertIn("--worker-run", mcp)
        self.assertIn("test-token", mcp)
        self.assertIn("--root", mcp)

    def test_mcp_inherited_servers_disabled_and_approved_servers_enabled(self):
        friend = dict(self.friend, mcp_servers={"selected": {"command": "/bin/example", "enabled": False}})
        launch = self.launch(friend=friend, inherited_names={"private", "selected", "laowu"})
        mcp = next(item for item in launch.argv if item.startswith("mcp_servers="))
        self.assertIn('"private" = {"enabled" = false}', mcp)
        self.assertIn('"selected" = {"command" = "/bin/example", "enabled" = true}', mcp)
        self.assertIn('"enabled_tools" = ["memory_search", "task_checkpoint", "event_emit", "context_read"]', mcp)
        self.assertIn('"event_emit" = {"approval_mode" = "approve"}', mcp)
        self.assertIn('"memory_search" = {"approval_mode" = "approve"}', mcp)
        self.assertIn('"task_checkpoint" = {"approval_mode" = "approve"}', mcp)
        self.assertIn('"default_tools_approval_mode" = "prompt"', mcp)
        # Only the local restricted MCP receives application-managed preapproval.
        self.assertNotIn('"selected" = {"default_tools_approval_mode"', mcp)

    def test_preflight_failure_removes_incomplete_runtime(self):
        for function in ("_configured_mcp_names", "_validate_config"):
            with self.subTest(function=function), self.probe, \
                    patch("laowu.adapters._configured_mcp_names", return_value=set()), \
                    patch("laowu.adapters._validate_config"), \
                    patch("laowu.adapters." + function, side_effect=AdapterError("failed", "safe error")):
                with self.assertRaises(AdapterError):
                    prepare_worker(self.root, self.friend, "codex", "run_test", "private task", self.workspace)
                self.assertFalse((self.root / ".laowu/runtime/run_test").exists())

    def test_readonly_friend_does_not_get_write_directory(self):
        friend = dict(self.friend, can_write=False, tools=["memory", "checkpoint"])
        launch = self.launch(friend=friend)
        self.assertIn("read-only", launch.argv)
        self.assertNotIn("--add-dir", launch.argv)
        self.assertIn("features.shell_tool=false", launch.argv)
        self.assertIn('web_search="disabled"', launch.argv)

    def test_slack_research_worker_has_no_local_file_tools(self):
        friend = dict(self.friend, can_write=False, tools=['read', 'web', 'memory', 'checkpoint'])
        launch = self.launch(friend=friend, access_context={'audience': 'shared'})
        for feature in ('shell_tool', 'unified_exec', 'view_image', 'code_mode', 'computer_use'):
            self.assertIn('features.' + feature + '=false', launch.argv)
        self.assertIn('features.skip_host_skill_discovery=true', launch.argv)
        self.assertIn('web_search="live"', launch.argv)
        self.assertNotIn('--add-dir', launch.argv)
        with patch('laowu.slack_permissions.probe', return_value=False):
            with self.assertRaises(PermissionError):
                self.launch(access_context={'audience': 'shared'}, run_id='run_blocked')
        with patch('laowu.slack_permissions.probe', return_value=True):
            protected = self.launch(access_context={'audience': 'shared'}, run_id='run_protected')
        self.assertIn('default_permissions="laowu_slack"', protected.argv)
        self.assertNotIn('--sandbox', protected.argv)
        self.assertIn(str(self.workspace), protected.argv)
        self.assertIn('features.shell_tool=true', protected.argv)

    def test_unsupported_clients_fail_without_generating_configs(self):
        for client in ("claude", "gemini", "unknown"):
            with self.assertRaises(AdapterError) as error:
                self.launch(client=client)
            self.assertEqual(error.exception.code, "unsupported_client")
        self.assertFalse((self.root / ".laowu").exists())

    def test_invalid_profile_and_reused_run_fail_closed(self):
        with self.assertRaises(AdapterError):
            self.launch(run_id="../../escape")
        with self.assertRaises(AdapterError):
            self.launch(friend=dict(self.friend, can_write=False))
        self.assertFalse((self.root / ".laowu/runtime/run_test").exists())
        self.launch()
        with self.assertRaises(AdapterError) as error:
            self.launch()
        self.assertEqual(error.exception.code, "run_exists")

    def test_no_inline_mcp_secrets_or_unknown_configuration(self):
        extra = {"private": {"command": "server", "env": {"TOKEN": "private"}}}
        with self.assertRaises(AdapterError) as error:
            self.launch(friend=dict(self.friend, mcp_servers=extra))
        self.assertEqual(error.exception.code, "unsupported_mcp_config")
        self.assertFalse((self.root / ".laowu/runtime/run_test").exists())

    def test_parser_ignores_raw_tools_reasoning_and_errors(self):
        samples = [
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "secret"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private thought"}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": {"secret": "x"}}},
            {"type": "item.completed", "item": {"type": "agent_message", "phase": "commentary", "text": "internal"}},
        ]
        for value in samples:
            self.assertEqual(parse_event("codex", json.dumps(value)), {})
        self.assertEqual(parse_event("codex", "not json: secret"), {})
        self.assertEqual(parse_event("codex", "[]"), {})
        event = parse_event("codex", json.dumps({"type": "error", "message": "token=private"}))
        self.assertEqual(event, {"event": "error", "status": "failed", "error": "client_error",
                                 "terminal": False,
                                 "diagnostic": {"category": "unknown", "basis": "unclassified"}})

    def test_parser_returns_only_final_message_and_metadata(self):
        self.assertEqual(parse_event("codex", '{"type":"thread.started","thread_id":"thr_123"}'),
                         {"session_id": "thr_123"})
        self.assertEqual(parse_event("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'),
                         {"event": "message", "message": "done"})
        self.assertEqual(parse_event("codex", '{"type":"turn.completed","usage":{"secret":"x"}}'),
                         {"event": "result", "status": "completed"})
        self.assertEqual(parse_event("gemini", '{"response":"ignored"}'), {})

    def test_render_only_codex_and_quotes_absolute_paths(self):
        result = render_project_configs(self.root, python="/usr/bin/python3")
        self.assertEqual(set(result["clients"]), {"codex"})
        self.assertEqual(len(result["files"]), 2)
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / ".gemini").exists())
        hooks = json.loads((self.root / ".codex/hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "Interrupt"})
        command = shlex.split(hooks["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(command[1], str(self.root / "scripts/laowu.py"))
        self.assertIn(str(self.root), command)
        self.assertEqual(command[-1], "SessionStart")
        interrupt = shlex.split(hooks["Interrupt"][0]["hooks"][0]["command"])
        self.assertEqual(interrupt[-2:], ["--event", "Interrupt"])
        self.assertEqual(result["clients"]["codex"]["verified_version"], VERIFIED_CODEX_VERSION)
        self.assertIn('default_tools_approval_mode = "approve"', (self.root / ".codex/config.toml").read_text())

    def test_render_is_idempotent_and_preserves_extra_config(self):
        config = self.root / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "my-custom-model"\n[features]\nhooks = true\n')
        hooks = self.root / ".codex/hooks.json"
        unrelated = {"hooks": [{"type": "command", "command": "echo mine"}]}
        hooks.write_text(json.dumps({"hooks": {"Stop": [unrelated]}, "description": "mine"}))
        render_project_configs(self.root)
        before = (config.read_text(), hooks.read_text())
        render_project_configs(self.root)
        self.assertEqual(before, (config.read_text(), hooks.read_text()))
        self.assertTrue(config.read_text().startswith('model = "my-custom-model"'))
        self.assertIn(unrelated, json.loads(hooks.read_text())["hooks"]["Stop"])

    def test_conflict_does_not_partially_write(self):
        render_project_configs(self.root)
        path = self.root / ".codex/config.toml"
        path.write_text(path.read_text().replace('command = ', 'command = "tampered" # ', 1))
        old_hooks = (self.root / ".codex/hooks.json").read_text()
        with self.assertRaises(AdapterError) as error:
            render_project_configs(self.root, python="/different/python")
        self.assertEqual(error.exception.code, "config_conflict")
        self.assertEqual(old_hooks, (self.root / ".codex/hooks.json").read_text())

    def test_symlink_cannot_write_global_config(self):
        outside = Path(self.temporary.name) / "global"
        outside.mkdir()
        (self.root / ".codex").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AdapterError) as error:
            render_project_configs(self.root)
        self.assertEqual(error.exception.code, "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])


class ProbeTests(unittest.TestCase):
    def test_inventory_discards_server_urls_environment_and_errors(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([
            {"name": "selected", "env": {"TOKEN": "private"}, "url": "https://private.example"},
            {"name": "other", "enabled": False}]), "private stderr")
        with patch("subprocess.run", return_value=result):
            self.assertEqual(_configured_mcp_names("/bin/codex", Path("/tmp")), {"selected", "other"})
        for output in ('{"private":"token"}', '[{"name":"bad\\nname"}]', 'private error'):
            result.stdout = output
            with self.subTest(output=output), patch("subprocess.run", return_value=result):
                with self.assertRaises(AdapterError) as error:
                    _configured_mcp_names("/bin/codex", Path("/tmp"))
                self.assertEqual(error.exception.code, "mcp_inventory_failed")
                self.assertNotIn("private", str(error.exception))

    def test_strict_validation_uses_eof_without_model_turn_and_sanitizes_errors(self):
        result = subprocess.CompletedProcess([], 0, "private stdout", "private stderr")
        with patch("subprocess.run", return_value=result) as run:
            _validate_config("/bin/codex", Path("/tmp"), {"agents.enabled": False})
            self.assertEqual(run.call_args.args[0], ["/bin/codex", "app-server", "--strict-config", "--stdio",
                                                    "-c", "agents.enabled=false"])
            self.assertEqual(run.call_args.kwargs["input"], "")
        result.returncode = 1
        with patch("subprocess.run", return_value=result), self.assertRaises(AdapterError) as error:
            _validate_config("/bin/codex", Path("/tmp"), {})
        self.assertEqual(error.exception.code, "config_validation_failed")
        self.assertNotIn("private", str(error.exception))

    def test_strict_config_read_only_failure_has_safe_diagnostic(self):
        result = subprocess.CompletedProcess([], 1, "", "Read-only file system (os error 30): PRIVATE_PATH token=PRIVATE")
        with patch("subprocess.run", return_value=result), self.assertRaises(AdapterError) as error:
            _validate_config("/bin/codex", Path("/tmp"), {})
        self.assertEqual(error.exception.diagnostics,
                         {"source": "config_validation", "exit_code": 1,
                          "category": "filesystem_read_only", "basis": "message_pattern"})
        self.assertNotIn("PRIVATE", str(error.exception) + json.dumps(error.exception.diagnostics))

    def test_probe_distinguishes_verified_version_from_compatible_flags(self):
        for version in (VERIFIED_CODEX_VERSION, "0.999.0"):
            def run(argv, **kwargs):
                if argv[-1] == "--version":
                    output = "codex-cli " + version
                elif "exec" in argv:
                    output = "--json --ephemeral --sandbox --skip-git-repo-check --add-dir --strict-config"
                else:
                    output = "--no-daemon"
                return subprocess.CompletedProcess(argv, 0, output, "")
            with self.subTest(version=version), patch("shutil.which", return_value="/bin/codex"), \
                    patch("subprocess.run", side_effect=run):
                info = _probe("codex")
                self.assertTrue(info["supported"])
                self.assertEqual(info["verified_version"], version == VERIFIED_CODEX_VERSION)

    def test_probe_requires_direct_process_flag(self):
        def run(argv, **kwargs):
            if argv[-1] == "--version":
                output = "codex-cli 0.141.0\n"
            elif "exec" in argv:
                output = "--json --ephemeral --sandbox --skip-git-repo-check --add-dir --strict-config"
            else:
                output = "no process control flag"
            return subprocess.CompletedProcess(argv, 0, output, "secret stderr")
        with patch("shutil.which", return_value="/bin/codex"), patch("subprocess.run", side_effect=run):
            info = _probe("codex")
        self.assertFalse(info["supported"])
        self.assertEqual(info["error"], "unsupported_client")
        self.assertNotIn("secret", json.dumps(info))

    def test_missing_client_never_reads_credentials(self):
        with patch("shutil.which", return_value=None), patch("subprocess.run") as run:
            self.assertEqual(detect_clients()["codex"]["error"], "client_missing")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
