# OpenSOP for Hermes

[![CI](https://github.com/opensop/hermes-opensop/actions/workflows/ci.yml/badge.svg)](https://github.com/opensop/hermes-opensop/actions/workflows/ci.yml)

**Turn your best agent workflow into a file anyone can run.**

Every process sits somewhere between a script and a judgment call. Some steps must always come out the same: run the tests, check the diff, merge only when the build is green. Others need a model: plan the work, write the change, decide what a failure means.

[OpenSOP](https://github.com/opensop/opensop) lets you set that dial step by step, in one file. Scripts handle the steps that must be certain, and the agent handles the steps that need thinking. This plugin makes Hermes the worker on that line, and adds three things a prompt or a skill can't: Hermes is re-anchored on its step every turn, is nudged to finish its step before it wraps up, and has its attempts to approve its own change refused.

- **Scripts decide done.** The agent submits its step; the next script step checks the work. If the tests fail, the run fails at that step, with the real reason, until the agent fixes it.
- **Hermes doesn't have to remember anything.** The process lives in the file and the run lives in a folder on disk, not in the conversation. Compaction or a new session loses nothing, and a step cut off mid-way is marked interrupted and can be re-run.
- **Any agent can pick up the run.** A run started in Hermes can be finished by another Hermes, by Claude Code or Codex through the `opensop` CLI, or by a person.
- **Every step leaves a receipt.** You audit evidence, not transcripts.

## What it adds to Hermes

| | |
|---|---|
| `sop_run` | Start a run of a process file. Script steps run at once; the run stops at the first step that needs the agent or a person. |
| `sop_status` | Where a run stands: the step it waits on, what that step expects, or why it failed. Give it any run id to pick up a run started elsewhere (with `workdir` if it was another project). |
| `sop_submit` | Finish the step the run waits on. The run continues and its scripts check the work. |
| `sop_retry` | After fixing a failure, re-run the failed or interrupted step and continue (the CLI's `opensop heal <run_id> --apply`). |
| Re-anchoring (`pre_llm_call`) | Every turn, the agent sees which run it is on and the step it owes. This survives compaction (`post_compaction`). |
| Finish gate (`pre_verify`) | On a turn where it edited code, the agent is nudged once to finish its open step, or say what blocks it, before wrapping up. |
| Human steps stay human (`pre_tool_call`) | Approval steps and escalated judgments are for a person. `sop_submit` refuses them, an `opensop submit` for them in the terminal is blocked, and so are edits to the run's own folder. Everything the agent submits is recorded as `decided_by: agent:hermes`. |
| Approval before a run (`pre_tool_call`) | `sop_run` goes through Hermes's approval prompt, because the run's script steps execute outside Hermes's terminal approvals. The prompt names the process file (with its hash) and the inputs; a different input or a changed file asks again. |

## Install

Requirements: Hermes Agent 0.23.0 or later, and the `opensop` CLI 0.10.0 or later on `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/opensop/opensop/main/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"   # where the installer puts opensop; Hermes must see it on PATH
hermes plugins install https://github.com/opensop/hermes-opensop
hermes plugins enable opensop          # shows what the plugin provides and asks you to confirm
```

## Try it

The plugin ships one example, [`examples/change-with-tests.sop.json`](examples/change-with-tests.sop.json):

1. **setup** (script): records the starting commit and refuses a dirty tree.
2. **change** (agent): makes the requested change.
3. **tests** (script): runs your test command. The run fails here until it passes.
4. **approve** (person): reviews the diff and approves or rejects it.
5. **decision** (script): a rejected change ends the run as failed, so "completed" always means approved.

Here the test command is an input, to keep the example general. In your own processes, put the command that defines "done" in the file, so the agent can't choose an easier one.

In a git checkout with no uncommitted changes (the setup step refuses a dirty tree), ask Hermes:

> Use the OpenSOP process at `~/.hermes/plugins/opensop/examples/change-with-tests.sop.json` to fix the failing test. The test command is `npm test`.

Then try the part a prompt can't do: quit Hermes mid-run, and finish from anywhere.

```bash
opensop runs                     # find the run id
opensop status <run_id>          # where the run stands, and the exact next command
opensop submit <run_id> approve --output decision=approve --decided-by person:$USER
opensop heal <run_id> --apply     # after fixing a failed step: re-run it and continue
```

Script steps always run in the project the run was started in, wherever you type these.

Or start a fresh Hermes session and say "continue OpenSOP run `<run_id>`". Runs are kept in `~/.opensop-local` unless the project has its own `.opensop/` folder (`opensop init`); in that case, start Hermes in that project or give it the project path.

## Write your own

A process is a JSON or YAML file. Each step is a script (`automated`), work for the agent (`form`), a decision for a person (`approval`), a model call (`llm`, `judgment`), a `loop`, or a `subprocess`. Move a step along the dial as you learn: when a judgment call keeps going wrong, replace it with a script. See the [OpenSOP spec](https://github.com/opensop/opensop/blob/main/SPEC.md) and the [agent guide](https://github.com/opensop/opensop/blob/main/docs/AGENTS.md).

## Security and disclosure

- **Shell commands and agent CLIs.** Every tool runs the `opensop` CLI as a child process, in the session's working directory. A process file's script steps run as shell commands on your machine, with your permissions. Its `llm` and `judgment` steps can drive other coding-agent CLIs non-interactively (`claude`, `codex`, `cursor-agent`, `kimi`) with the permission the file declares, up to full access. Only run process files you trust, as you would any script.
- **Files.** OpenSOP writes run folders under `$OPENSOP_LOCAL_HOME`, the nearest `.opensop/` cell, or `~/.opensop-local`. The plugin keeps a small session-to-run map in its Hermes plugin data.
- **Network.** The plugin makes no network calls. A process file's own steps can (for example `webhook` or `llm` steps); that is up to the file.
- **Credentials.** The plugin reads none. A process that calls a model uses that provider's own CLI login or API key.
- **Approvals.** `sop_run` asks for your approval through Hermes's own prompt before a run's script steps execute. The process definition is pinned when the run starts; scripts it calls are read when they run. The plugin never auto-approves anything, never changes Hermes's approval settings, and never starts Hermes itself. It refuses agent submissions of human steps; it does not decide them itself.
- **Long-running work.** A tool call lasts as long as the script steps it triggers, up to `OPENSOP_HERMES_TIMEOUT` seconds (default 1800). On timeout the plugin stops the CLI and the step's child processes, and the step is marked interrupted. Nothing runs in the background.

## Limitations

- The re-anchoring and finish gate follow one run per session. Start a second run and the session follows that one. While a run waits on a person, the session keeps showing it; `sop_status` with `detach: true` stops following it.
- The finish gate fires only on turns where Hermes edited code (that is when Hermes runs `pre_verify`).
- The human-step guards are seat belts, not a sandbox. They catch `sop_submit`, `opensop submit …` in the terminal and edits that name the run's folder or files. An agent with a shell can still get around them, for example by scripting the CLI another way. For a hard boundary, decide approval steps from a separate account or machine. Steps submitted through the plugin are marked `decided_by: agent:hermes` in the receipts.
- The process file is pinned when the run starts; the scripts it calls are not. An agent that rewrites a step's script can change what that step checks. Each script step's receipt records the script's `script_sha256`, so a changed script shows up in the audit. Keep process scripts where the agent doesn't work, or under review.
- Re-anchoring adds the current step to each user turn, so long sessions carry a snapshot per turn in their history.

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
