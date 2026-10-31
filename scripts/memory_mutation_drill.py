"""Break each guarantee in an isolated copy; collection errors never count as a catch."""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    # A restart finds the key through its bootstrap binding and must hand it back untouched.
    (
        "bootstrap-reactivates-revoked-key",
        "services/api/app/services/auth_settings_service.py",
        "        return row\n    existing = db.query(AgentAccessKey).all()",
        (
            "        row.revoked = False\n        db.commit()\n"
            "        return row\n    existing = db.query(AgentAccessKey).all()"
        ),
    ),
    (
        "bootstrap-overwrites-owner-rotation",
        "services/api/app/services/auth_settings_service.py",
        "        return row\n    existing = db.query(AgentAccessKey).all()",
        (
            "        row.key_hash = _hash_key(raw_key)\n        db.commit()\n"
            "        return row\n    existing = db.query(AgentAccessKey).all()"
        ),
    ),
    # A key renamed and rotated before bindings existed must stop startup, not get a copy.
    (
        "bootstrap-duplicates-renamed-key",
        "services/api/app/services/auth_settings_service.py",
        "    if row is None and existing:",
        "    if False:",
    ),
    (
        "russian-discarded",
        "services/api/app/services/signal_filter.py",
        "or re.search(russian, lower)",
        "or False",
    ),
    (
        "short-statements-discarded",
        "services/api/app/services/runtime_pipeline.py",
        "if value >= 0.3:",
        "if value >= 0.3 and len(content.split()) >= 4:",
    ),
    (
        "duplicate-delivery",
        "services/api/app/services/conversation_memory.py",
        "if receipt.fingerprint:",
        "if False:",
    ),
    (
        "resurrect-forgotten-evidence",
        "services/api/app/services/conversation_memory.py",
        'if receipt.state == "deleted":',
        "if False:",
    ),
    (
        "partial-evidence-commit",
        "services/api/app/services/conversation_memory.py",
        "db.add(evidence)",
        "db.add(evidence); db.commit()",
    ),
    # A failed vector write stays pending in the outbox and is retried later.
    (
        "lost-index-retry",
        "services/api/app/services/vector_outbox.py",
        (
            "                row.next_attempt_at = datetime.now(UTC) + timedelta(\n"
            "                    seconds=min(300, 2 ** min(row.attempts, 8))\n"
            "                )"
        ),
        '                row.status = "complete"',
    ),
    (
        "unscoped-ingestion",
        "services/api/app/routes/conversation.py",
        "if len(expected) < 16 or not key or not secrets.compare_digest(expected.encode(), key.encode()):",
        "if False:",
    ),
    (
        "backup-loses-tombstones",
        "services/api/app/services/backup_service.py",
        "MemoryRevision, MemoryConflict, ConversationReceipt,",
        "MemoryRevision, MemoryConflict,",
    ),
    (
        "invalid-source-still-recalled",
        "services/api/app/routes/runtime.py",
        'memory.source_type in {"automatic_listener", "owner_statement"}',
        'memory.source_type == "automatic_listener"',
    ),
]
SOURCE = "services/api/app"
CASES.extend([
    ("unbudgeted-hosted-dispatch", "services/api/app/services/ollama_service.py",
     '            record_refusal(db, config["model"], len(system) + len(prompt), max_tokens)\n            return None',
     '            record_refusal(db, config["model"], len(system) + len(prompt), max_tokens)\n            httpx.Client(timeout=90)\n            return None'),
    ("refused-cost-audit-lost", "services/api/app/services/hosted_budget.py",
     '    db.commit()', '    db.rollback()'),
])
CASES.extend([
    ("collection-inspection-falsely-healthy", "services/api/app/services/qdrant_store.py",
     '    if unverified:', '    if False:'),
])
CASES.extend([
    ("oversized-ingestion-retryable", "services/api/app/routes/conversation.py",
     '"retryable": False', '"retryable": True'),
    ("ingestion-cap-removed", "services/api/app/routes/conversation.py",
     'max_length=MAX_CONTENT_CHARACTERS', 'max_length=32000'),
])
CASES.extend([
    ("cryptography-pin-before-pkcs7-fix", "services/api/requirements.txt",
     'cryptography==50.0.0', 'cryptography==46.0.1'),
])
CASES.extend([
    ("quote-overwrites-calculated-cost", "services/api/app/services/hosted_budget.py",
     'quote.update(status="owner_supplied_estimate", upper_bound_microusd=total)',
     'quote.update(supplied)'),
])
SAFETY_TEST = "services/api/tests/test_audit_safety.py"
TEST = "services/api/tests/test_conversation_memory.py"
BOOTSTRAP_TEST = "services/api/tests/test_bootstrap_revocation.py"
# The repository-write guard, so the copied tests run exactly as they do in the suite.
GUARD = ["services/api/tests/conftest.py", "services/api/tests/repository_guard.py"]
IMPORT = "services/api"


def stale_anchors() -> list[str]:
    """Every mutant whose anchor no longer marks exactly one place in the code."""
    stale = []
    for name, filename, old, _new in CASES:
        path = ROOT / filename
        count = path.read_text(encoding="utf-8").count(old) if path.is_file() else 0
        if count != 1:
            stale.append(f"{name} ({filename}: anchor found {count}x, expected 1)")
    return stale


def main():
    # Checked before any run: a mutant that cannot be applied proves nothing, so none is skipped.
    if stale := stale_anchors():
        print("Mutation anchors changed; re-anchor these on the code that keeps the same rule:")
        print("\n".join(f"  {line}" for line in stale))
        return 1
    # In the OS temp directory, never the checkout: the tests refuse a run inside the repository.
    run = Path(tempfile.mkdtemp(prefix="memory-mutations-"))
    for name, filename, old, new in [("baseline", None, None, None), *CASES]:
        target = run / name
        shutil.copytree(
            ROOT / SOURCE, target / SOURCE, ignore=shutil.ignore_patterns("__pycache__")
        )
        (target / TEST).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / TEST, target / TEST)
        shutil.copyfile(ROOT / BOOTSTRAP_TEST, target / BOOTSTRAP_TEST)
        shutil.copyfile(ROOT / SAFETY_TEST, target / SAFETY_TEST)
        for guard in GUARD:
            shutil.copyfile(ROOT / guard, target / guard)
        shutil.copyfile(ROOT / "services/api/requirements.txt", target / "services/api/requirements.txt")
        if filename:
            file = target / filename
            source = file.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(
                    f"Mutant {name} no longer matches exactly one site; update the drill"
                )
            file.write_text(source.replace(old, new), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(target / IMPORT) + os.pathsep + env.get("PYTHONPATH", "")
        report = target / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                TEST,
                BOOTSTRAP_TEST,
                SAFETY_TEST,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",  # beside the copy, not inside it: the copied guard refuses that
                str(run / f"{name}-temp"),
                "--junitxml",
                str(report),
            ],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (target / "output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        suites = ET.parse(report).getroot().iter("testsuite") if report.exists() else []
        suites = list(suites)
        failed = sum(int(suite.get("failures", 0)) for suite in suites)
        errors = sum(int(suite.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
        valid = (
            bool(suites)
            and not errors
            and not skipped
            and (
                result.returncode == 0
                if name == "baseline"
                else result.returncode == 1 and failed > 0
            )
        )
        if not valid:
            print(f"FAILED: {name}; inspect {target / 'output.txt'}")
            return 1
        print(f"{name}: {'passed' if name == 'baseline' else 'caught'}", flush=True)
    print(f"All {len(CASES)} mutants caught. Evidence: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
