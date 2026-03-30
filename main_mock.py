"""Run agent against a local task dump with mock VM.

Usage:
    uv run python main_mock.py t20
    uv run python main_mock.py t20 t19 t25
"""

import json
import os
import sys
import time
from pathlib import Path

# ── .env loading ────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not os.environ.get(key):
            os.environ[key] = value

# ── Monkey-patch MessageToDict BEFORE importing agent ───────────────────────
import google.protobuf.json_format as _pbjf
_orig_mtd = _pbjf.MessageToDict

def _patched_mtd(message, *args, **kwargs):
    if isinstance(message, dict):
        return message
    return _orig_mtd(message, *args, **kwargs)

_pbjf.MessageToDict = _patched_mtd

# ── Now import agent and mock ──────────────────────────────────────────────
from mock_vm import MockVM
import agent
from agent import run_agent, OUTCOME_BY_NAME

MODEL_ID = os.getenv("MODEL_ID") or "grok-4-1-fast-non-reasoning"
DUMP_DIR = Path(__file__).parent / "dumps"

# Cost tracking
COST_INPUT = float(os.getenv("COST_INPUT") or "0.2")
COST_OUTPUT = float(os.getenv("COST_OUTPUT") or "0.5")

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"

# Reverse mapping: protobuf enum value → string name
OUTCOME_BY_VALUE = {v: k for k, v in OUTCOME_BY_NAME.items()}


def load_expected(task_dir: Path) -> dict:
    """Load expected.json for scoring."""
    exp_file = task_dir / "expected.json"
    if exp_file.exists():
        return json.loads(exp_file.read_text(encoding="utf-8"))
    # Fallback: no expected.json — just report what happened
    return {}


def score_task(mock: MockVM, expected: dict) -> tuple[float, list[str]]:
    """Score agent result against expected. Returns (score 0/1, detail lines)."""
    details = []

    # Get outcome string
    if mock.outcome is not None:
        outcome_str = OUTCOME_BY_VALUE.get(mock.outcome, f"UNKNOWN({mock.outcome})")
    else:
        outcome_str = "NO_ANSWER"
        details.append("no answer provided")
        return 0.0, details

    # Check outcome
    allowed = expected.get("allowed_outcomes", [])
    if allowed:
        if outcome_str in allowed:
            details.append(f"outcome {outcome_str} — OK (allowed: {', '.join(allowed)})")
        else:
            details.append(f"expected outcome {' or '.join(allowed)}, got {outcome_str}")
            return 0.0, details
    else:
        details.append(f"outcome: {outcome_str} (no expected defined)")

    # Check required writes
    for req_path in expected.get("required_writes", []):
        if req_path in mock.written_files:
            details.append(f"required write '{req_path}' — OK")
        else:
            details.append(f"missing file write '{req_path}'")
            return 0.0, details

    # Check required deletes
    for req_path in expected.get("required_deletes", []):
        if req_path in mock.deleted_files:
            details.append(f"required delete '{req_path}' — OK")
        else:
            details.append(f"missing file delete '{req_path}'")
            return 0.0, details

    # Check forbidden writes (supports glob-like "outbox/*")
    for pattern in expected.get("forbidden_writes", []):
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            violations = [f for f in mock.written_files if f.startswith(prefix + "/")]
        else:
            violations = [f for f in mock.written_files if f == pattern]
        if violations:
            for v in violations:
                details.append(f"unexpected change FileWritten '{v}'")
            return 0.0, details

    # Check forbidden deletes
    for pattern in expected.get("forbidden_deletes", []):
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            violations = [f for f in mock.deleted_files if f.startswith(prefix + "/")]
        else:
            violations = [f for f in mock.deleted_files if f == pattern]
        if violations:
            for v in violations:
                details.append(f"unexpected delete '{v}'")
            return 0.0, details

    return 1.0, details


def run_one(task_id: str) -> tuple[str, float, list[str], float]:
    """Run one task. Returns (task_id, score, details, elapsed_s)."""
    task_dir = DUMP_DIR / task_id
    if not task_dir.exists():
        return task_id, 0.0, [f"dump not found: {task_dir}"], 0.0

    instruction = (task_dir / "instruction.txt").read_text(encoding="utf-8").strip()
    expected = load_expected(task_dir)

    print(f"\n{'='*60}")
    print(f"  MOCK RUN: {task_id}")
    print(f"  instruction: {instruction[:100]}")
    print(f"  expected: {expected}")
    print(f"{'='*60}\n")

    # Create mock and monkey-patch
    mock = MockVM(task_dir)
    agent.PcmRuntimeClientSync = lambda url: mock

    t0 = time.time()
    try:
        usage = run_agent(MODEL_ID, "mock://local", instruction)
    except Exception as e:
        elapsed = time.time() - t0
        print(f"{CLI_RED}Agent error: {e}{CLI_CLR}")
        return task_id, 0.0, [f"agent error: {e}"], elapsed

    elapsed = time.time() - t0

    # Score
    score, details = score_task(mock, expected)

    # Print result
    style = CLI_GREEN if score == 1.0 else CLI_RED
    print(f"\n{style}[{task_id}] Score: {score:.2f} ({elapsed:.1f}s){CLI_CLR}")
    for d in details:
        print(f"  {d}")

    # Print agent tracking
    if mock.written_files:
        print(f"  {CLI_YELLOW}writes: {mock.written_files}{CLI_CLR}")
    if mock.deleted_files:
        print(f"  {CLI_YELLOW}deletes: {mock.deleted_files}{CLI_CLR}")
    if mock.outcome is not None:
        outcome_str = OUTCOME_BY_VALUE.get(mock.outcome, "?")
        print(f"  {CLI_BLUE}answer: {outcome_str} — {mock.answer_message}{CLI_CLR}")

    # Usage/cost
    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cost = (inp * COST_INPUT + out * COST_OUTPUT) / 1_000_000
        print(f"  {CLI_YELLOW}tokens: {inp:,} in / {out:,} out, cost: ${cost:.4f}{CLI_CLR}")

    return task_id, score, details, elapsed


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python main_mock.py t20 [t19 t25 ...]")
        sys.exit(1)

    task_ids = sys.argv[1:]
    results = []

    for tid in task_ids:
        results.append(run_one(tid))

    if len(results) > 1:
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        for tid, score, _, elapsed in results:
            style = CLI_GREEN if score == 1.0 else CLI_RED
            print(f"  {tid}: {style}{score:.2f}{CLI_CLR}  ({elapsed:.1f}s)")

        total = sum(s for _, s, _, _ in results) / len(results) * 100
        print(f"\n  TOTAL: {total:.1f}%")


if __name__ == "__main__":
    main()
