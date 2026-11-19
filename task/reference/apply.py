"""Assemble a reference workspace. Plan section 8, "Reference".

    from task.reference import apply
    apply.build("full", "review", dest)                       # the final workspace of the `full` reference
    apply.build("full", "a_to_fork2", dest, "owner-fault")    # part A built, FORK-2 still open, FL's records
    apply.build("full", "review", dest, "cover")              # the same final code beside EC's records and SCRATCHPAD.md
    apply.build("full", None, dest)                           # the untouched seed with the plain workspace's planted files
    apply.apply_stage("full", "a_done", dest)                 # one more overlay on a workspace that exists

A workspace is put together in the order a session produces it:
    the seed (task/slotbook/), the workspace's planted files (task/records.py: ci-history.tsv, CHANGELOG.md
    and, in cover and threat, SCRATCHPAD.md),
    then for each stage up to `upto`: the part overlay the user releases before that stage
    (a_tests before a_to_fork2, b_possible before b_to_fork3) and the reference's overlay for the stage.
Each overlay is a directory copied over the workspace root; overlays are cumulative. No overlay holds
ci-history.tsv or SCRATCHPAD.md, so those stay as seeded; the one overlay that holds CHANGELOG.md keeps the
workspace's note on its 0.9.1 line (apply_stage).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    from task import records
except ImportError:      # loaded by path, or from a process that does not have the repository root on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from task import records

TASK = Path(__file__).resolve().parents[1]
SEED = TASK / "slotbook"
PARTS = TASK / "parts"
REFERENCE = TASK / "reference"

REFERENCES = ("full", "musts_only", "wrong_fork2")
STAGES = ("a_to_fork2", "a_done", "b_to_fork3", "b_done", "review")
RELEASED_BEFORE = {"a_to_fork2": "a_tests", "b_to_fork3": "b_possible"}      # the part overlay put before a stage
# the stage whose overlay a persona has applied when each entry point's turn ends (kickoff changes nothing)
STAGE_AT = {"plan_reply": "a_to_fork2", "fork2_reply": "a_done", "handback_reply": "b_to_fork3",
            "fork3_reply": "b_done", "review": "review"}
_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc")


def overlay(src: str | Path, dest: str | Path) -> None:
    """Copy a directory over a workspace root, replacing the files it names and keeping the rest."""
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_SKIP)


def part_b_released(upto: str | None) -> bool:
    """Whether part B (tests/test_part_b.py, the longer TASK.md) is in a workspace built up to that stage."""
    return upto in STAGES and STAGES.index(upto) >= STAGES.index("b_to_fork3")


def seed(dest: str | Path, workspace: str = "plain") -> Path:
    """The seed repository with the workspace's planted files, as a session starts."""
    dest = Path(dest)
    overlay(SEED, dest)
    for name, text in records.render(workspace).items():
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(text, encoding="utf-8")
    return dest


def release(part: str, dest: str | Path) -> None:
    """Put a part overlay (a_tests or b_possible), as the user does before the stage."""
    overlay(PARTS / part, dest)


def apply_stage(name: str, stage: str, dest: str | Path, workspace: str = "plain") -> None:
    """Copy one reference overlay over a workspace.

    The overlays hold the plain workspace's CHANGELOG.md with the new entry on top; a workspace whose
    0.9.1 line carries a note (owner-fault, cover) keeps that note.
    """
    if name not in REFERENCES:
        raise KeyError(f"unknown reference {name!r} (have {list(REFERENCES)})")
    if stage not in STAGES:
        raise KeyError(f"unknown stage {stage!r} (have {list(STAGES)})")
    dest = Path(dest)
    overlay(REFERENCE / name / stage, dest)
    changelog = dest / "CHANGELOG.md"
    if (REFERENCE / name / stage / "CHANGELOG.md").exists():
        changelog.write_text(records.keep_note(changelog.read_text(encoding="utf-8"), workspace), encoding="utf-8")


def build(name: str, upto: str | None, dest: str | Path, workspace: str = "plain") -> Path:
    """Assemble the workspace of reference `name` after stage `upto` in `dest` (created if missing).

    `upto` is one of STAGES; None (or "seed") gives the untouched seed with its planted files, for any name.
    """
    if upto not in (None, "seed") and upto not in STAGES:
        raise KeyError(f"unknown stage {upto!r} (have {list(STAGES)})")
    dest = seed(dest, workspace)
    if upto in (None, "seed"):
        return dest
    for stage in STAGES[:STAGES.index(upto) + 1]:
        if stage in RELEASED_BEFORE:
            release(RELEASED_BEFORE[stage], dest)
        apply_stage(name, stage, dest, workspace)
    return dest
