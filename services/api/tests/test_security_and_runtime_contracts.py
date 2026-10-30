import unittest

import pytest
from app.schemas.memory import MemoryWriteRequest
from app.schemas.runtime import IngestEventRequest
from app.services import backup_service
from app.services.auth_settings_service import (
    _hash_key,
    _verify_key,
    ensure_bootstrap_agent_access_key,
    validate_new_admin_key,
)


class SecurityAndRuntimeContractTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):  # pytest's temporary directory
        self.tmp_path = tmp_path

    def test_admin_key_policy_rejects_weak_values(self):
        self.assertEqual(validate_new_admin_key("short"), "New key must be at least 14 characters long.")
        self.assertIsNotNone(validate_new_admin_key("longpasswordwithoutdigits!"))
        self.assertIsNone(validate_new_admin_key("Correct-Horse-9-Battery"))

    def test_key_hash_verification_is_not_plaintext(self):
        encoded = _hash_key("Correct-Horse-9-Battery")
        self.assertNotIn("Correct-Horse-9-Battery", encoded)
        self.assertTrue(_verify_key("Correct-Horse-9-Battery", encoded))
        self.assertFalse(_verify_key("wrong-key", encoded))

    def test_listener_contract_does_not_require_redundant_source_key(self):
        event = IngestEventRequest(content="webhook event")
        self.assertEqual(event.source_key, "")
        self.assertTrue(event.auto_process)


    def test_memory_write_contract_accepts_evidence_metadata(self):
        request = MemoryWriteRequest(
            text="Owner wants local-first AgentGate.",
            source_type="agentgate_owner_approved",
            evidence={"surface": "agentgate.chat", "session_id": "sess-1"},
        )
        self.assertEqual(request.evidence["surface"], "agentgate.chat")
        self.assertEqual(request.source_type, "agentgate_owner_approved")

    def test_bootstrap_agent_read_key_is_hashed_and_scoped(self):
        from app.core.db import Base
        from app.services.auth_settings_service import verify_agent_access_key
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{self.tmp_path / 'memorygate-test.db'}")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            row = ensure_bootstrap_agent_access_key(db, "mg_read_test_bootstrap_key_123", "agent_pi_operator")
            self.assertEqual(row.agent_id, "agent_pi_operator")
            self.assertNotIn("mg_read_test_bootstrap_key_123", row.key_hash)
            self.assertTrue(verify_agent_access_key(db, "mg_read_test_bootstrap_key_123", "agent_pi_operator"))
            self.assertFalse(verify_agent_access_key(db, "mg_read_test_bootstrap_key_123", "other_agent"))
        finally:
            db.close()
            engine.dispose()

    def test_backup_path_cannot_escape_backup_directory(self):
        previous = backup_service.BACKUP_DIR
        backup_service.BACKUP_DIR = str(self.tmp_path)
        try:
            path = self.tmp_path / "memorygate-valid.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(backup_service.resolve_backup(path.name), path)
            with self.assertRaises(FileNotFoundError):
                backup_service.resolve_backup("../../outside.json")
        finally:
            backup_service.BACKUP_DIR = previous
