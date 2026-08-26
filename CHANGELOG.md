# Changelog

## v0.3.0

### Added
- `set_todo`: set or clear a note's to-do state, completion, and due date in one
  call. Completing a note (or giving it a due date) also forces it to be a to-do;
  clearing the to-do flag also clears its due date and completion.
- `create_note`: optional `is_todo` and `due` parameters to create a note as a
  to-do with a due date (`YYYY-MM-DD` or a full ISO-8601 timestamp).
- `list_notes` and `get_all_notes`: optional `todo` filter (`all` / `open` /
  `done`). `get_all_notes` also accepts `todo_due` and `todo_completed` as
  `order_by` fields, with unset (`0`) values always sorted last.

### Fixed
- To-do state is now visible everywhere notes are rendered. Previously every
  to-do showed a bare `[todo]` regardless of completion. Markers now distinguish
  open to-dos (`[todo]`, with ` (due YYYY-MM-DD)` and ` OVERDUE` when applicable)
  from completed ones (`[done YYYY-MM-DD]`). `todo_due` and `todo_completed`
  (integer epoch-ms, `0` = unset) are parsed defensively and tolerate malformed
  or missing metadata without erroring.

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
