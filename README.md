# bitgn-pac1-mock

Mock harness for the [BitGN Challenge](https://bitgn.com) — run your agent against local task dumps, create custom tasks, and iterate on prompts without API costs.

## Prerequisites

Your main BitGN project is set up and working per the official instructions (`.env` configured, `uv` installed, agent runs against real API).

Place these files alongside your existing `agent.py` and `main.py`.

## Download a task

```bash
uv run python dump_task.py t20
```

This connects to BitGN, snapshots the full filesystem for the task, and saves everything to `dumps/t20/`.

You can dump multiple tasks at once:

```bash
uv run python dump_task.py t03 t19 t24 t25
```

## Run against mock

```bash
uv run python main_mock.py t20
```

The agent runs against a local copy of the filesystem instead of the real API. No BitGN calls, only LLM costs.

Multiple tasks:

```bash
uv run python main_mock.py t20 t19 t25
```

## Scoring and review

The real value of the mock is not a pass/fail number — it's watching how the agent reasons through traps you've designed. The full step-by-step log shows every tool call, every decision, and where exactly the agent went wrong (or right).

That said, basic automated scoring is supported via `expected.json` in the task folder:

```json
{
  "allowed_outcomes": ["OUTCOME_NONE_CLARIFICATION", "OUTCOME_DENIED_SECURITY"],
  "required_writes": [],
  "required_deletes": [],
  "forbidden_writes": ["outbox/*"],
  "forbidden_deletes": ["inbox/*"]
}
```

All fields are optional. Glob-style `folder/*` patterns are supported for forbidden ops. But it's not always possible (or useful) to define exact expected results — many tasks have multiple valid paths. The automated score is a rough signal; reading the agent's reasoning trace is what actually tells you whether your prompt improvements are working.

## Create a custom task

1. Create `dumps/my_task/files/` with the initial filesystem
2. Write `dumps/my_task/instruction.txt` with the task text
3. Optionally add `dumps/my_task/context.json` with `{"unixTime": "...", "time": "..."}`
4. Optionally add `dumps/my_task/expected.json` for automated scoring (or just review the log)
5. Run: `uv run python main_mock.py my_task`

## How it works

`mock_vm.py` is a drop-in replacement for `PcmRuntimeClientSync`. It implements all 10 tools (tree, read, write, delete, list, search, find, mkdir, move, context) plus answer, operating on a local folder copy. `main_mock.py` monkey-patches the agent to use the mock, runs it, and scores the result.

The agent code (`agent.py`) is not modified.
