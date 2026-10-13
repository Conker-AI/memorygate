# Revision-safe memory editing

Memory list/get/search responses include a positive integer `revision`. An editor should send that value as `expected_revision` in PATCH JSON, or as the DELETE query parameter. A stale value returns HTTP 409 (`revision_conflict`); reload and reconcile rather than silently retrying the edit.

For existing clients the field remains optional. Such clients do not gain stale-editor detection. SQLAlchemy optimistic version checks still reject races between loading and flushing a memory, including ORM edits made by other service paths. Bulk SQL is outside that guarantee.

Manual content edits, prior snapshots, conflict changes, audit records, and the
latest desired vector operation commit together. Qdrant remains an external
derived index, but its idempotent operation is never the only record of unfinished
work: failed or interrupted reconciliation stays pending in PostgreSQL and retries.

The additive PostgreSQL startup migration initializes existing rows to revision 1
and seeds one vector intent for existing source rows and deletion receipts. It is
idempotent and has been exercised against disposable PostgreSQL 16; deployment
must still test a backed-up copy of real user data.
