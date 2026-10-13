"""验证模型隔离、检查失败和超时收尾；不联网调用 ZCode。"""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/zcode_delegate.py"
spec = importlib.util.spec_from_file_location("delegate", SCRIPT)
delegate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegate)
PROVIDER = "account:bigmodel-individual-coding-plan"


class DelegateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.builtin = self.root / "builtin.json"
        self.builtin.write_text(json.dumps({"schemaVersion": 1, "config": {
            "providerConfigRules": {"providerRules": [
                {"providerId": PROVIDER, "config": {"builtinModelIds": ["GLM-5.3", "GLM-5.3-Flash"]}},
                {"providerId": "unwanted", "config": {"builtinModelIds": ["Other"]}},
            ]},
        }}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_model_isolation_and_cleanup_preserve_source(self):
        original = self.builtin.read_bytes()
        with delegate.model_environment("GLM-5.3-Flash", PROVIDER, self.builtin) as env:
            path = Path(env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"])
            rules = json.loads(path.read_text())["config"]["providerConfigRules"]["providerRules"]
            self.assertEqual([(r["providerId"], r["config"]["builtinModelIds"]) for r in rules],
                             [(PROVIDER, ["GLM-5.3-Flash"])])
            personal = json.loads(Path(env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]).read_text())
            self.assertEqual(personal["config"]["defaultModelSelection"]["options"], {"reasoningLevel": "high"})
        self.assertFalse(path.exists())
        self.assertEqual(self.builtin.read_bytes(), original)

    def test_unavailable_model_is_rejected(self):
        with self.assertRaises(ValueError):
            with delegate.model_environment("Other", PROVIDER, self.builtin):
                self.fail("unavailable model was accepted")

    def test_invalid_check_is_rejected(self):
        p = self.root / "checks.json"
        p.write_text('{"checks": [[""]]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            delegate.load_checks(p)

    def test_control_logs_are_ignored_without_overwriting_local_rules(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        exclude = self.root / ".git/info/exclude"
        exclude.write_text("# existing rule\nlocal-only.txt", encoding="utf-8")
        delegate.exclude_control_directory(self.root)
        original = exclude.read_bytes()
        delegate.exclude_control_directory(self.root)
        self.assertEqual(exclude.read_bytes(), original)
        self.assertTrue(exclude.read_text().startswith("# existing rule\nlocal-only.txt\n"))
        result = subprocess.run(["git", "check-ignore", ".zcode-control/runs/private.log"],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_tracked_control_directory_is_rejected(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        directory = self.root / ".zcode-control"
        directory.mkdir()
        (directory / "task.md").write_text("private task", encoding="utf-8")
        subprocess.run(["git", "add", ".zcode-control/task.md"], cwd=self.root, check=True)
        with self.assertRaises(ValueError):
            delegate.exclude_control_directory(self.root)

    def test_linked_worktree_uses_common_local_exclude(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "fixture"], cwd=self.root, check=True)
        worktree = self.root / "linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "fixture", str(worktree)],
                       cwd=self.root, check=True)
        delegate.exclude_control_directory(worktree)
        result = subprocess.run(["git", "check-ignore", ".zcode-control/task.md"],
                                cwd=worktree, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_timeout_and_worker_failure_are_reported(self):
        result = delegate.run_logged([sys.executable, "-c", "import time; time.sleep(20)"],
                                     self.root, self.root, "slow", 0.1)
        self.assertEqual(result["status"], "timed_out")
        failed = delegate.run_logged([sys.executable, "-c", "raise SystemExit(7)"],
                                     self.root, self.root, "fail", 5)
        self.assertEqual(failed["exit_code"], 7)

    def run_fake_assignment(self, checks=None, dirty=False, staged=False,
                            model="GLM-5.3-Flash", metadata=True):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        task = self.root / "task.md"
        task.write_text("Do a bounded task", encoding="utf-8")
        fake = self.root / "fake-zcode"
        fake.write_text(f'#!{sys.executable}\nprint(\'{{"sessionId": "fake-session", "response": "done"}}\')\n', encoding="utf-8")
        fake.chmod(0o755)
        storage = self.root / "storage"
        database = storage / "cli/db/db.sqlite"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE session_entry (session_id TEXT, data TEXT)")
            connection.execute("INSERT INTO session_entry VALUES (?, ?)", ("fake-session", '{"note":"other metadata"}'))
            if metadata:
                connection.execute("INSERT INTO session_entry VALUES (?, ?)",
                                   ("fake-session", json.dumps({"modelSelection": {"providerId": PROVIDER, "modelId": model}})))
        command = [sys.executable, str(SCRIPT), "--task-file", str(task), "--builtin-config", str(self.builtin)]
        if checks is not None:
            path = self.root / "checks.json"
            path.write_text(json.dumps({"checks": checks}), encoding="utf-8")
            command += ["--checks-file", str(path)]
        if dirty:
            file = self.root / "tracked.txt"
            file.write_text("good\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
            file.write_text("bad trailing space \n", encoding="utf-8")
            if staged:
                subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        result = subprocess.run(command, cwd=self.root,
                                env={**os.environ, "ZCODE_BIN": str(fake), "ZCODE_STORAGE_DIR": str(storage)},
                                capture_output=True, text=True, timeout=10)
        summary = json.loads(Path(json.loads(result.stdout)["summary"]).read_text())
        return result.returncode, summary

    def test_no_checks_is_never_verified(self):
        code, summary = self.run_fake_assignment()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "no_checks_review_required")
        self.assertFalse(summary["checks_verified"])

    def test_failed_check_is_not_verified(self):
        code, summary = self.run_fake_assignment([[sys.executable, "-c", "raise SystemExit(3)"]])
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "tests_failed")
        self.assertFalse(summary["checks_verified"])

    def test_whitespace_failure_cannot_pass(self):
        code, summary = self.run_fake_assignment([[sys.executable, "-c", "pass"]], dirty=True)
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "diff_check_failed")
        self.assertFalse(summary["checks_verified"])

    def test_staged_whitespace_failure_cannot_pass(self):
        code, summary = self.run_fake_assignment([[sys.executable, "-c", "pass"]], dirty=True, staged=True)
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "diff_check_failed")
        self.assertNotEqual(summary["git_diff_staged_check_exit_code"], 0)

    def test_model_mismatch_skips_checks(self):
        code, summary = self.run_fake_assignment([[sys.executable, "-c", "pass"]], model="GLM-5.3")
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "model_mismatch")
        self.assertEqual(summary["checks"], [])
        self.assertFalse(summary["checks_verified"])

    def test_missing_metadata_skips_checks(self):
        code, summary = self.run_fake_assignment([[sys.executable, "-c", "pass"]], metadata=False)
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "model_verification_failed")
        self.assertEqual(summary["checks"], [])

    def test_windows_tree_cleanup_and_failure_fallback(self):
        process = Mock(pid=123)
        with patch.object(delegate.subprocess, "run", return_value=Mock(returncode=0)) as run:
            delegate._terminate_tree_windows(process)
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"])
            process.kill.assert_not_called()
        with patch.object(delegate.subprocess, "run", side_effect=OSError("unavailable")):
            delegate._terminate_tree_windows(process)
            process.kill.assert_called_once()

    def desktop_fixtures(self):
        storage = self.root / "desktop-storage"
        cli_path = storage / "cli/db/db.sqlite"
        index_path = storage / "v2/tasks-index.sqlite"
        cli_path.parent.mkdir(parents=True)
        index_path.parent.mkdir(parents=True)
        with sqlite3.connect(cli_path) as connection:
            connection.executescript("""
                CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,
                    trace_id TEXT, time_created INTEGER, time_updated INTEGER);
                CREATE TABLE session_entry (session_id TEXT, type TEXT, data TEXT);
                CREATE TABLE part (session_id TEXT, data TEXT);
            """)
            connection.execute("INSERT INTO session VALUES (?, NULL, ?, 'CLI task title', ?, 100, 200)",
                               ("desktop-session", str(self.root.resolve()), "test-trace"))
            connection.execute("INSERT INTO session_entry VALUES (?, ?, ?)",
                               ("desktop-session", "runtime/model_selection", json.dumps({"modelSelection": {
                                   "providerId": PROVIDER, "modelId": "GLM-5.3-Flash"}})))
            connection.execute("INSERT INTO part VALUES (?, ?)",
                               ("desktop-session", '{"type":"text","text":"saved history"}'))
        with sqlite3.connect(index_path) as connection:
            connection.execute("""CREATE TABLE tasks (
                workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL,
                task_id TEXT NOT NULL, title TEXT NOT NULL, task_status TEXT, provider TEXT,
                mode TEXT, model TEXT, created_at INTEGER, updated_at INTEGER,
                meta_json TEXT, searchable_text TEXT,
                pinned INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
                PRIMARY KEY (workspace_key, task_id))""")
        return storage, cli_path, index_path

    def test_desktop_registration_links_existing_history_and_is_idempotent(self):
        storage, cli_path, index_path = self.desktop_fixtures()
        original = cli_path.read_bytes()
        with patch.dict(os.environ, {"ZCODE_STORAGE_DIR": str(storage)}):
            result = delegate.register_desktop_session("desktop-session", self.root, "[Codex] test", "completed")
            self.assertEqual(result["status"], "registered")
            self.assertEqual(result["title"], "CLI task title")
            with sqlite3.connect(index_path) as connection:
                row = connection.execute("SELECT task_id, meta_json, searchable_text FROM tasks WHERE "
                                         "workspace_key=? AND provider='glm' AND deleted=0 AND archived=0 AND pinned=0",
                                         (str(self.root.resolve()),)).fetchone()
                self.assertEqual(row[0], "desktop-session")
                self.assertEqual(json.loads(row[1])["model"], PROVIDER + "/GLM-5.3-Flash")
                self.assertEqual(row[2], "saved history")
                connection.execute("UPDATE tasks SET title='user name', archived=1")
            again = delegate.register_desktop_session("desktop-session", self.root, "overwrite?", "error")
            self.assertEqual(again["status"], "already_registered")
            self.assertEqual(again["title"], "user name")
            with sqlite3.connect(index_path) as connection:
                self.assertEqual(connection.execute("SELECT count(*), archived FROM tasks").fetchone(), (1, 1))
        self.assertEqual(cli_path.read_bytes(), original)

    def test_desktop_registration_rejects_another_workspace(self):
        storage, _, index_path = self.desktop_fixtures()
        with patch.dict(os.environ, {"ZCODE_STORAGE_DIR": str(storage)}), self.assertRaises(ValueError):
            delegate.register_desktop_session("desktop-session", self.root / "other", "bad", "completed")
        with sqlite3.connect(index_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_desktop_registration_does_not_create_missing_index(self):
        storage, _, index_path = self.desktop_fixtures()
        index_path.unlink()
        with patch.dict(os.environ, {"ZCODE_STORAGE_DIR": str(storage)}), self.assertRaises(sqlite3.Error):
            delegate.register_desktop_session("desktop-session", self.root, "test", "completed")
        self.assertFalse(index_path.exists())

    def test_timeout_session_recovery_requires_unique_exact_task_and_time(self):
        storage, cli_path, _ = self.desktop_fixtures()
        with patch.dict(os.environ, {"ZCODE_STORAGE_DIR": str(storage)}):
            self.assertEqual(delegate.find_session_for_task(self.root, "saved history", 50), ("desktop-session", None))
            self.assertIsNone(delegate.find_session_for_task(self.root, "other task", 50)[0])
            self.assertIsNone(delegate.find_session_for_task(self.root, "saved history", 101)[0])
            with sqlite3.connect(cli_path) as connection:
                connection.execute("INSERT INTO session VALUES ('other-session', NULL, ?, 'other', 'trace', 150, 160)",
                                   (str(self.root.resolve()),))
                connection.execute("INSERT INTO part VALUES ('other-session', ?)",
                                   ('{"type":"text","text":"saved history"}',))
            self.assertIsNone(delegate.find_session_for_task(self.root, "saved history", 50)[0])


if __name__ == "__main__":
    unittest.main()
