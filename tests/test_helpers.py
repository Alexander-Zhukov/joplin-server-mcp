"""Unit tests for the pure helpers in app/server.py.

No network and no Joplin instance required — everything here is string
manipulation. Run with `python tests/test_helpers.py` (exit code 0 = pass).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ.setdefault("JOPLIN_SERVER_URL", "http://localhost")
os.environ.setdefault("JOPLIN_EMAIL", "test@example.com")
os.environ.setdefault("JOPLIN_PASSWORD", "test")

import server as S  # noqa: E402

FAILURES = []


def check(cond, label, extra=""):
    if not cond:
        FAILURES.append(label)
        print(f"FAIL  {label}" + (f"  <- {extra}" if extra else ""))
    else:
        print(f"ok    {label}")


def raises(fn, label, needle=""):
    try:
        fn()
    except ValueError as e:
        check(needle.lower() in str(e).lower(), f"{label} (message mentions '{needle}')", str(e))
    else:
        check(False, f"{label} raises ValueError", "no exception")


# -- Markdown heading scan --------------------------------------------------

DOC = """Intro line

## Hosts

- docker-1
- docker-2

### Networking

vlan notes

## Runbook

```bash
# not a heading, it is a shell comment
echo hi
```

~~~
## also not a heading
~~~

Done.

#nothashtag
####### seven hashes is not a heading
"""


def test_headings():
    heads = S._headings(DOC)
    titles = [h["title"] for h in heads]
    check(titles == ["Hosts", "Networking", "Runbook"], "headings found, fences ignored", titles)
    check([h["level"] for h in heads] == [2, 3, 2], "levels parsed")
    check(heads[0]["line"] == 3, "line numbers are 1-based", heads[0]["line"])
    for h in heads:
        line_start = DOC.rfind("\n", 0, h["start"]) + 1
        check(h["start"] == line_start, f"start offset of '{h['title']}' is a line start")
        check(DOC[h["start"]:].startswith("#" * h["level"] + " "), f"start offset of '{h['title']}' hits the hashes")
        check(DOC[h["content_start"] - 1] == "\n", f"content_start of '{h['title']}' is past the heading line")
    check(S._headings("") == [], "empty body has no headings")
    check(S._headings("# Only")[0]["content_start"] == len("# Only"),
          "content_start is clamped to body length for a trailing heading")
    unterminated = "## A\n\n```\n## inside fence\n"
    check([h["title"] for h in S._headings(unterminated)] == ["A"],
          "an unterminated fence swallows the rest of the body")


def test_section_spans():
    heads = S._headings(DOC)
    hosts_end = S._section_end(heads, 0, len(DOC))
    check(DOC[hosts_end:].startswith("## Runbook"),
          "a section ends at the next same-level heading, subsections included",
          repr(DOC[hosts_end:hosts_end + 20]))
    net_end = S._section_end(heads, 1, len(DOC))
    check(DOC[net_end:].startswith("## Runbook"), "a subsection ends at the next higher-level heading")
    check(S._section_end(heads, 2, len(DOC)) == len(DOC), "the last section runs to the end of the body")

    outline = S._outline(DOC)
    check(outline[0]["chars"] > outline[1]["chars"], "outline size of a parent includes its subsections")
    check(sum(1 for h in outline if "chars" in h) == 3, "every outline entry is sized")


def test_find_section():
    check(S._find_section(DOC, "Hosts")["level"] == 2, "exact title match")
    check(S._find_section(DOC, "hosts")["title"] == "Hosts", "case-insensitive match")
    check(S._find_section(DOC, "Network")["title"] == "Networking", "unique substring match")
    check(S._find_section(DOC, "## Hosts")["title"] == "Hosts", "full heading line with hashes")
    check(S._find_section(DOC, "### Networking")["title"] == "Networking", "level-qualified match")
    raises(lambda: S._find_section(DOC, "## Networking"), "wrong level does not match", "not found")
    raises(lambda: S._find_section(DOC, "Nope"), "missing section rejected", "not found")
    raises(lambda: S._find_section("no headings here", "Hosts"), "body without headings rejected", "no markdown headings")
    raises(lambda: S._find_section(DOC, "##"), "bare hashes rejected", "invalid section")

    dupes = "## Log\n\na\n\n## Other\n\nb\n\n## Log\n\nc\n"
    raises(lambda: S._find_section(dupes, "Log"), "duplicate headings rejected", "ambiguous")
    check(S._find_section(dupes, "Other")["title"] == "Other", "unique heading still resolves next to duplicates")
    # An exact match wins over a substring match, so an exact name is never ambiguous.
    both = "## Log\n\na\n\n## Log archive\n\nb\n"
    check(S._find_section(both, "Log")["title"] == "Log", "exact match beats substring match")
    raises(lambda: S._find_section(both, "og"), "ambiguous substring rejected", "ambiguous")


# -- Splicing --------------------------------------------------------------

def test_splice_whole_body():
    body = "one\n\ntwo"
    out, at = S._splice(body, 0, len(body), "three", "end", "\n\n")
    check(out == "one\n\ntwo\n\nthree", "append to whole body", repr(out))
    check(out[at:] == "three", "returned offset points at the insert", repr(out[at:]))

    out, at = S._splice(body, 0, len(body), "zero", "start", "\n\n")
    check(out == "zero\n\none\n\ntwo", "prepend to whole body", repr(out))
    check(out[at:].startswith("zero"), "prepend offset points at the insert")

    out, _ = S._splice("", 0, 0, "first", "end", "\n\n")
    check(out == "first", "append into an empty body", repr(out))

    table = "| a | b |\n| - | - |"
    out, _ = S._splice(table, 0, len(table), "| c | d |", "end", "\n")
    check(out == "| a | b |\n| - | - |\n| c | d |", "single-newline separator for table rows", repr(out))

    out, _ = S._splice("one\n\n\n\n", 0, 8, "two", "end", "\n\n")
    check(out == "one\n\ntwo", "trailing blank lines are normalised", repr(out))


def test_splice_section():
    body = "# T\n\nintro\n\n## A\n\naaa\n\n## B\n\nbbb\n"
    head = S._find_section(body, "A")
    out, at = S._splice(body, head["content_start"], head["end"], "added", "end", "\n\n")
    check(out == "# T\n\nintro\n\n## A\n\naaa\n\nadded\n\n## B\n\nbbb\n", "append at end of a section", repr(out))
    check(out[at:].startswith("added"), "section-append offset points at the insert")

    out, at = S._splice(body, head["content_start"], head["end"], "newest", "start", "\n\n")
    check(out == "# T\n\nintro\n\n## A\n\nnewest\n\naaa\n\n## B\n\nbbb\n", "insert at start of a section", repr(out))
    check(out[at:].startswith("newest"), "section-prepend offset points at the insert")

    tail = S._find_section(body, "B")
    out, _ = S._splice(body, tail["content_start"], tail["end"], "more", "end", "\n\n")
    check(out == "# T\n\nintro\n\n## A\n\naaa\n\n## B\n\nbbb\n\nmore", "append to the final section")

    empty = "## A\n\n## B\n\nbbb\n"
    head = S._find_section(empty, "A")
    out, _ = S._splice(empty, head["content_start"], head["end"], "first", "end", "\n\n")
    check(out == "## A\n\nfirst\n\n## B\n\nbbb\n", "append into an empty section", repr(out))

    # Content outside the touched span must survive untouched.
    for position in ("start", "end"):
        head = S._find_section(body, "A")
        out, _ = S._splice(body, head["content_start"], head["end"], "x", position, "\n\n")
        check(out.startswith("# T\n\nintro\n\n") and out.endswith("## B\n\nbbb\n"),
              f"neighbouring sections untouched ({position})", repr(out))


def test_resource_refs_survive():
    ref = "d" * 32
    body = f"See ![diagram](:/{ref}) and <img src=\":/{ref}\"/>\n\n## Notes\n\nold\n"
    head = S._find_section(body, "Notes")
    out, _ = S._splice(body, head["content_start"], head["end"], "new", "end", "\n\n")
    check(out.count(f":/{ref}") == 2, "resource references are preserved verbatim", out)
    check(len(S._find_resource_refs(out)) == 1, "the reference is still recognised after the edit")


# -- Reporting -------------------------------------------------------------

def test_reporting():
    body = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj"
    check(S._line_of(body, 0) == 1, "offset 0 is line 1")
    check(S._line_of(body, body.index("e")) == 5, "line number from offset")
    check(S._line_of(body, -5) == 1, "negative offset clamps to line 1")

    ctx = S._context_lines(body, body.index("e"))
    check(ctx.splitlines()[0].strip().startswith("1 |"), "context starts at line 1 near the top", ctx)
    check(len(ctx.splitlines()) == 5 + 4, "context is bounded by the radius", len(ctx.splitlines()))
    check("5 | e" in ctx, "context contains the focus line", ctx)

    long_line = "x" * 400
    check(len(S._context_lines(long_line, 0).split("| ")[1]) == 200, "long lines are truncated")

    parsed = {"title": "T", "id": "a" * 32}
    report = S._edit_report("Appended to", parsed, "## A\n\nold", "## A\n\nold\n\nnew", 11, "detail", False)
    check("Chars 9 -> 14 (+5)" in report, "report states the char delta", report)
    check("lines 3 -> 5 (+2)" in report, "report states the line delta", report)
    check("headings 1 -> 1" in report, "report states the heading count before and after", report)
    check("DRY RUN" not in report, "a real write is not labelled dry run")
    dry = S._edit_report("Appended to", parsed, "a", "a\n\nb", 3, "detail", True)
    check(dry.startswith("DRY RUN"), "a dry run is labelled")


# -- To-do state (v0.3.0) --------------------------------------------------

def test_todo_helpers():
    for value, expected in [("123", 123), ("", 0), (None, 0), ("abc", 0), (" 42 ", 42), (7, 7)]:
        check(S._coerce_int(value) == expected, f"_coerce_int({value!r}) -> {expected}", S._coerce_int(value))

    check(S._parse_due_ms("") == 0, "empty due date is unset")
    check(S._ms_to_date(S._parse_due_ms("2026-09-01")) == "2026-09-01", "bare date round-trips as UTC")
    check(S._parse_due_ms("2026-09-01T15:30:00Z") == 1788276600000, "ISO timestamp parsed")
    for junk in ("tomorrow", "01-09-2026", "2026-02-30"):
        raises(lambda j=junk: S._parse_due_ms(j), f"junk due date {junk!r} rejected", "")

    day = 86400000
    now = S._now_ms()
    check(S._todo_marker({"is_todo": False}) == "", "plain note has no marker")
    check(S._todo_marker({"is_todo": True, "todo_due": 0, "todo_completed": 0}) == "[todo]", "open to-do")
    check("OVERDUE" in S._todo_marker({"is_todo": True, "todo_due": now - day, "todo_completed": 0}),
          "a past due date is overdue")
    check("OVERDUE" not in S._todo_marker({"is_todo": True, "todo_due": now + day, "todo_completed": 0}),
          "a future due date is not overdue")
    check(S._todo_marker({"is_todo": True, "todo_due": now - day, "todo_completed": now}).startswith("[done "),
          "completion overrides overdue")
    stale = {"is_todo": True, "metadata": {"todo_due": str(now - day), "todo_completed": "0"}}
    check("OVERDUE" in S._todo_marker(stale), "an index cache without the new fields falls back to metadata")
    check(S._todo_marker({"is_todo": True}) == "[todo]", "missing to-do fields never raise")

    notes = [
        {"id": "plain", "is_todo": False, "todo_completed": 0},
        {"id": "open", "is_todo": True, "todo_completed": 0},
        {"id": "done", "is_todo": True, "todo_completed": now},
    ]
    for flt, expected in [(None, "plain,open,done"), ("all", "open,done"), ("open", "open"), ("done", "done")]:
        got, err = S._filter_by_todo(notes, flt)
        check(err is None and ",".join(n["id"] for n in got) == expected, f"todo filter {flt!r}", got)
    _, err = S._filter_by_todo(notes, "bogus")
    check(err is not None, "an invalid to-do filter is reported")


# -- Serialization round-trip ---------------------------------------------

def test_note_round_trip():
    now = S._now()
    due = S._parse_due_ms("2026-09-01")
    raw = S._note_template("a" * 32, "Title", "line one\n\nline two", "b" * 32, now,
                           is_todo=True, due_ms=due)
    parsed = S._parse_joplin_item(raw)
    check(parsed["body"] == "line one\n\nline two", "body survives serialization", repr(parsed["body"]))
    check(parsed["title"] == "Title", "title survives serialization")
    check(parsed["is_todo"] and parsed["todo_due"] == due, "to-do fields survive serialization")

    # What _put_note writes must parse back to the same note.
    meta = dict(parsed["metadata"])
    meta["updated_time"] = meta["user_updated_time"] = S._now()
    body = parsed["body"] + "\n\nappended"
    content = f"{parsed['title']}\n\n{body}\n\n" + "\n".join(f"{k}: {v}" for k, v in meta.items())
    again = S._parse_joplin_item(content)
    check(again["body"] == body, "an edited body survives the write format", repr(again["body"]))
    check(again["id"] == parsed["id"] and again["parent_id"] == parsed["parent_id"], "identity preserved")
    check(list(again["metadata"])[-1] == "type_", "metadata key order preserved (type_ stays last)")


def test_insert_block_before_and_after():
    """`before`/`after` place a sibling block and must never rewrite existing bytes."""
    log = "# Work Log\n\nNewest first.\n\n---\n\n\n## 2026-08-26 — b\n\nbbb\n\n## 2026-08-25 — a\n\naaa\n"

    def assert_additive(old, new, cut, label):
        check(new[:cut] == old[:cut], f"{label}: everything before the cut is byte-identical")
        check(new.endswith(old[cut:]), f"{label}: everything after the cut is byte-identical")
        check(len(new) > len(old), f"{label}: the body only grew")

    head = S._find_section(log, "## 2026-08-26 — b")
    entry = "## 2026-08-26 — newest\n\nnnn"
    out, at = S._insert_block(log, head["start"], entry)
    check(out[at:].startswith(entry), "before: offset points at the insert")
    check(out.index(entry) < out.index("## 2026-08-26 — b"), "before: insert precedes the heading")
    check("Newest first." in out.split(entry)[0], "before: the preamble stays above the insert")
    assert_additive(log, out, head["start"], "before")
    check(len(S._headings(out)) == len(S._headings(log)) + 1, "before: one new heading")

    # An existing blank-line gap is left exactly as it was, not normalised.
    check(out.split(entry)[0].endswith("---\n\n\n"), "before: the existing gap is untouched",
          repr(out.split(entry)[0][-8:]))

    nested = "## A\n\naaa\n\n### A1\n\nsub\n\n## B\n\nbbb\n"
    head = S._find_section(nested, "## A")
    out, at = S._insert_block(nested, head["end"], "## New\n\nnnn")
    check(out.index("## New") > out.index("sub"), "after: lands past the subsection")
    check(out.index("## New") < out.index("## B"), "after: lands before the next same-level heading")
    assert_additive(nested, out, head["end"], "after")

    tail = S._find_section(nested, "## B")
    out, at = S._insert_block(nested, tail["end"], "## Last")
    check(out.rstrip("\n").endswith("## Last"), "after: the final section appends at the very end", repr(out[-20:]))
    assert_additive(nested, out, tail["end"], "after-last")

    # Blank lines are added only where the join needs them.
    prose = "prose with no trailing newline"
    out, at = S._insert_block(prose, len(prose), "X")
    check(out == "prose with no trailing newline\n\nX", "no trailing newline -> a blank line is added", repr(out))
    out, at = S._insert_block("line\n", 5, "X")
    check(out == "line\n\nX", "single newline -> one more is added", repr(out))
    out, at = S._insert_block("line\n\n", 6, "X")
    check(out == "line\n\nX", "an existing blank line is enough", repr(out))
    out, at = S._insert_block("## A\n\naaa\n", 0, "X")
    check(out == "X\n\n## A\n\naaa\n" and at == 0, "insert at offset 0 needs no lead", repr(out))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n== {name} ==")
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
