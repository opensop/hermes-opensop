"""OpenSOP for Hermes: run a process file, step by step, with receipts.

The run lives in OpenSOP's run folder, not in this session. The plugin binds a
Hermes session to a run, shows the agent the current step every turn, keeps
the agent going while its step is open, and refuses to let the agent decide a
step that is reserved for a human. Everything else is the `opensop` CLI.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess

from tools.registry import tool_error, tool_result

TOOLSET = "opensop"
CLI_TIMEOUT_S = int(os.environ.get("OPENSOP_HERMES_TIMEOUT", "1800"))
STATUS_TIMEOUT_S = 20  # hooks call status every turn; a wedged CLI must not stall the turn
HUMAN_REASONS = {"waiting_for_approval", "escalated"}
# Waits the agent can't act on: a person's decision, or an outside callback.
NOT_AGENT_WORK = HUMAN_REASONS | {"waiting_for_callback"}
MAX_CONTEXT_CHARS = 1500

_state = None  # PluginState, set in register()
_ctx = None


# ── opensop CLI ──────────────────────────────────────────────────────────────

def _opensop(args, cwd, timeout=CLI_TIMEOUT_S):
    """Run `opensop <args> --json` in cwd. Returns (dict, error-string)."""
    exe = shutil.which("opensop")
    if not exe:
        return None, "opensop is not on PATH. Install it: https://github.com/opensop/opensop#install"
    try:
        p = subprocess.run([exe, *args, "--json"], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"opensop {args[0]} timed out after {timeout}s"
    except OSError as e:
        return None, f"opensop could not start: {e}"
    try:
        out = json.loads(p.stdout or "null")
    except ValueError:
        out = None
    if isinstance(out, dict) and (p.returncode == 0 or out.get("run_id")):
        return out, None  # a run that ended failed still exits non-zero; its state is in status
    # Errors come as JSON on stderr ({error, message}), sometimes after plain notes.
    for line in reversed((p.stderr or "").strip().splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("error"):
            return None, e.get("message") or e["error"]
    text = (p.stderr or p.stdout or "").strip()
    return None, (text or f"opensop {args[0]} exited {p.returncode} with no output")[-2000:]


def _status(run_id, cwd):
    return _opensop(["status", run_id], cwd, timeout=STATUS_TIMEOUT_S)


def _view(st):
    """The part of `opensop status` an agent needs to act."""
    v = {"run_id": st.get("id"), "process": (st.get("process") or {}).get("name"),
         "state": st.get("state"), "run_dir": st.get("run_dir")}
    w = st.get("waiting")
    if w:
        v["waiting"] = {k: w.get(k) for k in ("step", "reason", "description", "expects") if w.get(k)}
        if w.get("reason") in HUMAN_REASONS:
            v["next"] = f"A person decides this step: opensop submit {v['run_id']} {w.get('step')} --output decision=..."
        else:
            v["next"] = "Do the work for this step, then call sop_submit with its outputs."
    if st.get("error"):
        v["error"] = st["error"]
        v["next"] = "Fix the cause, then call sop_retry to re-run the failed step."
    return v


# ── session binding (session -> run) ─────────────────────────────────────────

def _key(session_id):
    return "bind:" + hashlib.sha256(str(session_id).encode()).hexdigest()


def _bind(session_id, run_id, cwd):
    if _state is not None and session_id:
        _state.set(_key(session_id), {"run_id": run_id, "cwd": cwd})


def _bound(session_id):
    if _state is None or not session_id:
        return None
    return _state.get(_key(session_id))


def _unbind(session_id):
    if _state is not None and session_id:
        _state.set(_key(session_id), None)


def _session_cwd():
    """The calling session's working directory. Hooks and tools run in the gateway, whose own cwd is not it."""
    try:
        return _ctx.session_cwd() if _ctx is not None else ""
    except Exception:
        return ""


def _workdir(args):
    return args.get("workdir") or _session_cwd() or os.environ.get("TERMINAL_CWD") or os.getcwd()


def _resolve(args, kwargs):
    """(run_id, cwd) from args, falling back to this session's bound run."""
    b = _bound(kwargs.get("session_id")) or {}
    run_id = args.get("run_id") or b.get("run_id")
    cwd = args.get("workdir") or (b.get("cwd") if b.get("run_id") == run_id else None) or _workdir(args)
    return run_id, cwd


def _after(run_id, cwd, session_id):
    """Status after an action; bind this session to the run while it is open."""
    st, err = _status(run_id, cwd)
    if err:
        return tool_error(err, run_id=run_id)
    if st.get("state") in ("waiting", "failed", "running"):
        _bind(session_id, run_id, cwd)
    elif (_bound(session_id) or {}).get("run_id") == run_id:
        _unbind(session_id)  # only this run's own end releases the session
    return tool_result(_view(st))


# ── tools ────────────────────────────────────────────────────────────────────

def sop_run(args, **kwargs):
    process = (args.get("process") or "").strip()
    if not process:
        return tool_error("process is required: a path to a .sop.json/.sop.yaml file or a process name")
    cwd = _workdir(args)
    cli = ["run", process, "--inputs", json.dumps(args.get("inputs") or {})]
    out, err = _opensop(cli, cwd)
    if err:
        return tool_error(err)
    if not out.get("run_id"):
        return tool_error("opensop run did not start a run", output=json.dumps(out)[:500])
    return _after(out["run_id"], cwd, kwargs.get("session_id"))


def sop_status(args, **kwargs):
    run_id, cwd = _resolve(args, kwargs)
    if not run_id:
        return tool_error("no run_id given and this session has no open run")
    if args.get("detach"):
        if (_bound(kwargs.get("session_id")) or {}).get("run_id") == run_id:
            _unbind(kwargs.get("session_id"))
        return tool_result(run_id=run_id, detached=True,
                           note="This session no longer follows the run. The run itself is unchanged.")
    return _after(run_id, cwd, kwargs.get("session_id"))


def sop_submit(args, **kwargs):
    run_id, cwd = _resolve(args, kwargs)
    if not run_id:
        return tool_error("no run_id given and this session has no open run")
    st, err = _status(run_id, cwd)
    if err:
        return tool_error(err, run_id=run_id)
    w = st.get("waiting") or {}
    step = args.get("step") or w.get("step")
    if not w or not step:
        hint = " It failed: fix the cause, then call sop_retry." if st.get("state") == "failed" else ""
        return tool_error(f"Run {run_id} is {st.get('state')}, not waiting on a step.{hint}", run_id=run_id)
    if w.get("reason") in HUMAN_REASONS:
        return tool_error(f"Step '{w.get('step')}' is decided by a person, not the agent. "
                          "Ask the user to run: opensop submit " + run_id + " " + str(w.get("step")) +
                          " --output decision=...", run_id=run_id)
    _, err = _opensop(["submit", run_id, step, "--outputs", json.dumps(args.get("outputs") or {})], cwd)
    if err:
        return tool_error(err, run_id=run_id)
    return _after(run_id, cwd, kwargs.get("session_id"))


def sop_retry(args, **kwargs):
    run_id, cwd = _resolve(args, kwargs)
    if not run_id:
        return tool_error("no run_id given and this session has no open run")
    _, err = _opensop(["heal", run_id, "--apply"], cwd)
    if err:
        return tool_error(err, run_id=run_id)
    return _after(run_id, cwd, kwargs.get("session_id"))


_RUN_ID = {"type": "string", "description": "Run id. Defaults to this session's open run."}
_WORKDIR = {"type": "string", "description": "Directory to run opensop in. Defaults to the session's working directory."}

TOOLS = [
    (sop_run, {
        "name": "sop_run",
        "description": "Start a run of an OpenSOP process file. Script steps run at once; the run then stops "
                       "at the first step that needs work from you or a person. Returns that step and what it expects.",
        "parameters": {"type": "object", "properties": {
            "process": {"type": "string", "description": "Path to a .sop.json/.sop.yaml file, or a process name"},
            "inputs": {"type": "object", "description": "Process inputs as a JSON object"},
            "workdir": _WORKDIR}, "required": ["process"]}}),
    (sop_status, {
        "name": "sop_status",
        "description": "Show an OpenSOP run: its state, the step it waits on, or why it failed. "
                       "Give a run_id to pick up a run started anywhere else (another agent, a person, a crashed session).",
        "parameters": {"type": "object", "properties": {
            "run_id": _RUN_ID, "workdir": _WORKDIR,
            "detach": {"type": "boolean", "description": "Stop following this run in this session "
                       "(for example, while it waits on a person). The run is unchanged."}}}}),
    (sop_submit, {
        "name": "sop_submit",
        "description": "Finish the step the run waits on by giving its outputs. The run then continues; its "
                       "script steps check your work. Steps reserved for a person are refused.",
        "parameters": {"type": "object", "properties": {
            "run_id": _RUN_ID,
            "step": {"type": "string", "description": "Step id. Defaults to the step the run waits on."},
            "outputs": {"type": "object", "description": "The step's outputs, as named in its expects"},
            "workdir": _WORKDIR}, "required": ["outputs"]}}),
    (sop_retry, {
        "name": "sop_retry",
        "description": "Re-run the failed step of an OpenSOP run after you fixed the cause, and continue the run.",
        "parameters": {"type": "object", "properties": {"run_id": _RUN_ID, "workdir": _WORKDIR}}}),
]


# ── hooks ────────────────────────────────────────────────────────────────────

def _open_view(session_id):
    b = _bound(session_id)
    if not b or not b.get("run_id"):
        return None
    st, err = _status(b["run_id"], b.get("cwd"))
    if err or st.get("state") not in ("waiting", "failed", "running"):
        _unbind(session_id)
        return None
    return _view(st)


def _pre_llm_call(session_id=None, **kwargs):
    """Every turn: re-anchor the agent on the run's current step."""
    v = _open_view(session_id)
    if not v:
        return None
    if v.get("error"):
        v["error"] = {**v["error"], "message": str(v["error"].get("message") or "")[-600:]}
    text = ("[OpenSOP] This session is working on run " + str(v["run_id"]) + " (" + str(v["process"]) +
            "), state " + str(v["state"]) + ". " + json.dumps({k: v[k] for k in ("waiting", "error", "next") if k in v}))
    return {"context": text[:MAX_CONTEXT_CHARS]}


def _pre_verify(session_id=None, attempt=0, **kwargs):
    """Before the agent finishes: an open step means the work is not done."""
    if attempt:
        return None
    v = _open_view(session_id)
    if not v:
        return None
    if v.get("error") or (v.get("waiting") or {}).get("reason") not in NOT_AGENT_WORK | {None}:
        return {"action": "continue",
                "message": f"OpenSOP run {v['run_id']} is still open: {v['next']} "
                           "The run's scripts decide when it is done, so finish the step or explain what blocks it."}
    return None


_SUBMIT = re.compile(r"\bopensop\b(?:\s+--?\S+)*\s+submit\s+(\S+)")


def _pre_tool_call(tool_name=None, args=None, session_id=None, **kwargs):
    """Refuse a terminal `opensop submit` on a step reserved for a person."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    m = _SUBMIT.search(str(args.get("command") or ""))
    if not m:
        return None
    try:
        run_id = shlex.split(m.group(1))[0]
    except ValueError:
        run_id = m.group(1).strip("'\"")
    st, err = (None, "unusable") if run_id.startswith(("$", "-")) else _status(run_id, args.get("workdir") or _workdir({}))
    if err:  # `opensop submit $id ...` or flags first: judge by the session's own run
        bound = _bound(session_id) or {}
        if not bound.get("run_id"):
            return None  # fail open: the CLI reports its own errors
        run_id = bound["run_id"]
        st, err = _status(run_id, bound.get("cwd"))
        if err:
            return None
    w = st.get("waiting") or {}
    if w.get("reason") in HUMAN_REASONS:
        return {"action": "block",
                "message": f"OpenSOP: step '{w.get('step')}' of run {run_id} is decided by a person. "
                           "Ask the user to submit it."}
    return None


def _post_compaction(session_id=None, old_session_id=None, **kwargs):
    b = _bound(old_session_id)
    if b and session_id and session_id != old_session_id:
        _bind(session_id, b["run_id"], b.get("cwd"))
        _unbind(old_session_id)


def register(ctx):
    global _state, _ctx
    _ctx = ctx
    _state = getattr(ctx, "state", None)  # absent in the catalog capability probe
    for handler, schema in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji="📋")
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_verify", _pre_verify)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_compaction", _post_compaction)
