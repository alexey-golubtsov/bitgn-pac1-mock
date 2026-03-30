"""Dump a single BitGN PAC1 task: filesystem snapshot + instruction + expected scoring.

Usage:
    uv run python dump_task.py t19
    uv run python dump_task.py t03 t24 t25
"""

import json
import os
import sys
from pathlib import Path

# ── .env loading (same as main.py) ──────────────────────────────────────────
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

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    ContextRequest,
    ListRequest,
    ReadRequest,
    TreeRequest,
)
from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"
DUMP_DIR = Path(__file__).parent / "dumps"


def collect_file_paths(entry, prefix=""):
    """Recursively collect all file paths from tree result."""
    name = entry.name
    current = f"{prefix}/{name}" if prefix else name
    children = list(entry.children)
    if not children:
        # leaf = file
        yield current
    else:
        # directory: recurse
        for child in children:
            yield from collect_file_paths(child, current)


def collect_dir_paths(entry, prefix=""):
    """Recursively collect all directory paths from tree result."""
    name = entry.name
    current = f"{prefix}/{name}" if prefix else name
    children = list(entry.children)
    if children:
        yield current
        for child in children:
            yield from collect_dir_paths(child, current)


def dump_one_task(task_id: str):
    print(f"\n{'='*60}")
    print(f"  DUMPING: {task_id}")
    print(f"{'='*60}")

    harness = HarnessServiceClientSync(BITGN_URL)

    # 1. Start playground
    trial = harness.start_playground(
        StartPlaygroundRequest(
            benchmark_id=BENCHMARK_ID,
            task_id=task_id,
        )
    )
    print(f"  trial_id: {trial.trial_id}")
    print(f"  instruction: {trial.instruction[:120]}...")

    vm = PcmRuntimeClientSync(trial.harness_url)
    out = DUMP_DIR / task_id
    out.mkdir(parents=True, exist_ok=True)

    # 2. Save instruction
    (out / "instruction.txt").write_text(trial.instruction, encoding="utf-8")
    print(f"  [OK] instruction.txt")

    # 3. Context
    try:
        ctx = vm.context(ContextRequest())
        ctx_dict = MessageToDict(ctx)
        (out / "context.json").write_text(
            json.dumps(ctx_dict, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  [OK] context.json")
    except ConnectError as e:
        print(f"  [ERR] context: {e.message}")

    # 4. Full tree
    try:
        tree_result = vm.tree(TreeRequest(root="", level=0))
        # Save raw tree as protobuf dict
        tree_dict = MessageToDict(tree_result)
        (out / "tree.json").write_text(
            json.dumps(tree_dict, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  [OK] tree.json")
    except ConnectError as e:
        print(f"  [ERR] tree: {e.message}")
        # Can't continue without tree
        harness.end_trial(EndTrialRequest(trial_id=trial.trial_id))
        return

    # 5. Also save human-readable tree
    def format_tree(entry, prefix="", is_last=True):
        branch = "└── " if is_last else "├── "
        lines = [f"{prefix}{branch}{entry.name}"]
        child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
        children = list(entry.children)
        for idx, child in enumerate(children):
            lines.extend(format_tree(child, child_prefix, idx == len(children) - 1))
        return lines

    tree_lines = [tree_result.root.name or "."]
    for idx, child in enumerate(list(tree_result.root.children)):
        tree_lines.extend(
            format_tree(child, is_last=idx == len(list(tree_result.root.children)) - 1)
        )
    (out / "tree.txt").write_text("\n".join(tree_lines), encoding="utf-8")
    print(f"  [OK] tree.txt")

    # 6. Read every file
    file_paths = list(collect_file_paths(tree_result.root))
    print(f"  Found {len(file_paths)} files to read")

    files_dir = out / "files"
    files_dir.mkdir(exist_ok=True)

    read_errors = []
    for fpath in file_paths:
        # Normalize: remove leading slash or root name
        clean = fpath.lstrip("/")
        if clean.startswith(tree_result.root.name + "/"):
            clean = clean[len(tree_result.root.name) + 1:]

        # Read via API — path should start without /
        read_path = clean
        try:
            result = vm.read(ReadRequest(path=read_path))
            # Save to local mirror
            local_path = files_dir / clean
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(result.content, encoding="utf-8")
        except ConnectError as e:
            # Might be a directory that tree reported as leaf
            read_errors.append(f"{read_path}: {e.message}")

    print(f"  [OK] {len(file_paths) - len(read_errors)} files read")
    if read_errors:
        print(f"  [WARN] {len(read_errors)} read errors:")
        for err in read_errors[:10]:
            print(f"    - {err}")
        (out / "read_errors.txt").write_text("\n".join(read_errors), encoding="utf-8")

    # 7. End trial (no answer) — get score_detail
    try:
        result = harness.end_trial(EndTrialRequest(trial_id=trial.trial_id))
        expected_lines = [
            f"score: {result.score}",
            "",
            "score_detail:",
            *list(result.score_detail),
        ]
        (out / "expected.txt").write_text("\n".join(expected_lines), encoding="utf-8")
        print(f"  [OK] expected.txt (score={result.score})")
    except ConnectError as e:
        print(f"  [ERR] end_trial: {e.message}")

    print(f"\n  DONE → {out}/")


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python dump_task.py t19 [t03 t24 ...]")
        sys.exit(1)

    task_ids = sys.argv[1:]
    print(f"Dumping {len(task_ids)} tasks: {', '.join(task_ids)}")
    print(f"Output: {DUMP_DIR}/")

    for tid in task_ids:
        try:
            dump_one_task(tid)
        except Exception as e:
            print(f"  [FATAL] {tid}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
