"""The backup list names only backup files: a folder that happens to match the file pattern is
not offered as a backup the download would then refuse."""

from pathlib import Path

from app.services import backup_service
from memory_stack import stack as stack


def test_a_folder_named_like_a_backup_is_not_listed(stack):
    made = stack.http.post("/system/backups", headers=stack.admin_headers())
    assert made.status_code == 200, made.text
    (Path(backup_service.BACKUP_DIR) / "memorygate-dir.json").mkdir()

    listed = stack.get("/system/backups")

    assert listed.status_code == 200, listed.text
    assert [item["filename"] for item in listed.json()["results"]] == [made.json()["filename"]]
    assert stack.get("/system/backups/memorygate-dir.json/download").status_code == 404
