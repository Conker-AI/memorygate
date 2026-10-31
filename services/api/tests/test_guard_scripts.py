"""Keeps the repository guard scripts honest: each rule is exercised against a throwaway tree.

`scripts/check_file_sizes.py` and `scripts/check_package_boundaries.py` gate CI, so a later
edit could quietly loosen what they enforce. Each test builds a minimal tracked tree in
`tmp_path`, runs the real script there with `cwd=tmp_path`, and checks the exit code and the
message, so a loosened rule or a reworded failure shows up as a test failure rather than in
production.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHECK_SIZES = REPO / "scripts" / "check_file_sizes.py"
CHECK_BOUNDARIES = REPO / "scripts" / "check_package_boundaries.py"


def run_script(script: Path, tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script)], cwd=tree, capture_output=True, text=True)


def tracked_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A git repo with the files staged, as `git ls-files` only sees the index."""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def source(lines: int) -> str:
    """A file with exactly `lines` newlines, as check_file_sizes counts them."""
    return "pass\n" * lines


# --- scripts/check_file_sizes.py: line counts and file-size-allowlist.txt ---


def test_file_at_the_limit_passes(tmp_path):
    tree = tracked_tree(tmp_path, {"src.py": source(600)})
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "file sizes ok (limit 600, 0 allowlisted files)" in result.stdout


def test_file_over_the_limit_fails(tmp_path):
    tree = tracked_tree(tmp_path, {"src.py": source(601)})
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 1
    assert "src.py: 601 lines, limit 600. Split it by concern." in result.stdout


def test_allowlisted_file_at_its_ceiling_passes(tmp_path):
    files = {"src.py": source(700), "file-size-allowlist.txt": "src.py 700\n"}
    tree = tracked_tree(tmp_path, files)
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "file sizes ok (limit 600, 1 allowlisted files)" in result.stdout


def test_allowlisted_file_over_its_ceiling_fails(tmp_path):
    files = {"src.py": source(701), "file-size-allowlist.txt": "src.py 700\n"}
    tree = tracked_tree(tmp_path, files)
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 1
    assert (
        "src.py: grew to 701 lines; it is allowlisted at 700 and may only shrink." in result.stdout
    )


def test_allowlisted_file_that_shrank_fails_asking_to_lower_the_entry(tmp_path):
    files = {"src.py": source(650), "file-size-allowlist.txt": "src.py 700\n"}
    tree = tracked_tree(tmp_path, files)
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 1
    assert "src.py: shrank to 650 lines; lower its entry in file-size-allowlist.txt from 700." in (
        result.stdout
    )


def test_allowlisted_file_at_the_limit_fails_asking_to_delete_the_line(tmp_path):
    files = {"src.py": source(600), "file-size-allowlist.txt": "src.py 700\n"}
    tree = tracked_tree(tmp_path, files)
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 1
    message = (
        "src.py: now 600 lines, under the limit; delete its line from file-size-allowlist.txt."
    )
    assert message in result.stdout


def test_allowlisted_path_that_is_not_tracked_fails(tmp_path):
    tree = tracked_tree(tmp_path, {"file-size-allowlist.txt": "ghost.py 700\n"})
    result = run_script(CHECK_SIZES, tree)
    assert result.returncode == 1
    message = "ghost.py: listed in file-size-allowlist.txt but no longer tracked; delete the line."
    assert message in result.stdout


# --- scripts/check_package_boundaries.py: loose modules and package-boundaries.txt edges ---


def boundary_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A tree shaped like the repo: app modules under services/api, the allowlist at the root.

    The script walks services/api with rglob and reads package-boundaries.txt from its cwd
    (the tree root), so git is not needed.
    """
    for name, text in files.items():
        path = (
            tmp_path / name
            if name == "package-boundaries.txt"
            else tmp_path / "services/api" / name
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_module_loose_in_app_fails(tmp_path):
    # An empty allowlist file: the script, unlike check_file_sizes.py, crashes when it is absent.
    tree = boundary_tree(tmp_path, {"app/loose.py": "pass\n", "package-boundaries.txt": ""})
    result = run_script(CHECK_BOUNDARIES, tree)
    assert result.returncode == 1
    message = (
        "services/api/app/loose.py: modules live in a package named for their type; "
        "only main.py sits in app/."
    )
    assert message in result.stdout


def test_unlisted_cross_package_import_fails_and_names_the_edge(tmp_path):
    tree = boundary_tree(
        tmp_path,
        {
            "app/core/thing.py": "from app.services import helper\n",
            "app/services/helper.py": "pass\n",
            "package-boundaries.txt": "",
        },
    )
    result = run_script(CHECK_BOUNDARIES, tree)
    assert result.returncode == 1
    assert "core -> services" in result.stdout
    assert "services/api/app/core/thing.py" in result.stdout
    assert "a new cross-package import" in result.stdout


def test_listed_cross_package_import_passes(tmp_path):
    tree = boundary_tree(
        tmp_path,
        {
            "app/core/thing.py": "from app.services import helper\n",
            "app/services/helper.py": "pass\n",
            "package-boundaries.txt": "core -> services\n",
        },
    )
    result = run_script(CHECK_BOUNDARIES, tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "package boundaries ok (1 cross-package edges, all listed)" in result.stdout


def test_listed_edge_no_longer_imported_fails(tmp_path):
    tree = boundary_tree(
        tmp_path,
        {"app/core/thing.py": "pass\n", "package-boundaries.txt": "core -> services\n"},
    )
    result = run_script(CHECK_BOUNDARIES, tree)
    assert result.returncode == 1
    message = (
        "core -> services: listed in package-boundaries.txt but no longer imported; "
        "delete the line."
    )
    assert message in result.stdout
