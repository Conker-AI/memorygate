"""Fail when MemoryGate's packages gain a dependency nobody decided on.

Run from the repository root: python scripts/check_package_boundaries.py
Every module under services/api/app/ lives in a package named for its type (core, models,
routes, schemas, services); app/main.py, the app entry, is the only module outside them. Two
rules hold without exception: no other module sits loose in app/, and only app/main.py
imports app.routes or app.main. Every other import that crosses packages must be listed in
package-boundaries.txt. The edges that existed when the check arrived are listed there; an
edge that disappears must be deleted, so the list only gets shorter unless someone adds a
line on purpose, with the reason in the commit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ALLOWLIST = Path("package-boundaries.txt")
ROOT = Path("services/api")
APP = ROOT / "app"


def owner(path: Path) -> str:
    """The package a file belongs to (core, models, ...), or main for app/main.py."""
    return path.parts[1] if len(path.parts) > 2 else path.stem


def imported_packages(path: Path) -> set[str]:
    package = list(path.parent.parts)
    found = set()
    for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module or "").split(".")]).strip(".")
            # `from app import models` names the package in the alias.
            modules = [f"app.{a.name}" for a in node.names] if module == "app" else [module]
        else:
            continue
        for module in modules:
            parts = module.split(".")
            if parts[0] == "app" and len(parts) > 1:
                found.add(parts[1])
    return found


def edges() -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        path = path.relative_to(ROOT)
        source = owner(path)
        for target in imported_packages(path) - {source}:
            found.setdefault((source, target), []).append(path.as_posix())
    return found


def allowlist() -> set[tuple[str, str]]:
    entries = set()
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            source, target = (part.strip() for part in line.split("->"))
            entries.add((source, target))
    return entries


def main() -> int:
    problems = [
        f"{path.as_posix()}: modules live in a package named for their type; only main.py sits in app/."
        for path in APP.glob("*.py")
        if path.name != "main.py"
    ]
    allowed = allowlist()
    found = edges()
    for (source, target), paths in sorted(found.items()):
        where = ", ".join((ROOT / p).as_posix() for p in paths[:3]) + (
            " ..." if len(paths) > 3 else ""
        )
        if target in ("routes", "main") and source != "main":
            problems.append(
                f"{source} -> {target} ({where}): only app/main.py may import app.{target}."
            )
        elif (source, target) not in allowed:
            problems.append(
                f"{source} -> {target} ({where}): a new cross-package import. Move the code to "
                f"the package it belongs to, or add the edge to {ALLOWLIST} and say why."
            )
    problems += [
        f"{source} -> {target}: listed in {ALLOWLIST} but no longer imported; delete the line."
        for source, target in sorted(allowed - set(found))
    ]
    if problems:
        print("\n".join(problems))
        return 1
    print(f"package boundaries ok ({len(found)} cross-package edges, all listed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
