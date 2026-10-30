"""The mutation drill refuses to run while a mutant's anchor no longer marks one place in the code."""

import importlib.util
from pathlib import Path

DRILL = Path(__file__).resolve().parents[3] / "scripts" / "memory_mutation_drill.py"
BACKUP = "services/api/app/services/backup_service.py"


def load_drill():
    spec = importlib.util.spec_from_file_location("memory_mutation_drill", DRILL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_mutant_still_marks_one_place_in_the_code():
    assert load_drill().stale_anchors() == []


def test_a_moved_anchor_stops_the_drill_before_anything_runs(monkeypatch, capsys):
    drill = load_drill()
    moved = [
        ("anchor-gone", BACKUP, "this line is not in the file", "pass"),
        ("anchor-ambiguous", BACKUP, "from app.models", "pass"),
        ("file-gone", "services/api/app/services/no_such_service.py", "pass", "return"),
    ]
    monkeypatch.setattr(drill, "CASES", [*drill.CASES, *moved])

    def must_not_run(*args, **kwargs):
        raise AssertionError("the drill started although an anchor had moved")

    monkeypatch.setattr(drill.tempfile, "mkdtemp", must_not_run)
    monkeypatch.setattr(drill.subprocess, "run", must_not_run)
    assert drill.main() == 1
    report = capsys.readouterr().out
    assert f"anchor-gone ({BACKUP}: anchor found 0x, expected 1)" in report
    assert "anchor-ambiguous" in report and "file-gone" in report
    assert "russian-discarded" not in report
