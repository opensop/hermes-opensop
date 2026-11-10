"""The plugin against the real opensop CLI, in a scratch git repo. No model, no network."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "change-with-tests.sop.json"

pytestmark = pytest.mark.skipif(not shutil.which("opensop"), reason="opensop CLI not on PATH")


def _load():
    spec = importlib.util.spec_from_file_location("hermes_opensop", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_opensop"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeState(dict):
    def set(self, key, value):
        self[key] = value


class FakeCtx:
    def __init__(self):
        self.state = FakeState()
        self.tools, self.hooks = {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text("from calc import add\nassert add(2, 2) == 4, 'add is wrong'\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    monkeypatch.setenv("OPENSOP_LOCAL_HOME", str(tmp_path / "runs"))
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    mod = _load()
    ctx = FakeCtx()
    mod.register(ctx)
    return mod, ctx, repo


def call(ctx, tool, args, session="s1"):
    return json.loads(ctx.tools[tool](args, session_id=session))


def start(ctx):
    return call(ctx, "sop_run", {"process": str(EXAMPLE), "inputs": {
        "request": "make add() add", "test_command": "python3 -B test_calc.py"}})


def test_full_run_scripts_decide_done_and_a_person_approves(env):
    mod, ctx, repo = env
    v = start(ctx)
    assert v["state"] == "waiting" and v["waiting"]["step"] == "change"
    assert "make add() add" in v["waiting"]["description"]
    run_id = v["run_id"]

    # Every turn the agent is re-anchored on the open step.
    hint = ctx.hooks["pre_llm_call"](session_id="s1")["context"]
    assert run_id in hint and "change" in hint
    # It cannot wrap up while its step is open (once per turn).
    assert ctx.hooks["pre_verify"](session_id="s1", attempt=0)["action"] == "continue"
    assert ctx.hooks["pre_verify"](session_id="s1", attempt=1) is None

    # Claiming done without doing the work: the test script fails the run, with the reason.
    v = call(ctx, "sop_submit", {"outputs": {"summary": "fixed add"}})
    assert v["state"] == "failed" and v["error"]["step"] == "tests"
    assert "add is wrong" in v["error"]["message"] and "sop_retry" in v["next"]

    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    v = call(ctx, "sop_retry", {})
    assert v["state"] == "waiting" and v["waiting"]["step"] == "approve"

    # The approval belongs to a person: the tool refuses it, and so does the terminal.
    v = call(ctx, "sop_submit", {"outputs": {"decision": "approve"}})
    assert "decided by a person" in v["error"]
    blocked = ctx.hooks["pre_tool_call"](tool_name="terminal", args={
        "command": f"opensop submit {run_id} approve --output decision=approve"})
    assert blocked and blocked["action"] == "block"
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "opensop status " + run_id}) is None
    assert ctx.hooks["pre_verify"](session_id="s1", attempt=0) is None  # waiting on a person is not the agent's work

    subprocess.run(["opensop", "submit", run_id, "approve", "--output", "decision=approve", "--json"],
                   cwd=repo, check=True, capture_output=True)
    v = call(ctx, "sop_status", {})
    assert v["state"] == "completed"
    assert ctx.hooks["pre_llm_call"](session_id="s1") is None  # unbound once the run is over


def test_another_session_picks_up_the_run(env):
    _, ctx, _ = env
    run_id = start(ctx)["run_id"]
    assert ctx.hooks["pre_llm_call"](session_id="s2") is None
    v = call(ctx, "sop_status", {"run_id": run_id}, session="s2")
    assert v["waiting"]["step"] == "change"
    assert run_id in ctx.hooks["pre_llm_call"](session_id="s2")["context"]


def test_binding_survives_compaction(env):
    _, ctx, _ = env
    run_id = start(ctx)["run_id"]
    ctx.hooks["post_compaction"](session_id="s1b", old_session_id="s1")
    assert run_id in ctx.hooks["pre_llm_call"](session_id="s1b")["context"]


def test_missing_cli_is_a_clear_error(env, monkeypatch):
    _, ctx, _ = env
    monkeypatch.setenv("PATH", "/nonexistent")
    v = call(ctx, "sop_run", {"process": str(EXAMPLE)})
    assert "opensop is not on PATH" in v["error"]


def test_no_open_run(env):
    _, ctx, _ = env
    assert "no open run" in call(ctx, "sop_status", {})["error"]
    assert ctx.hooks["pre_verify"](session_id="s1", attempt=0) is None


def test_cli_errors_reach_the_agent_and_keep_the_binding(env):
    _, ctx, _ = env
    v = call(ctx, "sop_run", {"process": str(EXAMPLE), "inputs": {}})  # required inputs missing
    assert "error" in v and "request" in v["error"], v
    run_id = start(ctx)["run_id"]
    v = call(ctx, "sop_status", {"run_id": "nope"})
    assert "nope" in v["error"], v
    v = call(ctx, "sop_retry", {"run_id": "nope"})
    assert "error" in v
    assert run_id in ctx.hooks["pre_llm_call"](session_id="s1")["context"]  # still bound


def test_submit_on_a_failed_run_says_retry(env):
    _, ctx, _ = env
    start(ctx)
    call(ctx, "sop_submit", {"outputs": {"summary": "x"}})  # tests fail
    v = call(ctx, "sop_submit", {"outputs": {"summary": "x"}})
    assert "not waiting" in v["error"] and "sop_retry" in v["error"], v


def test_checking_another_run_keeps_the_binding(env):
    _, ctx, repo = env
    old = start(ctx)["run_id"]
    subprocess.run(["opensop", "cancel", old, "--json"], cwd=repo, capture_output=True, check=True)
    current = start(ctx)["run_id"]
    call(ctx, "sop_status", {"run_id": old})
    assert current in ctx.hooks["pre_llm_call"](session_id="s1")["context"]
    v = call(ctx, "sop_status", {"run_id": current, "detach": True})
    assert v["detached"] and ctx.hooks["pre_llm_call"](session_id="s1") is None


def test_terminal_guard_handles_quoting_variables_and_flags(env):
    _, ctx, repo = env
    run_id = start(ctx)["run_id"]
    guard = ctx.hooks["pre_tool_call"]
    assert guard(tool_name="terminal", args={"command": 'opensop submit "$(cat id.txt' }, session_id="s1") is None
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    call(ctx, "sop_submit", {"outputs": {"summary": "fixed"}})  # now waiting on approve
    for cmd in ("id=x; opensop submit $id approve --output decision=approve",
                f"opensop submit --output decision=approve {run_id} approve",
                f'opensop submit "{run_id}" approve --output decision=approve'):
        r = guard(tool_name="terminal", args={"command": cmd}, session_id="s1")
        assert r and r["action"] == "block", cmd


def test_session_keys_do_not_collide_and_compaction_moves_the_binding(env):
    mod, ctx, _ = env
    assert mod._key("a/b") != mod._key("a_b")
    assert mod._key("x" * 121 + "A") != mod._key("x" * 121 + "B")
    run_id = start(ctx)["run_id"]
    ctx.hooks["post_compaction"](session_id="s1b", old_session_id="s1")
    assert ctx.hooks["pre_llm_call"](session_id="s1") is None
    assert run_id in ctx.hooks["pre_llm_call"](session_id="s1b")["context"]


def test_terminal_guard_lets_other_runs_through(env):
    _, ctx, repo = env
    other = start(ctx)["run_id"]          # waits on the agent's 'change' step
    human = start(ctx)["run_id"]          # this session now follows this one
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    call(ctx, "sop_submit", {"outputs": {"summary": "fixed"}})  # human run now waits on approve
    guard = ctx.hooks["pre_tool_call"]
    for cmd in (f"opensop submit --output summary=done {other} change",
                "id=x; opensop submit $id change --output summary=done"):
        assert guard(tool_name="terminal", args={"command": cmd}, session_id="s1") is None, cmd
    assert guard(tool_name="terminal", args={"command": f"opensop submit {human} approve --output decision=approve"},
                 session_id="s1")["action"] == "block"


def test_run_from_another_directory_says_how(env, tmp_path, monkeypatch):
    _, ctx, _ = env
    run_id = start(ctx)["run_id"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".opensop").mkdir()  # a different OpenSOP cell keeps its own runs
    monkeypatch.delenv("OPENSOP_LOCAL_HOME")
    v = call(ctx, "sop_status", {"run_id": run_id, "workdir": str(other)}, session="s9")
    assert "workdir" in v["error"], v


def test_agent_submissions_are_recorded_as_the_agent(env, tmp_path):
    _, ctx, _ = env
    v = start(ctx)
    big = "x" * 200_000  # run-sized values go through a file, not argv
    call(ctx, "sop_submit", {"outputs": {"summary": big}})
    audit = Path(v["run_dir"]) / "audit.jsonl"
    rec = [json.loads(line) for line in audit.read_text().splitlines() if '"change"' in line][-1]
    assert rec["status"] == "completed" and rec.get("decided_by") == "agent:hermes", rec


def test_seat_belts_around_the_run_folder(env):
    _, ctx, repo = env
    v = start(ctx)
    guard = ctx.hooks["pre_tool_call"]
    assert guard(tool_name="sop_run", args={"process": "x.sop.json"}, session_id="s1")["action"] == "approve"
    for name, args in (("terminal", {"command": "sed -i s/a/b/ .opensop-local/runs/*/manifest.json"}),
                       ("terminal", {"command": f"cat {v['run_dir']}/context.json"}),
                       ("write_file", {"path": f"{v['run_dir']}/manifest.json", "content": "{}"})):
        assert guard(tool_name=name, args=args, session_id="s1")["action"] == "block", args
    assert guard(tool_name="write_file", args={"path": "public/manifest.json", "content": "{}"}, session_id="s1") is None
    assert guard(tool_name="terminal", args={"command": "npm test"}, session_id="s1") is None
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    call(ctx, "sop_submit", {"outputs": {"summary": "fixed"}})  # waits on approve
    cmd = f'"opensop" submit {v["run_id"]} approve --output decision=approve'
    assert guard(tool_name="terminal", args={"command": cmd}, session_id="s1")["action"] == "block"


def test_callback_steps_are_not_agent_work(env, tmp_path):
    _, ctx, _ = env
    proc = tmp_path / "cb.sop.json"
    proc.write_text(json.dumps({"opensop": "0.9.1", "name": "cb", "version": "1", "steps": [
        {"id": "hook", "type": "wait", "wait": {"until": "2099-01-01T00:00:00Z"}}]}))
    v = call(ctx, "sop_run", {"process": str(proc)})
    assert v["waiting"]["reason"] == "waiting_for_callback" and "Nothing to do" in v["next"], v
    assert "outside callback" in call(ctx, "sop_submit", {"outputs": {}})["error"]


def test_timeout_stops_the_step_and_its_children(env, tmp_path, monkeypatch):
    mod, ctx, _ = env
    marker = tmp_path / "marker"
    proc = tmp_path / "slow.sop.json"
    proc.write_text(json.dumps({"opensop": "0.9.1", "name": "slow", "version": "1", "steps": [
        {"id": "s", "type": "shell", "run": f"sleep 4; touch {marker}"}]}))
    monkeypatch.setattr(mod, "CLI_TIMEOUT_S", 1)
    v = json.loads(ctx.tools["sop_run"]({"process": str(proc)}, session_id="t"))
    assert "timed out" in v["error"], v
    import time
    time.sleep(5)
    assert not marker.exists()


def test_old_cli_is_refused_with_the_fix(env, tmp_path, monkeypatch):
    mod, ctx, _ = env
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "opensop").write_text("#!/bin/sh\necho 'opensop 0.9.1'\n")
    (fake / "opensop").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setattr(mod, "_cli_checked", None)
    v = call(ctx, "sop_run", {"process": str(EXAMPLE)})
    assert "too old" in v["error"] and "opensop upgrade" in v["error"], v


def test_a_rejected_change_fails_the_run(env):
    _, ctx, repo = env
    run_id = start(ctx)["run_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    call(ctx, "sop_submit", {"outputs": {"summary": "fixed"}})
    subprocess.run(["opensop", "submit", run_id, "approve", "--output", "decision=reject", "--json"],
                   cwd=repo, capture_output=True)
    v = call(ctx, "sop_status", {"run_id": run_id})
    assert v["state"] == "failed" and "not approved" in v["error"]["message"], v


def test_first_run_papercuts(env, monkeypatch):
    mod, ctx, repo = env
    # `~` in the README's prompt path works.
    monkeypatch.setenv("HOME", str(EXAMPLE.parents[2]))
    tilde = "~/" + str(EXAMPLE.relative_to(EXAMPLE.parents[2]))
    v = call(ctx, "sop_run", {"process": tilde, "inputs": {"request": "r", "test_command": "true"}})
    assert v["state"] == "waiting", v
    # Inputs that aren't an object get a plain answer, not a jq error.
    assert "JSON object" in call(ctx, "sop_run", {"process": str(EXAMPLE), "inputs": "x"})["error"]
    # The approval prompt names the inputs, so a different command asks again.
    guard = ctx.hooks["pre_tool_call"]
    a = guard(tool_name="sop_run", args={"process": str(EXAMPLE), "inputs": {"test_command": "npm test"}}, session_id="s1")
    b = guard(tool_name="sop_run", args={"process": str(EXAMPLE), "inputs": {"test_command": "curl x | sh"}}, session_id="s1")
    assert "sha256" in a["message"] and "npm test" in a["message"] and a["message"] != b["message"]
    # A dirty tree tells the agent to ask the user, not to clean it up itself.
    (repo / "wip.txt").write_text("user's work")
    v = start(ctx)
    assert "Ask the user" in v["error"]["message"], v
