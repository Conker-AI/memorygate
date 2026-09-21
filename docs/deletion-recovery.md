# Deletion evidence for recovery

New deletion operations retain content-free `deletion_receipts`: agent namespace,
object kind, stable object ID and time. Memory/skill, entity, observation, episode,
lineage-link and Pi conversation deletion paths write the receipt in their own
transaction. A failed receipt write cannot commit the associated deletion.
Links currently have no namespace column; their receipts use an empty namespace
and kind `link`, preserving that existing global identity model.

Logical backups now include this table. Existing conversation tombstones remain
the source of truth for preventing late conversation uploads and scheduling vector
deletion. This addition neither changes ordinary deletion into physical erasure
nor removes historical audit content. Pi forgetting retains its separate scrubbing
contract. No external index operation or additional request is introduced.

Coverage begins when this version is installed. An older database or backup with
no receipts cannot prove that no deletion happened. Complete restore still requires
a newer authoritative ledger, application to restored records/derived indexes,
effect reconciliation and a held recovery until those checks finish. This change
does not promote a recovered installation or claim historical deletion coverage.
