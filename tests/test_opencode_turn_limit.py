import json
import subprocess
import sys

from coral.agent.builtin.opencode import _is_completed_turn, _tee_and_limit


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
