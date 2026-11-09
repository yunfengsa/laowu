"""Named profile generation and optional real no-model sandbox acceptance."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from laowu import slack_permissions as permissions


class SlackPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="slack-permissions-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "老五 中文"
        self.root.mkdir()
        (self.root / ".laowu").mkdir()
        self.workspace = self.root / "工作目录"
        self.workspace.mkdir()
        permissions._CACHE.clear()

    def test_profile_exact_carveouts_and_write_without_legacy_sandbox(self):
        values = permissions.profile_overrides(self.root, self.workspace, True)
        self.assertEqual(values["default_permissions"], "laowu_slack")
        self.assertNotIn("sandbox_mode", values)
        fs = values["permissions.laowu_slack.filesystem"]
        self.assertEqual(fs[":root"], "deny")
        self.assertEqual(fs[":minimal"], "read")
        self.assertEqual(fs[str(self.root)], "read")
        self.assertEqual(fs[str(self.workspace)], "write")
        self.assertEqual(fs[str(self.root / "config")], "read")
        for path in (self.root / ".laowu", Path.home() / ".ssh",
                     Path.home() / ".aws", Path.home() / ".config", Path("/proc")):
            self.assertEqual(fs[str(path)], "deny")
        self.assertEqual(fs[str(Path.home() / ".codex/packages")], "read")
        self.assertEqual(fs[str(Path.home() / ".codex/auth.json")], "deny")
        self.assertNotIn(str(Path.home() / ".codex"), fs)
        self.assertIs(values["permissions.laowu_slack.network.enabled"], False)
        env = values["shell_environment_policy"]
        self.assertEqual(env["inherit"], "none")
        self.assertNotIn("OPENAI_API_KEY", env["set"])

    def test_root_workspace_has_write_but_private_subtree_still_denied(self):
        values = permissions.profile_overrides(self.root, self.root, True)
        fs = values["permissions.laowu_slack.filesystem"]
        self.assertEqual(fs[str(self.root)], "write")
        self.assertEqual(fs[str(self.root / ".laowu")], "deny")
        self.assertEqual(fs[str(self.root / "config")], "read")

    def test_policy_directory_cannot_receive_a_nested_workspace_write_grant(self):
        directory = self.root / "config"
        nested = directory / "nested"
        nested.mkdir(parents=True)
        alias = self.root / "config-alias"
        alias.symlink_to(directory, target_is_directory=True)
        for workspace in (directory, nested, alias):
            with self.subTest(workspace=workspace), self.assertRaises(PermissionError):
                permissions.profile_overrides(self.root, workspace, True)
        values = permissions.profile_overrides(self.root, nested, False)
        self.assertEqual(values["permissions.laowu_slack.filesystem"][str(nested)], "read")

    def test_other_project_config_remains_an_ordinary_writable_workspace(self):
        other = self.root.parent / "other-project"
        config = other / "config"
        config.mkdir(parents=True)
        for workspace in (other, config):
            fs = permissions.profile_overrides(self.root, workspace, True)["permissions.laowu_slack.filesystem"]
            self.assertEqual(fs[str(workspace)], "write")
            self.assertEqual(fs[str(self.root / "config")], "read")
            if workspace == other:
                self.assertNotIn(str(config), fs)

    def test_readonly_profile_does_not_grant_workspace_write(self):
        values = permissions.profile_overrides(self.root, self.workspace, False)
        self.assertEqual(values["permissions.laowu_slack.filesystem"][str(self.workspace)], "read")

    def test_broad_private_and_symlink_workspaces_are_rejected(self):
        link = self.root / "private-link"
        link.symlink_to(self.root / ".laowu", target_is_directory=True)
        for workspace in ("/", Path.home(), "/tmp", self.root / ".laowu", link):
            with self.subTest(workspace=str(workspace)):
                with self.assertRaises(PermissionError):
                    permissions.profile_overrides(self.root, workspace, True)

    def test_custom_codex_home_is_denied_with_only_packages_readable(self):
        home = self.root.parent / "custom-codex"
        home.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            values = permissions.profile_overrides(self.root, self.workspace, True)
        fs = values["permissions.laowu_slack.filesystem"]
        self.assertNotIn(str(home), fs)
        self.assertEqual(fs[str(home / "auth.json")], "deny")
        self.assertEqual(fs[str(home / "packages")], "read")

    def test_probe_success_cache_is_version_path_and_time_bounded(self):
        with patch.object(permissions.shutil, "which", return_value="/bin/codex"), \
                patch.object(permissions, "_version", return_value=["0.163.0", "/bin/codex", 10, 1]) as version, \
                patch.object(permissions, "_run_probe", return_value=True) as run, \
                patch.object(permissions.time, "monotonic", return_value=100) as clock:
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertEqual(run.call_count, 1)
            (self.root / ".codex").mkdir()
            (self.root / ".codex/config.toml").write_text('model="changed"\n')
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertEqual(run.call_count, 2)
            clock.return_value = 401
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertEqual(run.call_count, 3)
            version.return_value = ["0.164.0", "/bin/codex", 20, 2]
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertEqual(run.call_count, 4)
            second = self.root / "second-workspace"
            second.mkdir()
            self.assertTrue(permissions.probe(self.root, second))
            self.assertEqual(run.call_count, 5)

    def test_failure_and_missing_client_fail_closed(self):
        with patch.object(permissions.shutil, "which", return_value=None):
            self.assertFalse(permissions.probe(self.root, self.workspace))
        with patch.object(permissions.shutil, "which", return_value="/bin/codex"), \
                patch.object(permissions, "_version", return_value=None):
            self.assertFalse(permissions.probe(self.root, self.workspace))
        with patch.object(permissions.shutil, "which", return_value="/bin/codex"), \
                patch.object(permissions, "_version", return_value=["0.163.0"]), \
                patch.object(permissions, "_run_probe", side_effect=PermissionError("not exported")):
            self.assertFalse(permissions.probe(self.root, self.workspace))

    def test_fake_sandbox_contract_and_probe_artifacts_are_removed(self):
        home = self.root.parent / "synthetic-home"
        home.mkdir()
        for name in (".codex", ".ssh", ".aws", ".config"):
            (home / name).mkdir()
        with patch.object(permissions.Path, "home", return_value=home), \
                patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}), \
                patch.object(permissions.shutil, "which", return_value="/fake/codex"), \
                patch.object(permissions, "_version", return_value=["0.163.0"]), \
                patch.object(permissions.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "laowu-sandbox-probe:ok\n", "")
            self.assertTrue(permissions.probe(self.root, self.workspace))
            self.assertTrue(permissions.probe(self.root, self.root))
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["/fake/codex", "--no-daemon", "sandbox", "-P"])
            self.assertIn("-I", command)
            self.assertNotIn("exec", command)
            fs_override = next(value for value in command if value.startswith("permissions.laowu_slack.filesystem="))
            self.assertIn('"' + str(self.root / "config") + '" = "read"', fs_override)
            self.assertIn('"' + str(self.root) + '" = "write"', fs_override)
            self.assertEqual(run.call_args.kwargs["env"]["OPENAI_API_KEY"], "probe-only-not-a-secret")
            permissions._CACHE.clear()
            run.return_value = subprocess.CompletedProcess([], 1, "laowu-sandbox-probe:failed\n", "")
            self.assertFalse(permissions.probe(self.root, self.workspace))
            permissions._CACHE.clear()
            run.side_effect = subprocess.TimeoutExpired(command, 30)
            self.assertFalse(permissions.probe(self.root, self.workspace))
        self.assertEqual(list(self.root.parent.rglob(".laowu-*-probe-*")), [])

    @unittest.skipUnless(os.environ.get("LAOWU_RUN_SANDBOX_PROBE") == "1" and shutil.which("codex"),
                         "set LAOWU_RUN_SANDBOX_PROBE=1 for real no-model host verification")
    def test_live_sandbox_write_and_secret_symlink_environment_denials(self):
        self.assertTrue(permissions.probe(self.root, self.workspace))


if __name__ == "__main__":
    unittest.main()
