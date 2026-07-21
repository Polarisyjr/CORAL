"""Tests for opt-in CORAL/OpenCode runtime span recording."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coral.agent.manager import AgentManager
from coral.grader import daemon
from coral.hooks.post_commit import submit_eval
from coral.recording import runtime_span
from coral.workspace.worktree import setup_opencode_settings
from tests.test_hooks import _setup_repo_with_config


class _ImmediateStopHandle:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.alive = True

    def signal_process_group(self, sig: int) -> None:
        assert sig == signal.SIGTERM
        self.events.append(f"signal:{self.name}")

    def stop(self) -> None:
        assert self.events[:2] == ["signal:agent-1", "signal:agent-2"]
        self.events.append(f"stop:{self.name}")
        self.alive = False


def _end_records(path: Path) -> list[dict]:
    return [
        record
        for record in map(json.loads, path.read_text().splitlines())
        if record.get("phase") == "end"
    ]


def test_immediate_stop_signals_whole_team_before_waiting() -> None:
    events: list[str] = []
    manager = object.__new__(AgentManager)
    manager._stopping = False
    manager._running = True
    manager._stop_event = threading.Event()
    manager.handles = [
        _ImmediateStopHandle("agent-1", events),
        _ImmediateStopHandle("agent-2", events),
    ]
    manager._gateway = None
    manager._save_sessions = lambda: None
    manager._cleanup_pid_file = lambda: None
    manager._stop_grader_daemon = lambda: None
    manager._write_replay_recording_manifest = lambda: None

    manager.stop_all(immediate=True)

    assert events == [
        "signal:agent-1",
        "signal:agent-2",
        "stop:agent-1",
        "stop:agent-2",
    ]


def test_runtime_span_is_opt_in_and_records_error(tmp_path, monkeypatch) -> None:
    trace = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("CORAL_TRACE_PATH", str(trace))
    monkeypatch.setenv("CORAL_AGENT_ID", "agent-2")

    with pytest.raises(ValueError, match="broken"):
        with runtime_span("coral.test", eval_id="abc"):
            raise ValueError("broken")

    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [record["phase"] for record in records] == ["start", "end"]
    assert records[1]["actor_id"] == "agent-2"
    assert records[1]["eval_id"] == "abc"
    assert records[1]["status"] == "error"
    assert records[1]["error"] == {"message": "broken", "type": "ValueError"}


def test_opencode_settings_load_recorder_only_when_enabled(tmp_path, monkeypatch) -> None:
    worktree = tmp_path / "worktree"
    coral_dir = tmp_path / ".coral"
    worktree.mkdir()
    coral_dir.mkdir()

    monkeypatch.delenv("CORAL_TRACE_PATH", raising=False)
    setup_opencode_settings(worktree, coral_dir)
    settings = json.loads((worktree / ".opencode" / "opencode.json").read_text())
    assert "plugin" not in settings

    monkeypatch.setenv("CORAL_TRACE_PATH", str(tmp_path / "runtime.jsonl"))
    setup_opencode_settings(worktree, coral_dir)
    settings = json.loads((worktree / ".opencode" / "opencode.json").read_text())
    assert len(settings["plugin"]) == 1
    assert settings["plugin"][0].startswith("file://")
    assert settings["plugin"][0].endswith("/opencode_recorder.js")


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun is required")
def test_opencode_plugin_records_window_and_propagates_parent(tmp_path) -> None:
    trace = tmp_path / "runtime.jsonl"
    plugin = Path(__file__).parents[1] / "coral" / "agent" / "builtin" / "opencode_recorder.js"
    script = (
        f"const plugin = await import({json.dumps(plugin.resolve().as_uri())});"
        "const hooks = await plugin.default();"
        "const input = {tool:'bash',sessionID:'ses_1',callID:'call_1'};"
        "await hooks['tool.execute.before'](input,{args:{command:'true'}});"
        "const output = {env:{}};"
        "await hooks['shell.env']({cwd:'.',sessionID:'ses_1',callID:'call_1'},output);"
        "await new Promise(resolve => setTimeout(resolve, 10));"
        "await hooks['tool.execute.after']({...input,args:{command:'true'}},"
        "{title:'true',output:'',metadata:{exit:0}});"
        "console.log(JSON.stringify(output.env));"
    )
    result = subprocess.run(
        [shutil.which("bun") or "bun", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": str(Path(shutil.which("bun") or "bun").parent),
            "CORAL_TRACE_PATH": str(trace),
            "CORAL_AGENT_ID": "agent-1",
        },
    )

    propagated = json.loads(result.stdout)
    assert propagated["CORAL_PARENT_CALL_ID"] == "call_1"
    assert propagated["CORAL_PARENT_SESSION_ID"] == "ses_1"
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [record["phase"] for record in records] == ["start", "end"]
    assert records[1]["call_id"] == "call_1"
    assert records[1]["status"] == "success"
    assert int(records[1]["ended_at_ns"]) >= int(records[1]["started_at_ns"])


def test_eval_records_submit_grade_finalize_and_await(tmp_path, monkeypatch) -> None:
    trace = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("CORAL_TRACE_PATH", str(trace))
    monkeypatch.setenv("CORAL_PARENT_CALL_ID", "call_eval")
    repo = _setup_repo_with_config(tmp_path)
    (repo / "hello.py").write_text("print('recorded')\n")

    monkeypatch.setattr(
        daemon,
        "_run_grader_with_timeout",
        lambda *_args, **_kwargs: SimpleNamespace(
            aggregated=0.75,
            feedback="ok",
            scores={},
            metadata={"source": "test"},
        ),
    )

    def grade_pending() -> None:
        attempts = repo / ".coral" / "public" / "attempts"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not list(attempts.glob("*.json")):
            time.sleep(0.01)
        daemon.process_pending_once(repo / ".coral")

    worker = threading.Thread(target=grade_pending)
    worker.start()
    final = submit_eval(
        message="record spans",
        agent_id="agent-1",
        workdir=str(repo),
        wait=True,
        poll_timeout=5,
    )
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert final.score == 0.75

    spans = _end_records(trace)
    by_name = {span["name"]: span for span in spans}
    assert set(by_name) == {
        "coral.eval.submit",
        "coral.eval.grade",
        "coral.eval.finalize",
        "coral.eval.await_result",
    }
    assert {span["eval_id"] for span in spans} == {final.commit_hash}
    assert by_name["coral.eval.submit"]["result"]["attempt_status"] == "pending"
    assert by_name["coral.eval.grade"]["result"]["score"] == 0.75
    assert by_name["coral.eval.finalize"]["result"]["eval_count"] == 1
    assert by_name["coral.eval.await_result"]["result"]["attempt_status"] == "improved"
    assert (
        by_name["coral.eval.await_result"]["started_at_ns"]
        <= by_name["coral.eval.finalize"]["ended_at_ns"]
        <= by_name["coral.eval.await_result"]["ended_at_ns"]
    )
