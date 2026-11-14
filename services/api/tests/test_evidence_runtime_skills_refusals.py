"""Evidence, runtime job and skill routes: a missing or foreign record is 404, a disabled source is
409, and a refusal changes nothing. Every call goes through the real HTTP API (coverage round 5)."""

from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from memory_stack import stack as stack
from sqlalchemy import func, select

SOURCE = {"source_key": "notes", "source_type": "manual", "label": "Notes"}
OTHER = "companion"


def post(stack, path, body=None, agent="owner"):
    return stack.http.post(path, headers=stack.admin_headers(agent), json=body or {})


def count(stack, model):
    with stack.sessions() as db:
        return db.scalar(select(func.count()).select_from(model))


# --- Evidence sources and objects --------------------------------------------


def test_the_source_list_is_sorted_by_label_and_empty_before_any_exists(stack):
    assert stack.get("/evidence/sources").json() == {"results": []}
    post(stack, "/evidence/sources", {**SOURCE, "source_key": "zed", "label": "Zed"})
    post(stack, "/evidence/sources", SOURCE)
    labels = [row["label"] for row in stack.get("/evidence/sources").json()["results"]]
    assert labels == ["Notes", "Zed"]


def test_an_unknown_source_cannot_rotate_its_key_or_take_evidence(stack):
    assert post(stack, "/evidence/sources/nobody/rotate-ingest-key").status_code == 404
    refused = post(stack, "/evidence", {"source_key": "Nobody", "title": "x", "summary": "x"})
    assert refused.status_code == 404 and "nobody" in refused.text
    assert count(stack, EvidenceObject) == 0


def test_a_disabled_source_is_refused_with_409_and_the_list_filters_by_source(stack):
    post(stack, "/evidence/sources", SOURCE)
    kept = post(stack, "/evidence", {"source_key": "notes", "title": "Kept", "summary": "kept"})
    assert kept.status_code == 200, kept.text
    post(stack, "/evidence/sources", {**SOURCE, "enabled": False})
    refused = post(stack, "/evidence", {"source_key": "notes", "title": "Late", "summary": "late"})
    assert refused.status_code == 409 and count(stack, EvidenceObject) == 1
    post(stack, "/evidence/sources", {**SOURCE, "source_key": "other", "label": "Other"})
    listed = stack.get("/evidence?source_key=%20Notes%20").json()["results"]
    assert [row["title"] for row in listed] == ["Kept"]
    assert stack.get("/evidence?source_key=other").json()["results"] == []


# --- Runtime ingest and jobs --------------------------------------------------


def test_ingest_into_an_unknown_or_disabled_source_is_404_and_stores_nothing(stack):
    body = {"source_key": "notes", "content": "hello"}
    assert stack.http.post("/runtime/ingest", headers=stack.admin_headers(), json=body).status_code == 404
    post(stack, "/evidence/sources", {**SOURCE, "enabled": False})
    assert stack.http.post("/runtime/ingest", headers=stack.admin_headers(), json=body).status_code == 404
    assert count(stack, EvidenceObject) == 0


def test_a_job_is_listed_by_status_retried_once_and_never_for_another_agent(stack):
    post(stack, "/evidence/sources", SOURCE)
    sent = stack.http.post(
        "/runtime/ingest",
        headers=stack.admin_headers(),
        json={"source_key": "notes", "content": "I walk on Sundays"},
    )
    assert sent.status_code == 200, sent.text
    jobs = stack.get("/runtime/jobs").json()["results"]
    assert len(jobs) == 1
    job = jobs[0]
    assert stack.get(f"/runtime/jobs?status={job['status']}").json()["results"] == jobs
    assert stack.get("/runtime/jobs?status=no-such-state").json()["results"] == []
    assert stack.get("/runtime/jobs", OTHER).json()["results"] == []

    assert post(stack, f"/runtime/jobs/{job['id']}/retry", agent=OTHER).status_code == 404
    assert post(stack, "/runtime/jobs/job_unknown/retry").status_code == 404
    retried = post(stack, f"/runtime/jobs/{job['id']}/retry")
    assert retried.status_code == 200 and retried.json() == {"status": "queued", "job_id": job["id"]}


def test_a_job_whose_evidence_is_gone_cannot_be_retried(stack):
    post(stack, "/evidence/sources", SOURCE)
    stack.http.post(
        "/runtime/ingest",
        headers=stack.admin_headers(),
        json={"source_key": "notes", "content": "gone soon"},
    )
    job = stack.get("/runtime/jobs").json()["results"][0]
    with stack.sessions() as db:
        db.delete(db.get(EvidenceObject, job["evidence_id"]))
        db.commit()
    refused = post(stack, f"/runtime/jobs/{job['id']}/retry")
    assert refused.status_code == 404 and "Evidence" in refused.text


def test_invalidating_unknown_or_foreign_evidence_is_404(stack):
    post(stack, "/evidence/sources", SOURCE)
    made = post(stack, "/evidence", {"source_key": "notes", "title": "Mine", "summary": "mine"})
    evidence_id = made.json()["id"]
    url = f"/runtime/evidence/{evidence_id}/invalidate?reason=wrong"
    assert stack.http.post(url, headers=stack.admin_headers(OTHER)).status_code == 404
    unknown = "/runtime/evidence/ev_unknown/invalidate?reason=wrong"
    assert stack.http.post(unknown, headers=stack.admin_headers()).status_code == 404
    with stack.sessions() as db:
        assert db.get(EvidenceObject, evidence_id).invalidated_at is None


# --- Skills --------------------------------------------------------------------

SKILL = {"title": "Check amounts", "body": "Look at the amount first.", "linked_tools": ["pay.charge"]}


def skill(stack, agent="owner", **body):
    created = post(stack, "/skills", {**SKILL, **body}, agent)
    assert created.status_code == 200, created.text
    return created.json()["skill"]["id"]


def test_a_skill_of_another_agent_or_a_plain_memory_is_not_found(stack):
    mine = skill(stack)
    assert stack.get(f"/skills/{mine}", OTHER).status_code == 404
    other = stack.http.patch(f"/skills/{mine}", headers=stack.admin_headers(OTHER), json={"title": "x"})
    assert other.status_code == 404
    assert stack.get("/skills/mem_unknown").status_code == 404
    memory = stack.write("I always walk home the long way when it is grey", source_type="user")
    assert stack.get(f"/skills/{memory['id']}").status_code == 404
    with stack.sessions() as db:
        assert db.get(Memory, mine).summary == "Check amounts"


def test_a_patch_changes_only_the_given_fields_and_keeps_the_rest(stack):
    mine = skill(stack)

    def patch(body):
        return stack.http.patch(f"/skills/{mine}", headers=stack.admin_headers(), json=body)

    assert patch({"title": "Renamed"}).status_code == 200
    changed = patch({"linked_tools": ["mail.send"], "version": "2", "active": False}).json()["skill"]
    assert changed["title"] == "Renamed" and changed["body"] == SKILL["body"]
    assert changed["linked_tools"] == ["mail.send"] and changed["version"] == "2"
    assert changed["active"] is False
    again = patch({"active": True, "body": "New body."}).json()["skill"]
    assert again["active"] is True and again["linked_tools"] == ["mail.send"] and again["body"] == "New body."


def test_an_old_skill_that_stored_a_plain_list_of_tools_still_reads_and_patches(stack):
    mine = skill(stack)
    with stack.sessions() as db:
        db.get(Memory, mine).tags_json = '["pay.charge"]'
        db.commit()
    read = stack.get(f"/skills/{mine}").json()
    assert read["linked_tools"] == ["pay.charge"] and read["active"] is True
    with stack.sessions() as db:
        db.get(Memory, mine).tags_json = "{not json"
        db.commit()
    patched = stack.http.patch(f"/skills/{mine}", headers=stack.admin_headers(), json={"title": "Fixed"})
    assert patched.status_code == 200 and patched.json()["skill"]["title"] == "Fixed"
