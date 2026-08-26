# Changelog

## v0.3.1

### Added
- `append_to_note`: `position` accepts `before` and `after`, which place the
  text beside a named section rather than inside it — above its heading, or past
  its whole span with subsections included. Both require `section`, ignore
  `separator`, and are strictly additive: no existing byte is rewritten and a
  blank line is inserted only where the join would otherwise glue the block to
  its neighbour (an existing wider gap is left alone).

  This completes the set shipped in v0.3.0, which could not express "put this
  above that section": in a newest-first log whose first heading is preceded by
  a preamble, `position="start"` lands above the preamble, so the only route was
  a `replace_in_note` anchored on the heading.

## v0.3.0

### Added
- Partial note edits, resolved server-side so a client only sends the fragment
  it is changing instead of the whole body:
  - `append_to_note`: add text to a note, or under a given heading, at either
    end of it. `separator` controls the join (a blank line by default, `\n` for
    table rows), `if_absent` makes a re-run a no-op, `dry_run` previews.
  - `replace_in_note`: replace an exact string. A missing anchor, or a second
    unexpected match without `replace_all`, is refused rather than guessed.
  - `replace_section`: replace everything under a heading, keeping the heading.
  - `get_note_outline`: headings with line numbers and section sizes — enough to
    target an edit in a large note without reading it.
  Every write reports character/line deltas, the heading count before and after,
  and the numbered lines around the change. Sections are matched on heading text
  (bare or with its `#` level) and end at the next heading of the same or higher
  level; headings inside fenced code blocks are ignored.
- `get_note`: `raw=True` returns the body verbatim, with `:/<id>` resource
  references left intact, so it can be used as an exact anchor for
  `replace_in_note`.
- `tests/test_helpers.py`: dependency-free unit tests for the pure helpers
  (section resolution, splicing, edit reports, to-do state, serialization
  round-trip). Run with `python tests/test_helpers.py`.
- `set_todo`: set or clear a note's to-do state, completion, and due date in one
  call. Completing a note (or giving it a due date) also forces it to be a to-do;
  clearing the to-do flag also clears its due date and completion.
- `create_note`: optional `is_todo` and `due` parameters to create a note as a
  to-do with a due date (`YYYY-MM-DD` or a full ISO-8601 timestamp).
- `list_notes` and `get_all_notes`: optional `todo` filter (`all` / `open` /
  `done`). `get_all_notes` also accepts `todo_due` and `todo_completed` as
  `order_by` fields, with unset (`0`) values always sorted last.

### Changed
- `update_note` still replaces the whole body; its description now points at the
  partial-edit tools, and it shares one write path with `set_todo` so both
  preserve metadata key order identically.

### Fixed
- To-do state is now visible everywhere notes are rendered. Previously every
  to-do showed a bare `[todo]` regardless of completion. Markers now distinguish
  open to-dos (`[todo]`, with ` (due YYYY-MM-DD)` and ` OVERDUE` when applicable)
  from completed ones (`[done YYYY-MM-DD]`). `todo_due` and `todo_completed`
  (integer epoch-ms, `0` = unset) are parsed defensively and tolerate malformed
  or missing metadata without erroring.

### Thanks
- @paoloviviani for [#7](https://github.com/Alexander-Zhukov/joplin-server-mcp/pull/7)
  — *Add to-do state: rendering, filters, set_todo, create_note flags*. Joplin's
  serialized format always carried `todo_due` and `todo_completed`; this server
  never surfaced them or let a client set them. Everything under to-do state
  above is their work. Thank you!

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
