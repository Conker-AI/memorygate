"""What goes with a deleted entity: its history snapshots, events, edges, lineage links, and
its id in observation and pattern id lists. Used by DELETE /entity/{id} and by recovery replay,
so an entity deleted before a restore leaves nothing behind after it either."""

from app.models.entity import EntityEdge, EntityEvent, EntityHistory
from app.services.lineage_forgetting import erase_links, strip_id
from sqlalchemy import delete, or_

ID_LISTS = (("observations", "entity_ids_json"), ("patterns", "applies_to_entity_ids_json"))


def erase_dependents(db, entity_id: str) -> None:
    """Delete everything that refers to `entity_id`, inside the caller's transaction (no commit).
    Each removed lineage link leaves a content-free `link` receipt, as other deletes do."""
    db.execute(delete(EntityHistory).where(EntityHistory.entity_id == entity_id))
    db.execute(delete(EntityEvent).where(EntityEvent.entity_id == entity_id))
    db.execute(delete(EntityEdge).where(
        or_(EntityEdge.from_entity_id == entity_id, EntityEdge.to_entity_id == entity_id)))
    erase_links(db, "entity", entity_id)
    for table, column in ID_LISTS:
        strip_id(db, table, column, entity_id)
