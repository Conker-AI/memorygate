import json
from datetime import UTC

import pytest
from app.core.db import Base
from app.models.entity import Entity, EntityEdge
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.schemas.explorer import ExploreRequest
from app.schemas.runtime import AgentContextRequest
from app.services.memory_explorer import compact, explore
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'explore.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        db.add_all([
            Memory(id="m", agent_id="owner", text="long " * 2000, summary="Short summary"),
            Memory(id="foreign", agent_id="other", text="private other namespace"),
            Memory(id="inactive", agent_id="owner", text="withdrawn", status="needs_review"),
            Entity(id="e", agent_id="owner", entity_type="person", name="Alex"),
            Entity(id="e2", agent_id="owner", entity_type="project", name="Project"),
            EvidenceObject(id="s", agent_id="owner", source_id="connector", source_key="call", source_type="audio", title="Call", normalized_payload_json='{"content":"hello"}'),
            EvidenceObject(id="invalid", agent_id="owner", source_id="connector", source_key="call", source_type="audio", title="old"),
            EntityEdge(id="ee", from_entity_id="e", to_entity_id="e2", relationship_type="works_on"),
        ])
        from datetime import datetime
        db.flush()
        db.get(EvidenceObject, "invalid").invalidated_at = datetime.now(UTC)
        for identity, kind, target, relation in [("1", "entity", "e", "about"), ("2", "evidence", "s", "derived_from"),
                ("3", "memory", "foreign", "related"), ("4", "memory", "inactive", "related"), ("5", "evidence", "invalid", "derived_from")]:
            db.add(ObjectLink(id=identity, source_type="memory", source_id="m", target_type=kind, target_id=target, relationship=relation))
        db.commit()
        yield db
    engine.dispose()


def test_compact_then_expand_and_chunk_evidence(db):
    body = AgentContextRequest(query="x", compact=True)
    value = compact(db, {"query": "x", "retrieval": {}, "memories": [{"id": "m"}]}, body, "owner")
    assert value["objects"][0]["preview"] == "Short summary"
    assert value["objects"][0]["connections"] == {"about": 1, "derived_from": 1}
    assert "long long" not in json.dumps(value)
    first = explore(db, ExploreRequest(object_type="memory", object_id="m", limit=1), "owner")
    second = explore(db, ExploreRequest(object_type="memory", object_id="m", after=first["next_after"], limit=1), "owner")
    assert first["links"][0]["id"] != second["links"][0]["id"]
    assert second["next_after"] is None
    assert "private other namespace" not in json.dumps([first, second])
    text = explore(db, ExploreRequest(object_type="memory", object_id="m", operation="content", field="text", characters=100), "owner")
    assert text["content"] == ("long " * 20) and text["next_offset"] == 100
    evidence = explore(db, ExploreRequest(object_type="evidence", object_id="s", operation="content", field="normalized_payload_json"), "owner")
    assert json.loads(evidence["content"]) == {"content": "hello"}


def test_scope_filters_counts_as_well_as_content(db):
    value = explore(db, ExploreRequest(object_type="memory", object_id="m", scope="selected", memory_ids=["m"]), "owner")
    assert value["connections"] == {} and value["links"] == []
    for kind, identity, extra in [("memory", "foreign", {}), ("memory", "inactive", {}),
            ("evidence", "invalid", {}), ("memory", "m", {"scope": "none"}),
            ("entity", "e", {"scope": "selected", "memory_ids": ["m"]})]:
        with pytest.raises(HTTPException) as error:
            explore(db, ExploreRequest(object_type=kind, object_id=identity, **extra), "owner")
        assert error.value.status_code == 404


def test_reverse_connections_entity_edges_and_field_allowlist(db):
    value = explore(db, ExploreRequest(object_type="entity", object_id="e"), "owner")
    assert value["connections"] == {"about": 1, "works_on": 1}
    assert len(value["links"]) == 2
    with pytest.raises(HTTPException):
        explore(db, ExploreRequest(object_type="evidence", object_id="s", operation="content", field="raw_payload_json"), "owner")


def test_library_pages_search_and_scopes_preserve_connections(db):
    from app.schemas.explorer import LibraryRequest
    from app.services.memory_explorer import library
    first = library(db, LibraryRequest(limit=2), "owner")
    second = library(db, LibraryRequest(limit=2, after=first["next_after"]), "owner")
    assert first["total"] == second["total"] == 4
    assert second["next_after"] is None
    assert len({(item["type"], item["id"]) for item in first["objects"] + second["objects"]}) == 4
    assert "private other namespace" not in json.dumps([first, second])
    searched = library(db, LibraryRequest(search="Short summary"), "owner")
    assert searched["total"] == 1
    assert searched["objects"][0]["connections"] == {"about": 1, "derived_from": 1}
    selected = library(db, LibraryRequest(scope="selected", memory_ids=["m"]), "owner")
    assert selected["total"] == 1 and selected["objects"][0]["connections"] == {}
    assert library(db, LibraryRequest(scope="none"), "owner")["total"] == 0
    assert library(db, LibraryRequest(search="%"), "owner")["total"] == 0


def test_read_tier_cannot_switch_namespace_in_body():
    from app.routes import runtime
    from app.schemas.runtime import MemoryQuestionRequest
    for handler, body in [(runtime.agent_context, AgentContextRequest(query="x", agent_id="other")),
                          (runtime.ask_memorygate, MemoryQuestionRequest(question="x", agent_id="other")),
                          (runtime.explore_memory, ExploreRequest(object_type="memory", object_id="m", agent_id="other"))]:
        with pytest.raises(HTTPException) as error:
            handler(body, "owner", "read")
        assert error.value.status_code == 403


def test_mcp_context_and_followup_use_same_operator_scope(db, monkeypatch):
    import importlib.util
    from io import BytesIO
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "mcp" / "memorygate_mcp.py"
    spec = importlib.util.spec_from_file_location("explorer_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MEMORYGATE_KEY", "test-key")
    monkeypatch.setenv("MEMORYGATE_SCOPE", "selected")
    monkeypatch.setenv("MEMORYGATE_MEMORY_IDS", '["m"]')
    monkeypatch.setenv("MEMORYGATE_AGENT_ID", "owner")
    requests = []
    def endpoint(request, **kwargs):
        body = json.loads(request.data)
        requests.append(body)
        assert request.get_header("X-agent-id") == "owner"
        if request.full_url.endswith("/context"):
            assert body["compact"] is True
            result = compact(db, {"query": "x", "retrieval": {}, "memories": [{"id": "m"}]}, AgentContextRequest(**body), "owner")
        else:
            result = explore(db, ExploreRequest(**body), "owner")
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(module, "urlopen", endpoint)
    assert module.context({"query": "x"})["objects"][0]["connections"] == {}
    result = module.explore({"object_type": "memory", "object_id": "m", "operation": "content", "field": "text", "characters": 50})
    assert len(result["content"]) == 50
    assert all(item["scope"] == "selected" and item["memory_ids"] == ["m"] for item in requests)
    with pytest.raises(ValueError):
        module.explore({"object_type": "memory", "object_id": "m", "scope": "all"})
