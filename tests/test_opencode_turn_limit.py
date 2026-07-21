import json
import os
import signal
import subprocess
import sys
import threading

from coral.agent.builtin.opencode import (
    _install_signal_safe_shell,
    _is_completed_turn,
    _tee_and_limit,
)
from coral.agent.runtime import _extract_session_id


def test_completed_turn_only_accepts_step_finish_json() -> None:
    assert _is_completed_turn('{"type":"step_finish","part":{"type":"step-finish"}}')
    assert not _is_completed_turn('{"type":"step_start"}')
    assert not _is_completed_turn('{"type":"tool_use"}')
    assert not _is_completed_turn("not json")


def test_stream_stops_process_group_at_turn_limit(tmp_path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,time\n"
            "for i in range(10):\n"
            " print(json.dumps({'type':'step_finish','index':i}), flush=True)\n"
            " time.sleep(.05)\n"
            "time.sleep(10)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_path = tmp_path / "agent-1.0.log"
    _tee_and_limit(process, log_path.open("w"), "agent-1", log_path, 2, False)
    process.wait(timeout=2)
    assert json.loads(log_path.with_suffix(".turn-limit.json").read_text()) == {
        "agent_id": "agent-1",
        "max_turns": 2,
        "terminal_reason": "max_turns",
        "turns_completed": 2,
    }


def test_stream_stops_only_after_completed_turn_boundary(tmp_path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,time\n"
            "print(json.dumps({'type':'step_start'}), flush=True)\n"
            "time.sleep(.1)\n"
            "print(json.dumps({'type':'tool_use','state':'completed'}), flush=True)\n"
            "print(json.dumps({'type':'step_finish'}), flush=True)\n"
            "time.sleep(10)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stop = threading.Event()
    stop.set()
    log_path = tmp_path / "agent-1.0.log"
    _tee_and_limit(process, log_path.open("w"), "agent-1", log_path, 0, False, stop)
    process.wait(timeout=2)
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [record["type"] for record in records] == [
        "step_start",
        "tool_use",
        "step_finish",
    ]


def test_generic_session_extractor_accepts_opencode_spelling(tmp_path) -> None:
    log = tmp_path / "agent.log"
    log.write_text('{"type":"step_finish","sessionID":"ses_opencode"}\n')
    assert _extract_session_id(log) == "ses_opencode"


def test_signal_safe_shell_reports_signal_as_numeric_exit(tmp_path) -> None:
    env = {"PATH": os.environ["PATH"], "SHELL": "/bin/bash"}
    wrapper = _install_signal_safe_shell(env, tmp_path)

    aborted = subprocess.run(
        [str(wrapper), "-c", "kill -ABRT $$"],
        capture_output=True,
        env=env,
    )
    ordinary_failure = subprocess.run(
        [str(wrapper), "-c", "exit 23"],
        capture_output=True,
        env=env,
    )

    assert aborted.returncode == 128 + signal.SIGABRT
    assert ordinary_failure.returncode == 23
