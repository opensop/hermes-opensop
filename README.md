# OpenSOP for Hermes

**Turn your best agent workflow into a file anyone can run.**

Every process sits somewhere between a script and a judgment call. Some steps must always come out the same: run the tests, check the diff, merge only when the build is green. Others need a model: plan the work, write the change, decide what a failure means.

[OpenSOP](https://github.com/opensop/opensop) lets you set that dial step by step, in one file. Scripts handle the steps that must be certain, and the agent handles the steps that need thinking. This plugin makes Hermes the worker on that line:

- **Scripts decide done.** The agent submits its step; the next script step checks the work. If the tests fail, the run fails at that step, with the real reason, until the agent fixes it.
- **Hermes doesn't have to remember anything.** The process lives in the file and the run lives in a folder on disk, not in the conversation. Compaction, a crash or a new session loses nothing.
- **Any agent can pick up the run.** A run started in Hermes can be finished by another Hermes, by Claude Code or Codex through the `opensop` CLI, or by a person.
- **Every step leaves a receipt.** You audit evidence, not transcripts.

## What it adds to Hermes

| | |
|---|---|
| `sop_run` | Start a run of a process file. Script steps run at once; the run stops at the first step that needs the agent or a person. |
| `sop_status` | Where a run stands: the step it waits on, what that step expects, or why it failed. Give it any run id to pick up a run started elsewhere. |
| `sop_submit` | Finish the step the run waits on. The run continues and its scripts check the work. |
| `sop_retry` | After fixing a failure, re-run the failed step and continue. |
| Re-anchoring (`pre_llm_call`) | Every turn, the agent sees which run it is on and the step it owes. This survives compaction (`post_compaction`). |
| Finish gate (`pre_verify`) | After editing code, the agent can't wrap up while its step is still open. It is nudged once to finish the step or say what blocks it. |
| Human steps stay human (`pre_tool_call`) | Approval steps and escalated judgments are decided by a person. `sop_submit` refuses them, and so does `opensop submit` typed into the terminal. |

## Install

Requirements: Hermes Agent 0.23.0 or later, and the `opensop` CLI 0.10.0 or later on `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/opensop/opensop/main/cli/install.sh | bash
hermes plugins install https://github.com/opensop/hermes-opensop
hermes plugins enable opensop
```

## Try it

The plugin ships one example, [`examples/change-with-tests.sop.json`](examples/change-with-tests.sop.json):

1. **setup** (script): records the starting commit and refuses a dirty tree.
2. **change** (agent): makes the requested change.
3. **tests** (script): runs your test command. The run fails here until it passes.
4. **approve** (person): reviews the diff and approves or rejects it.

In a git checkout, ask Hermes:

> Use the OpenSOP process at `~/.hermes/plugins/opensop/examples/change-with-tests.sop.json` to fix the failing test. The test command is `npm test`.

Then try the part a prompt can't do: quit Hermes mid-run, and finish from anywhere.

```bash
opensop status <run_id>          # where the run stands, and the exact next command
opensop submit <run_id> approve --output decision=approve
```

Or start a fresh Hermes session and say "continue OpenSOP run `<run_id>`".

## Write your own

A process is a JSON or YAML file. Each step is a script (`automated`), work for the agent (`form`), a decision for a person (`approval`), a model call (`llm`, `judgment`), a `loop`, or a `subprocess`. Move a step along the dial as you learn: when a judgment call keeps going wrong, replace it with a script. See the [OpenSOP spec](https://github.com/opensop/opensop/blob/main/SPEC.md) and the [agent guide](https://github.com/opensop/opensop/blob/main/docs/AGENTS.md).

## Security and disclosure

- **Shell commands.** Every tool runs the `opensop` CLI as a subprocess, in the session's working directory. OpenSOP runs a process file's script steps as shell commands on your machine, with your permissions. Only run process files you trust, as you would any script.
- **Files.** OpenSOP writes run folders under `$OPENSOP_LOCAL_HOME`, the nearest `.opensop/` cell, or `~/.opensop-local`. The plugin keeps a small session-to-run map in its Hermes plugin data.
- **Network.** The plugin makes no network calls. A process file's own steps can (for example `webhook` or `llm` steps); that is up to the file.
- **Credentials.** The plugin reads none. A process that calls a model uses that provider's own CLI login or API key.
- **Approvals.** The plugin never auto-approves anything, never changes Hermes's approval settings, and never starts Hermes or shell children in non-interactive mode. It blocks the agent from deciding human steps; it does not decide them itself.
- **Long-running work.** A tool call lasts as long as the script steps it triggers, up to `OPENSOP_HERMES_TIMEOUT` seconds (default 1800). Nothing runs in the background.

## Limitations

- The re-anchoring and finish gate follow one run per session. Start a second run and the session follows that one.
- The finish gate fires only on turns where Hermes edited code (that is when Hermes runs `pre_verify`).
- The terminal guard recognises `opensop submit <run_id> …`. It is a seat belt for an honest agent, not a sandbox.

## Develop

```bash
# opensop and hermes on PATH; HERMES_SRC points at a hermes-agent checkout (for its fake model)
pytest -q
hermes plugins validate .
hermes plugins doctor . --ci
```

`tests/test_plugin.py` drives the tools and hooks against the real `opensop` CLI in a scratch git repo. `tests/test_e2e_hermes.py` runs real Hermes sessions against Hermes's scripted fake model, with no API key: one session starts a run and ends, the plain CLI sees it, and a fresh session picks it up and is stopped by the test script.

## License

Apache-2.0
