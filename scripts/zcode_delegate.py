#!/usr/bin/env python3
"""Run a single ZCode assignment, optional safe test commands, and write review evidence.

Invoked by Codex from the root of a local Git worktree. No retries here:
Codex is the reviewer and decides whether/how to delegate a revised prompt.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager


def run_capture(args, cwd):
    try:
        p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
        return {"exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}


def _terminate_tree_windows(process):
    # Windows 的 kill() 只终止直接子进程；taskkill /T 同时清理后代。
    try:
        outcome = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if outcome.returncode != 0:
            process.kill()
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def run_logged(args, cwd, out_dir, name, timeout, env=None):
    stdout_path = out_dir / f"{name}.stdout.log"
    stderr_path = out_dir / f"{name}.stderr.log"
    result = {"command": args, "stdout": str(stdout_path), "stderr": str(stderr_path),
              "status": "unknown", "exit_code": None}
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(args, cwd=cwd, stdout=stdout, stderr=stderr,
                                       stdin=subprocess.DEVNULL, env=env,
                                       start_new_session=os.name != "nt")
            try:
                process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                if os.name == "nt":
                    _terminate_tree_windows(process)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        result["exit_code"] = process.returncode
        result["status"] = "passed" if process.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        result["status"] = "timed_out"
    except OSError as exc:
        result["status"] = "could_not_start"
        result["error"] = str(exc)
    return result


@contextmanager
def model_environment(model, provider, builtin_path):
    """为单轮派工提供仅含指定套餐和模型的临时配置，保留用户全局设置。"""
    builtin = json.loads(builtin_path.read_text(encoding="utf-8"))
    rules = builtin["config"]["providerConfigRules"]["providerRules"]
    selected = [rule for rule in rules if rule["providerId"] == provider]
    if len(selected) != 1 or model not in selected[0]["config"]["builtinModelIds"]:
        raise ValueError(f"Model {model} is not available in bundled provider {provider}")
    selected[0]["config"]["builtinModelIds"] = [model]
    builtin["config"]["providerConfigRules"]["providerRules"] = selected
    personal = {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {"providerRules": []},
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
            "defaultModelSelection": {
                "providerId": provider, "modelId": model,
                "options": {"reasoningLevel": "high"},
            },
        },
    }
    # 0.16.9 要求 reasoningLevel；省略时会静默回退到第一个可选模型。
    with tempfile.TemporaryDirectory(prefix="codex-zcode-profile-") as directory:
        paths = [Path(directory) / "builtin.json", Path(directory) / "personal.json"]
        for path, value in zip(paths, [builtin, personal]):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            path.chmod(0o600)
        yield {
            **os.environ,
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(paths[0]),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": str(paths[1]),
            "ZCODE_MODEL_RETRY_MAX_RETRIES": "0",
        }


def session_id_from_worker(worker):
    """从 CLI 的 JSON 日志提取会话 ID。"""
    log = Path(worker["stdout"]) if worker.get("stdout") else None
    if not log or not log.is_file():
        return None, "worker stdout log is missing"
    text = log.read_text(encoding="utf-8", errors="replace")
    candidates = [text.strip()] + [line.strip() for line in text.splitlines() if line.strip()]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("sessionId"):
            return str(payload["sessionId"]), None
    return None, "worker stdout contains no JSON object with sessionId"


def verify_session_model(session_id, requested_model, requested_provider):
    """核对 CLI 会话记录中的实际模型；缺少证据时返回错误。"""
    # shortcut: 依赖 ZCode 0.16.9 的 session_entry 布局，升级 CLI 时重新验证。
    root = Path(os.environ.get("ZCODE_STORAGE_DIR") or Path.home() / ".zcode").resolve()
    database = root / "cli" / "db" / "db.sqlite"
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT data FROM session_entry WHERE session_id = ? ORDER BY rowid",
                                      (session_id,)).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        return None, f"could not read session metadata database: {exc}"
    if not rows:
        return None, f"no session_entry row for session {session_id}"
    try:
        entries = [json.loads(row[0]) for row in rows]
        selections = [entry["modelSelection"] for entry in entries if "modelSelection" in entry]
        if not selections:
            return None, "session metadata contains no modelSelection"
        selection = selections[-1]
        actual = {"providerId": selection["providerId"], "modelId": selection["modelId"]}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return None, f"session metadata is malformed: {exc}"
    if actual["providerId"] != requested_provider or actual["modelId"] != requested_model:
        return actual, (f"session ran {actual['providerId']}/{actual['modelId']} "
                        f"instead of requested {requested_provider}/{requested_model}")
    return actual, None


def load_checks(path):
    if not path:
        return []
    contents = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(contents, dict) or not isinstance(contents.get("checks"), list):
        raise ValueError('checks file must contain {"checks": [["program", "arg", ...], ...]}')
    checks = contents["checks"]
    for check in checks:
        if not isinstance(check, list) or not check or not all(isinstance(a, str) and a for a in check):
            raise ValueError("Each check must be a non-empty list of non-empty string arguments")
    return checks


def exclude_control_directory(repo):
    """Keep assignment text and logs out of Git without changing project files."""
    tracked = run_capture(["git", "ls-files", "--", ".zcode-control"], repo)
    if tracked["exit_code"] != 0 or tracked["stdout"].strip():
        raise ValueError("Cannot protect logs: .zcode-control is tracked or git ls-files failed")
    location = run_capture(["git", "rev-parse", "--git-path", "info/exclude"], repo)
    if location["exit_code"] != 0:
        raise ValueError("Cannot locate Git's local exclude file")
    path = Path(location["stdout"].strip())
    if not path.is_absolute():
        path = repo / path
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    # Keep our rule last so earlier negations cannot expose assignment logs.
    if content.splitlines() and content.splitlines()[-1] == "/.zcode-control/":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as exclude:
        exclude.write(("\n" if content and not content.endswith("\n") else "") + "/.zcode-control/\n")


def register_desktop_session(session_id, repo, title, status):
    """为已存在的 CLI 会话增加 Desktop 列表索引，不重写历史或已有对话。"""
    root = Path(os.environ.get("ZCODE_STORAGE_DIR") or Path.home() / ".zcode").resolve()
    # shortcut: 使用当前 ZCode Desktop 的任务索引契约，升级后核对恢复插件与列结构。
    cli = sqlite3.connect((root / "cli/db/db.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        cli.row_factory = sqlite3.Row
        row = cli.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if row is None or row["parent_id"] is not None or Path(row["directory"]).resolve() != repo.resolve():
            raise ValueError("Session is not a root conversation in the assigned workspace")
        title = row["title"] or title
        runtime = {kind: json.loads(data) for kind, data in cli.execute(
            "SELECT type, data FROM session_entry WHERE session_id = ?", (session_id,))}
        selection = runtime["runtime/model_selection"]["modelSelection"]
        mode = runtime.get("runtime/execution_state", {}).get("mode", "build")
        model = f"{selection['providerId']}/{selection['modelId']}"
        texts = [json.loads(data).get("text", "") for (data,) in cli.execute(
            "SELECT data FROM part WHERE session_id = ? AND json_extract(data, '$.type') = 'text' ORDER BY rowid",
            (session_id,))]
        meta = {"taskId": session_id, "traceId": row["trace_id"] or session_id,
                "title": title, "titleOverridden": False, "workspacePath": str(repo.resolve()),
                "createdAt": row["time_created"], "updatedAt": row["time_updated"],
                "provider": "glm", "mode": mode, "model": model,
                "status": status, "target": None}
        reasoning = selection.get("options", {}).get("reasoningLevel")
        if reasoning:
            meta["thoughtLevel"] = reasoning
    finally:
        cli.close()
    index = sqlite3.connect((root / "v2/tasks-index.sqlite").as_uri() + "?mode=rw", uri=True)
    try:
        with index:
            cursor = index.execute("""INSERT INTO tasks
                (workspace_key, workspace_path, task_id, title, task_status, provider, mode, model,
                 created_at, updated_at, meta_json, searchable_text)
                VALUES (?, ?, ?, ?, ?, 'glm', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_key, task_id) DO NOTHING""",
                (str(repo.resolve()), str(repo.resolve()), session_id, title, status, mode, model,
                 meta["createdAt"], meta["updatedAt"], json.dumps(meta, ensure_ascii=False), "\n".join(texts)))
            saved_title = index.execute("SELECT title FROM tasks WHERE workspace_key = ? AND task_id = ?",
                                        (str(repo.resolve()), session_id)).fetchone()[0]
        return {"status": "registered" if cursor.rowcount else "already_registered",
                "task_id": session_id, "title": saved_title, "workspace": str(repo.resolve())}
    finally:
        index.close()


def find_session_for_task(repo, task_text, started_ms):
    """超时未输出 JSON 时，按工作区、时间与完整任务文本定位本轮唯一会话。"""
    root = Path(os.environ.get("ZCODE_STORAGE_DIR") or Path.home() / ".zcode").resolve()
    try:
        cli = sqlite3.connect((root / "cli/db/db.sqlite").as_uri() + "?mode=ro", uri=True)
        try:
            rows = cli.execute("""SELECT DISTINCT session.id FROM session JOIN part ON part.session_id = session.id
                WHERE session.directory = ? AND session.parent_id IS NULL AND session.time_created >= ?
                  AND json_extract(part.data, '$.type') = 'text' AND json_extract(part.data, '$.text') = ?""",
                (str(repo.resolve()), started_ms, task_text)).fetchall()
        finally:
            cli.close()
    except (sqlite3.Error, OSError) as exc:
        return None, str(exc)
    return (rows[0][0], None) if len(rows) == 1 else (None, "No unique session matches this assignment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--checks-file", type=Path, default=None)
    parser.add_argument("--zcode-timeout", type=int, default=1200)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--mode", choices=["edit", "build", "plan"], default="edit")
    parser.add_argument("--model", choices=["GLM-5.3", "GLM-5.3-Flash"], default="GLM-5.3-Flash")
    parser.add_argument("--provider", choices=["account:bigmodel-individual-coding-plan", "account:zai-individual-coding-plan"],
                        default="account:bigmodel-individual-coding-plan")
    parser.add_argument("--builtin-config", type=Path, default=Path(os.environ.get(
        "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE",
        "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json")))
    args = parser.parse_args()

    cwd = Path.cwd().resolve()
    git_root = run_capture(["git", "rev-parse", "--show-toplevel"], cwd)
    if git_root["exit_code"] != 0:
        parser.error("Run from a local Git repository/worktree.")
    repo = Path(git_root["stdout"].strip()).resolve()
    task_path = args.task_file.resolve()
    if not task_path.is_file():
        parser.error(f"Task file does not exist: {task_path}")
    checks_path = args.checks_file.resolve() if args.checks_file else None
    if checks_path and not checks_path.is_file():
        parser.error(f"Checks file does not exist: {checks_path}")
    if args.zcode_timeout <= 0 or args.test_timeout <= 0:
        parser.error("Timeouts must be positive integers")
    try:
        checks = load_checks(checks_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    task_text = task_path.read_text(encoding="utf-8").strip()
    if not task_text:
        parser.error("The task file is empty")

    try:
        exclude_control_directory(repo)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    started_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    output = repo / ".zcode-control" / "runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    before = run_capture(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo)

    zcode_command = os.environ.get("ZCODE_BIN", "zcode")
    resolved_bin = shutil.which(zcode_command)
    if resolved_bin:
        worker_cmd = [resolved_bin, "--cwd", str(repo), "--mode", args.mode,
                      "--disallowed-tools", "Task,Agent",
                      "--output-format", "json", "--prompt", task_text]
        try:
            with model_environment(args.model, args.provider, args.builtin_config) as env:
                worker = run_logged(worker_cmd, repo, output, "zcode", args.zcode_timeout, env=env)
        except (OSError, ValueError, KeyError) as exc:
            worker = {"command": worker_cmd, "status": "could_not_start", "exit_code": None,
                      "error": str(exc)}
    else:
        worker = {"command": [zcode_command], "status": "could_not_start", "exit_code": None,
                  "error": f"Could not find '{zcode_command}' on PATH. Configure ZCode CLI first."}

    session_id = None
    actual_model_selection = None
    model_error = None
    if worker["status"] == "passed":
        session_id, session_error = session_id_from_worker(worker)
        if session_error:
            model_error = session_error
        else:
            actual_model_selection, model_error = verify_session_model(
                session_id, args.model, args.provider)
    else:
        session_id, _ = session_id_from_worker(worker)
        if not session_id:
            session_id, _ = find_session_for_task(repo, task_text, started_ms)
    model_verified = (worker["status"] == "passed" and actual_model_selection is not None
                      and model_error is None)

    test_results = []
    if model_verified:
        for i, command in enumerate(checks, 1):
            test = run_logged(command, repo, output, f"check-{i}", args.test_timeout)
            test_results.append(test)
            if test["status"] != "passed":
                break

    after = run_capture(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo)
    diff_stat = run_capture(["git", "diff", "--stat"], repo)
    diff_check = run_capture(["git", "diff", "--check"], repo)
    staged_stat = run_capture(["git", "diff", "--cached", "--stat"], repo)
    staged_check = run_capture(["git", "diff", "--cached", "--check"], repo)
    diff_failed = diff_check["exit_code"] != 0 or staged_check["exit_code"] != 0
    success = model_verified and all(t["status"] == "passed" for t in test_results)
    # No configured tests means only worker execution succeeded; never call that "verified".
    success = success and not diff_failed
    verified = success and bool(checks)
    if worker["status"] != "passed":
        status = "worker_failed"
    elif not model_verified:
        status = "model_mismatch" if actual_model_selection is not None else "model_verification_failed"
    elif diff_failed:
        status = "diff_check_failed"
    elif not success:
        status = "tests_failed"
    else:
        status = "checks_passed_review_required" if verified else "no_checks_review_required"
    desktop_task = {"status": "not_registered", "reason": "No completed CLI session id available"}
    if session_id:
        try:
            title = f"[Codex] {args.model} · {task_text.splitlines()[0].lstrip('# ').strip()[:64]}"
            desktop_task = register_desktop_session(session_id, repo, title,
                                                    "completed" if success else "error")
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            desktop_task = {"status": "registration_failed", "error": str(exc), "task_id": session_id}
    summary = {
        "run_id": run_id, "repo": str(repo), "task_file": str(task_path),
        "checks_file": str(checks_path) if checks_path else None,
        "model": args.model, "provider": args.provider,
        "session_id": session_id,
        "actual_model_selection": actual_model_selection,
        "model_verification_error": model_error,
        "desktop_task": desktop_task,
        "worker": worker, "checks": test_results, "test_count_configured": len(checks),
        "checks_verified": verified,
        "git_status_before": before["stdout"], "git_status_after": after["stdout"],
        "git_diff_stat": diff_stat["stdout"],
        "git_diff_check_exit_code": diff_check["exit_code"],
        "git_diff_check_message": diff_check["stdout"] + diff_check["stderr"],
        "git_diff_staged_stat": staged_stat["stdout"],
        "git_diff_staged_check_exit_code": staged_check["exit_code"],
        "git_diff_staged_check_message": staged_check["stdout"] + staged_check["stderr"],
        "status": status,
        "review_required": True,
        "note": "A zero exit code or passing tests do not replace Codex diff/security review. "
                "git diff evidence covers tracked staged and unstaged changes only; "
                "untracked files always require manual review.",
    }
    summary_file = output / "summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary_file), "status": summary["status"],
                      "worker_log": worker.get("stdout"), "checks_verified": verified,
                      "desktop_task": desktop_task,
                      "git_status_after": after["stdout"]}, ensure_ascii=False))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
