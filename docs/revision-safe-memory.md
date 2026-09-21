# Revision-safe memory editing

Memory list/get/search responses include a positive integer `revision`. An editor should send that value as `expected_revision` in PATCH JSON, or as the DELETE query parameter. A stale value returns HTTP 409 (`revision_conflict`); reload and reconcile rather than silently retrying the edit.

For existing clients the field remains optional. Such clients do not gain stale-editor detection. SQLAlchemy optimistic version checks still reject races between loading and flushing a memory, including ORM edits made by other service paths. Bulk SQL is outside that guarantee.

Manual content edits, prior snapshots, conflict changes and audit records now commit together. Vector indexing remains post-commit and can report degradation independently; this does not make the external index transactional.

The additive PostgreSQL startup migration initializes existing rows to revision 1. It has not been run against user data. Temporary SQLite tests cover stale edit/delete, competing ORM writers/deleters, strict revision validation and rollback on audit/conflict failure. Deployment must still exercise the migration against a backed-up PostgreSQL staging database.
