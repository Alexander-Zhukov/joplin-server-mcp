# Changelog

## v0.2.0

### Added
- `search_notes`: multi-term (AND) matching plus a `scope` (`title`/`body`/`all`)
  and `notebook_id` / `tag` filters, with title-weighted ranking. Results now
  show each note's notebook and tags.
- `list_notes`: optional `tag` filter; results now show tags.
- `get_or_create_notebook`: resolve a `/`-separated notebook path, creating any
  missing levels in a single call.

### Fixed
- Create tools no longer risk a concurrent index refresh dropping a just-created
  item before the server listing catches up. Locally-written items are protected
  until the server confirms them, making back-to-back create/read flows (such as
  repeated `get_or_create_notebook` calls) reliably idempotent.

### Thanks
- @LasseLegarth for [#5](https://github.com/Alexander-Zhukov/joplin-server-mcp/pull/5)
  — *Inherit share_id from parent notebook on create/update*. Notes and
  notebooks now inherit their parent's `share_id` on creation and when moved, so
  membership in a shared notebook is preserved. Thank you!
