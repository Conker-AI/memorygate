# Bootstrap read authority survives owner changes

Environment bootstrap configuration seeds one missing read-key authority. A
permanent `bootstrap_authorities` record binds it to the original key ID, rather
than relying on its editable label, secret hash or namespace.

Restart does not undo revocation, rename, secret rotation or namespace changes.
Changing the bootstrap environment key or label also does not reset an existing
binding. If the owner deletes the bound key, startup refuses to recreate it:
remove bootstrap configuration and provision/use an owner-issued read key.

For a pre-upgrade database without a binding, an existing label or matching hash
can identify the original row, including a revoked row. If existing keys cannot
be matched, initialization stops for owner review. It cannot reliably distinguish
a previously renamed-and-rotated bootstrap key from an unrelated key, so it does
not mint another authority. A fresh empty key store can still bootstrap normally.

The added table is created by the existing metadata initialization. Key insertion
and binding commit together; concurrent seeders use the winning binding. No raw
secret or additional secret hash is stored in the binding. Database administrators
can still explicitly change database policy; this is restart safety, not a sandbox
against the host administrator.

Temporary SQLite tests exercise individual and combined owner changes, deletion,
legacy ambiguity and concurrent initial seeding. No owner database was migrated
and no running service was restarted for this change.
