"""End to end in a real Hermes, driven by Hermes's own scripted fake model (no API key).

Needs a Hermes source checkout for the fake provider: HERMES_SRC=/path/to/hermes-agent.
Session 1 starts a run and dies. The run is picked up outside Hermes with the plain
CLI, then by a fresh Hermes session, which submits the step and sees the test fail.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = os.environ.get("HERMES_SRC")

pytestmark = pytest.mark.skipif(not (HERMES_SRC and shutil.which("hermes") and shutil.which("opensop")),
                                reason="needs HERMES_SRC, hermes and opensop on PATH")


def _stop_gateway(home):
    pidf = home / "gateway.pid"
    if pidf.exists():
        pid = json.loads(pidf.read_text())["pid"]
        environ = Path(f"/proc/{pid}/environ")
        if environ.exists() and f"HERMES_HOME={home}".encode() in environ.read_bytes():
            os.kill(pid, signal.SIGTERM)


def _session(home, repo, script, prompt):
    sys.path.insert(0, HERMES_SRC)
    from tests.fakes.fake_llm_provider import FakeLLMServer, write_hermes_home
    with FakeLLMServer(script) as srv:
        write_hermes_home(home, srv.base_url, extra_config="plugins:\n  enabled:\n    - opensop\n")
        env = {**os.environ, "HERMES_HOME": str(home), "TERMINAL_CWD": str(repo)}
        out = subprocess.run(["hermes", "-z", prompt], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=300)
        reqs = srv.main_requests()
    _stop_gateway(home)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout, reqs


def _tool_result(reqs, i):
    return json.loads([m for m in reqs[i]["messages"] if m["role"] == "tool"][-1]["content"])


def _call(name, args):
    sys.path.insert(0, HERMES_SRC)
    from tests.fakes.fake_llm_provider import ToolCall
    return ToolCall("tool_call", {"calls": [{"name": name, "arguments": args}]})


def test_run_survives_the_agent(tmp_path, monkeypatch):
    sys.path.insert(0, HERMES_SRC)
    from tests.fakes.fake_llm_provider import Text

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text("from calc import add\nassert add(2, 2) == 4, 'add is wrong'\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(git + args, check=True)
    monkeypatch.setenv("OPENSOP_LOCAL_HOME", str(tmp_path / "runs"))
    home = tmp_path / "hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "opensop").symlink_to(ROOT)

    # Session 1: start the run, then the session ends.
    _, reqs = _session(home, repo, [
        _call("sop_run", {"process": str(ROOT / "examples" / "change-with-tests.sop.json"),
                          "inputs": {"request": "make add() add", "test_command": "python3 -B test_calc.py"}}),
        Text("Started.")], "Fix add() using the change-with-tests process")
    started = _tool_result(reqs, 1)
    assert started["waiting"]["step"] == "change", started
    run_id = started["run_id"]

    # Any other agent or a person sees the same run with the plain CLI.
    st = json.loads(subprocess.run(["opensop", "status", run_id, "--json"], cwd=repo,
                                   capture_output=True, text=True, check=True).stdout)
    assert st["state"] == "waiting" and st["waiting"]["step"] == "change"

    # Session 2, a fresh Hermes: picks the run up by id, claims done, and the script says no.
    _, reqs = _session(home, repo, [
        _call("sop_status", {"run_id": run_id}),
        _call("sop_submit", {"outputs": {"summary": "fixed add"}}),
        Text("The tests failed; I will fix add().")], f"Continue OpenSOP run {run_id}")
    assert _tool_result(reqs, 1)["waiting"]["step"] == "change"
    failed = _tool_result(reqs, 2)
    assert failed["state"] == "failed" and "add is wrong" in failed["error"]["message"], failed
