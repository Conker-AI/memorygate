"""The suite stops any test that writes into the repository (see repository_guard.py)."""

import os
import shutil
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from repository_guard import REPO, RepositoryWrite, pytest_configure, take_writes
from sqlalchemy import create_engine, text


def test_writes_into_the_repository_are_stopped_and_fail_the_test(request):
    # Neither path exists, so a broken guard fails these calls instead of changing the checkout.
    target = REPO / "services" / "api" / "tests" / f"leak-{uuid.uuid4().hex}"
    source = target.with_suffix(".source")
    attempts = [
        lambda: target.write_text("x", encoding="utf-8"),
        lambda: os.close(os.open(target, os.O_CREAT | os.O_WRONLY)),
        lambda: sqlite3.connect(target),
        lambda: sqlite3.connect(target.as_uri() + "?mode=rwc", uri=True),
        lambda: create_engine(f"sqlite:///{target}").connect(),  # how every test opens a store
        lambda: target.mkdir(),
        lambda: source.replace(target),
        lambda: target.unlink(),
        lambda: shutil.rmtree(target),
    ]
    for attempt in attempts:
        with pytest.raises(RepositoryWrite):
            attempt()
    assert not target.exists()
    writes = take_writes(request.node.nodeid)  # Taken, so this test is not failed for them.
    assert len(writes) == len(attempts)
    paths = {line.split(" ", 1)[1] for line in writes}
    assert paths == {os.path.normcase(source), os.path.normcase(target)}


def test_a_relative_path_resolves_against_the_working_directory(monkeypatch, request):
    # `sqlite:///./x.db` and `BACKUP_DIR=backups` land wherever the run started: the checkout.
    monkeypatch.chdir(REPO / "services" / "api")
    name = f"leak-{uuid.uuid4().hex}.db"
    with pytest.raises(RepositoryWrite):
        create_engine(f"sqlite:///./{name}").connect()
    assert not (REPO / "services" / "api" / name).exists()
    assert len(take_writes(request.node.nodeid)) == 1


def test_reads_and_temporary_files_are_left_alone(tmp_path, request):
    assert (REPO / "README.md").read_text(encoding="utf-8")
    (REPO / "services").mkdir(exist_ok=True)  # Already there: nothing changes.
    with sqlite3.connect(tmp_path / "gate.db") as conn:
        conn.execute("CREATE TABLE t (x)")
    with sqlite3.connect((tmp_path / "gate.db").as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)
    sqlite3.connect(":memory:").close()
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1
    engine.dispose()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert take_writes(request.node.nodeid) == []


@pytest.mark.parametrize("option", [True, False], ids=["basetemp", "temp-root"])
def test_a_temporary_directory_inside_the_repository_is_refused(monkeypatch, tmp_path, option):
    inside = str(REPO / ".test-runs" / "full")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path) if option else inside)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=inside if option else None))
    with pytest.raises(pytest.UsageError, match="is inside the repository"):
        pytest_configure(config)
    config.option.basetemp = str(tmp_path)
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    pytest_configure(config)  # Outside the checkout: accepted.
