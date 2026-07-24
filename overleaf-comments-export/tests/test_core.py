#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["mini-racer", "pytest"]
# ///
"""Unit tests for the PURE CORE of overleaf-comments-export.user.js.

The core is engine-portable (no DOM, no async). We load the userscript source into a fresh V8 via
mini-racer, using the file's own export hatch (`typeof window === 'undefined'` -> module.exports),
then call the exported functions with JSON in / JSON out.

Run deterministically:  TZ=UTC uv run overleaf-comments-export/tests/test_core.py
"""
import json
import random
import re
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

USERSCRIPT = Path(__file__).resolve().parent.parent / "overleaf-comments-export.user.js"


def make_core():
    """Fresh V8 context with the userscript's CORE exported on module.exports."""
    ctx = MiniRacer()
    ctx.eval("var module={exports:{}};\n" + USERSCRIPT.read_text())

    def call(fn, *args):
        js_args = ", ".join(json.dumps(a) for a in args)
        return json.loads(ctx.eval(f"JSON.stringify(module.exports.{fn}({js_args}))"))

    call.ctx = ctx
    return call


@pytest.fixture(scope="module")
def core():
    return make_core()


# ---- escapeLatex --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("\\", "\\textbackslash{}"),
    ("{", "\\{"),
    ("}", "\\}"),
    ("$", "\\$"),
    ("&", "\\&"),
    ("#", "\\#"),
    ("^", "\\textasciicircum{}"),
    ("_", "\\_"),
    ("%", "\\%"),
    ("~", "\\textasciitilde{}"),
    ("a_b%c{d}", "a\\_b\\%c\\{d\\}"),
    ("100%", "100\\%"),
    ("plain text", "plain text"),
])
def test_escape_latex_each(core, raw, expected):
    assert core("escapeLatex", raw) == expected


def test_escape_latex_no_double_escaping(core):
    # `\` -> `\textbackslash{}`: the introduced braces must NOT be re-escaped to `\{\}`.
    assert core("escapeLatex", "\\") == "\\textbackslash{}"
    assert core("escapeLatex", "~") == "\\textasciitilde{}"
    assert core("escapeLatex", "^") == "\\textasciicircum{}"
    for bad in ("\\textbackslash\\{\\}", "\\textasciitilde\\{\\}", "\\textasciicircum\\{\\}"):
        assert bad not in "".join(core("escapeLatex", ch) for ch in "\\~^")


# ---- collapseWhitespace -------------------------------------------------------------------------

def test_collapse_space(core):
    assert core("collapseWhitespace", "  a\n\n b\t c  ") == "a b c"
    assert core("collapseWhitespace", "results  too") == "results too"


def test_collapse_break(core):
    assert core("collapseWhitespace", "a\nb", "break") == "a \\\\ b"


# ---- formatTimestamp ----------------------------------------------------------------------------

def test_format_timestamp(core):
    assert core("formatTimestamp", 1747160820000) == "13 May, 6:27 pm"


@pytest.mark.parametrize("bad", ["NaN", "null", "\"nope\""])
def test_format_timestamp_non_finite(core, bad):
    assert core.ctx.eval(f"JSON.stringify(module.exports.formatTimestamp({bad}))") == '""'


# ---- contentHash --------------------------------------------------------------------------------

def test_content_hash_stable_and_charset(core):
    a = core("contentHash", "13 May, 6:27 pm Ingolf hi there")
    assert a == core("contentHash", "13 May, 6:27 pm Ingolf hi there")
    assert all(ch in "0123456789abcdefghijklmnopqrstuvwxyz" for ch in a)
    assert 8 <= len(a) <= 13


def test_content_hash_changes_on_each_field(core):
    base = ["13 May, 6:27 pm", "Ingolf", "hi", "text"]
    h0 = core("contentHash", " ".join(base))
    for i in range(len(base)):
        v = base[:]
        v[i] = v[i] + "X"
        assert core("contentHash", " ".join(v)) != h0


# ---- buildMarker / parseMarker ------------------------------------------------------------------

def test_marker_roundtrip(core):
    tid, mid, h = "6a01c932b2a02a63fe000001", "6a6229f9c507ff1d96174e80", "abc123xyz"
    marker = core("buildMarker", tid, mid, h)
    assert marker == f"%olcsync:{tid}:{mid}:{h}"
    assert core("parseMarker", marker) == {"threadId": tid, "messageId": mid, "hash": h}
    # trailing-whitespace tolerance
    assert core("parseMarker", marker + "   ") == {"threadId": tid, "messageId": mid, "hash": h}
    # full rendered line ends with a parseable marker
    line = "\\olc[d][a][h]{c} " + marker
    assert core("parseMarker", line) == {"threadId": tid, "messageId": mid, "hash": h}


def test_parse_marker_non_matching(core):
    assert core("parseMarker", "just some latex \\section{Intro}") is None
    assert core("parseMarker", "%olcsync:NOTHEX:xx:yy") is None


# ---- buildAnnotations ---------------------------------------------------------------------------

def _fixture():
    # Lifted from buildjob.md: a single-message thread, a multi-message thread, and a resolved one.
    threads = {
        "699ca5b92c9c4a2743000001": {"messages": [
            {"id": "699ca5b9e080d6e5b395024d", "content": "double check this numbers",
             "timestamp": 1771873721280,
             "user": {"first_name": "Carlos", "last_name": "Rombaldo Junior", "email": "c@ucl.ac.uk"}}]},
        "6a01c932b2a02a63fe000001": {"messages": [
            {"id": "6a01c932cb5c62b755df2326", "content": "I wouldn't say this here.",
             "timestamp": 1778501938277,
             "user": {"first_name": "shanejohnson7", "email": "s@x.com"}},
            {"id": "6a6229f9c507ff1d96174e80", "content": "A test reply.",
             "timestamp": 1784818169254,
             "user": {"first_name": "Ingolf", "last_name": "Becker", "email": "i@ucl.ac.uk"}}]},
        "6a01cb0b8cb67d21e0000001": {"resolved": True, "messages": [
            {"id": "6a01cb0b967758dbb0bd9f65", "content": "I would remove the figure",
             "timestamp": 1778502411972, "user": {"first_name": "shanejohnson7", "email": "s@x.com"}}]},
        "no_position_thread000000": {"messages": [
            {"id": "deadbeef", "content": "orphan", "timestamp": 1778502411972,
             "user": {"first_name": "Nobody"}}]},
    }
    comments = [
        {"id": "699ca5b92c9c4a2743000001",
         "op": {"c": "", "p": 100, "t": "699ca5b92c9c4a2743000001"}},
        {"id": "6a01c932b2a02a63fe000001",
         "op": {"c": "probability sampling", "p": 5000, "t": "6a01c932b2a02a63fe000001"}},
        {"id": "6a01cb0b8cb67d21e0000001",
         "op": {"c": "", "p": 8000, "t": "6a01cb0b8cb67d21e0000001"}},
    ]
    return threads, comments


def test_build_annotations_defaults(core):
    threads, comments = _fixture()
    res = core("buildAnnotations", threads, comments)
    anns = res["annotations"]
    # resolved thread skipped by default; orphan thread has no position -> unanchored
    assert res["unanchored"] == 1
    tids = {a["threadId"] for a in anns}
    assert "6a01cb0b8cb67d21e0000001" not in tids  # resolved, skipped
    assert "no_position_thread000000" not in tids

    # empty highlight -> offset == p ; non-empty -> offset == p + len(c)
    single = next(a for a in anns if a["threadId"] == "699ca5b92c9c4a2743000001")
    assert single["highlight"] == "" and single["offset"] == 100 and single["anchorOffset"] == 100

    multi = [a for a in anns if a["threadId"] == "6a01c932b2a02a63fe000001"]
    assert len(multi) == 2  # one \olc per message
    assert {a["messageId"] for a in multi} == {"6a01c932cb5c62b755df2326", "6a6229f9c507ff1d96174e80"}
    assert {a["author"] for a in multi} == {"shanejohnson7", "Ingolf Becker"}
    for a in multi:  # same anchor, offset advanced past the highlighted span
        assert a["anchorOffset"] == 5000
        assert a["offset"] == 5000 + len("probability sampling")
        assert a["highlight"] == "probability sampling"


def test_build_annotations_include_resolved(core):
    threads, comments = _fixture()
    res = core("buildAnnotations", threads, comments, {"includeResolved": True})
    tids = {a["threadId"] for a in res["annotations"]}
    assert "6a01cb0b8cb67d21e0000001" in tids
    assert res["unanchored"] == 1


def test_build_annotations_non_finite_timestamp_still_emitted(core):
    threads = {"6a623092a752c42b6a000001": {"messages": [
        {"id": "6a623092b4a994f4d270b29b", "content": "Test comment",
         "timestamp": {"__type": "Date"}, "user": {"first_name": "Ingolf", "last_name": "Becker"}}]}}
    comments = [{"id": "x", "op": {"c": "probability sampling", "p": 4976, "t": "6a623092a752c42b6a000001"}}]
    res = core("buildAnnotations", threads, comments)
    assert len(res["annotations"]) == 1
    assert res["annotations"][0]["date"] == ""


# ---- planEdits / applyEdits ---------------------------------------------------------------------

def _marker_re():
    # mirror MARKER_RE_SRC for python-side assertions
    return r"%olcsync:([0-9a-f]+):([0-9a-f]+):([0-9a-z]+)\s*$"


def test_plan_edits_fresh_insert_and_idempotency(core):
    threads, comments = _fixture()
    anns = core("buildAnnotations", threads, comments)["annotations"]
    doc = "A" * 100 + "B" * 5000 + "C" * 4000  # offsets 100 and 5020 fall inside

    edits = core("planEdits", doc, anns)
    # all inserts, sorted DESCENDING by `from`, non-overlapping
    froms = [e["from"] for e in edits]
    assert froms == sorted(froms, reverse=True)
    for e in edits:
        assert e["from"] == e["to"]  # zero-width inserts
    # non-overlapping (distinct offsets here)
    assert len(froms) == len(set(froms))

    out = core("applyEdits", doc, edits)
    assert "\\olc[" in out
    # every %olcsync marker is IMMEDIATELY followed by a newline (or end-of-string) — no source text
    # sits between the marker and the newline, so the leading `%` can never comment out real source.
    markers = list(re.finditer(r"%olcsync:[0-9a-f]+:[0-9a-f]+:[0-9a-z]+", out))
    assert len(markers) == len(anns)
    for m in markers:
        after = out[m.end():m.end() + 1]
        assert after in ("\n", ""), f"marker must be immediately followed by newline/EOS, got {after!r}"

    # idempotency: re-planning against the written doc yields no edits
    assert core("planEdits", out, anns) == []


def test_plan_edits_replace_on_changed_hash(core):
    threads, comments = _fixture()
    anns = core("buildAnnotations", threads, comments)["annotations"]
    doc = "A" * 100 + "B" * 5000 + "C" * 4000
    written = core("applyEdits", doc, core("planEdits", doc, anns))

    # change one message's text -> new hash -> exactly one REPLACE of that line
    threads2 = json.loads(json.dumps(threads))
    threads2["6a01c932b2a02a63fe000001"]["messages"][1]["content"] = "A CHANGED reply."
    anns2 = core("buildAnnotations", threads2, comments)["annotations"]

    edits = core("planEdits", written, anns2)
    assert len(edits) == 1
    e = edits[0]
    assert e["from"] < e["to"]  # a line replacement, not an insert
    out = core("applyEdits", written, edits)
    # exactly one \olc line carries the changed message id, i.e. no duplicate
    changed_id = "6a6229f9c507ff1d96174e80"
    assert out.count(f":{changed_id}:") == 1
    assert "A CHANGED reply." in out
    assert core("planEdits", out, anns2) == []  # stable again


def test_plan_edits_new_reply_single_insert(core):
    threads, comments = _fixture()
    anns = core("buildAnnotations", threads, comments)["annotations"]
    doc = "A" * 100 + "B" * 5000 + "C" * 4000
    written = core("applyEdits", doc, core("planEdits", doc, anns))

    # add a brand-new reply to the multi-message thread -> exactly one INSERT
    threads2 = json.loads(json.dumps(threads))
    threads2["6a01c932b2a02a63fe000001"]["messages"].append(
        {"id": "6affffffffffffffffffffff", "content": "third message",
         "timestamp": 1785000000000, "user": {"first_name": "New", "last_name": "Person"}})
    anns2 = core("buildAnnotations", threads2, comments)["annotations"]

    edits = core("planEdits", written, anns2)
    assert len(edits) == 1
    assert edits[0]["from"] == edits[0]["to"]  # insert
    out = core("applyEdits", written, edits)
    assert out.count(":6affffffffffffffffffffff:") == 1
    assert core("planEdits", out, anns2) == []


def test_apply_edits_order_independent(core):
    # applyEdits re-sorts DESCENDING internally, so any input order yields the same document.
    threads, comments = _fixture()
    anns = core("buildAnnotations", threads, comments)["annotations"]
    doc = "A" * 100 + "B" * 5000 + "C" * 4000
    edits = core("planEdits", doc, anns)  # planEdits returns edits sorted DESCENDING by `from`
    assert len(edits) >= 2  # need multiple edits for order to matter
    baseline = core("applyEdits", doc, edits)

    ascending = sorted(edits, key=lambda e: e["from"])
    assert core("applyEdits", doc, ascending) == baseline

    shuffled = edits[:]
    random.Random(1234).shuffle(shuffled)
    assert core("applyEdits", doc, shuffled) == baseline


def test_multiline_highlight_no_orphan_on_resync(core):
    # A highlight that wraps across two source lines carries a real `\n`. It must be collapsed BEFORE
    # rendering so the \olc line stays on ONE physical line; otherwise a re-sync (changed hash) would
    # replace only the marker's physical line and orphan the macro fragment above it.
    threads = {"6a01c932b2a02a63fe000001": {"messages": [
        {"id": "6a6229f9c507ff1d96174e80", "content": "please rephrase",
         "timestamp": 1784818169254, "user": {"first_name": "Ingolf", "last_name": "Becker"}}]}}
    comments = [{"id": "6a01c932b2a02a63fe000001",
                 "op": {"c": "first line\nsecond line", "p": 500, "t": "6a01c932b2a02a63fe000001"}}]
    doc = "A" * 500 + "B" * 500

    anns = core("buildAnnotations", threads, comments)["annotations"]
    assert "\n" not in anns[0]["highlight"]  # collapsed: no literal newline in the stored highlight
    written = core("applyEdits", doc, core("planEdits", doc, anns))

    # edit the comment -> new content -> new hash -> re-sync
    threads2 = json.loads(json.dumps(threads))
    threads2["6a01c932b2a02a63fe000001"]["messages"][0]["content"] = "please REPHRASE this"
    anns2 = core("buildAnnotations", threads2, comments)["annotations"]
    edits = core("planEdits", written, anns2)
    assert len(edits) == 1  # a single in-place REPLACE, not an insert leaving an orphan
    final = core("applyEdits", written, edits)

    assert final.count("\\olc[") == 1  # exactly one \olc token, no orphaned fragment / duplicate
    olc_lines = [ln for ln in final.split("\n") if "\\olc[" in ln]
    assert len(olc_lines) == 1  # it sits on its own single physical line...
    assert "\\n" not in olc_lines[0]  # ...and contains no literal newline mid-macro
    m = re.search(r"%olcsync:[0-9a-f]+:[0-9a-f]+:[0-9a-z]+", olc_lines[0])
    assert m and m.end() == len(olc_lines[0])  # marker sits at end of its line (immediately before \n)
    assert core("planEdits", final, anns2) == []  # stable after the re-sync


def test_plan_edits_overlap_guard(core):
    # Pathological: a brand-new comment anchored INSIDE a previously-injected \olc marker line. The
    # insert offset falls within an existing REPLACE range; planEdits must NOT emit overlapping edits,
    # and must divert the offending insert into the `conflicts` list instead.
    marker_line = "\\olc[d][a][h]{old} %olcsync:aa:bb:oldhash"
    doc = "X" * 20 + "\n" + marker_line + "\n" + "Y" * 20
    inside = 21 + 5  # an offset strictly within the marker line (which starts at 21)
    ann_replace = {"threadId": "aa", "messageId": "bb", "hash": "newhash", "offset": 0,
                   "line": "\\olc[d][a][h]{new} %olcsync:aa:bb:newhash"}
    ann_insert = {"threadId": "cc", "messageId": "dd", "hash": "zz", "offset": inside,
                  "line": "\\olc[d2][a2][h2]{c2} %olcsync:cc:dd:zz"}
    args = [doc, [ann_replace, ann_insert]]

    edits = core("planEdits", *args)
    ranges = sorted((e["from"], e["to"]) for e in edits)
    for (f1, t1), (f2, t2) in zip(ranges, ranges[1:]):
        assert t1 <= f2, "planEdits emitted overlapping ranges"
    # the conflicting insert is diverted to `conflicts` (dropped by JSON array serialization, so read
    # it back directly from the returned array object)
    n_conflicts = core.ctx.eval(
        "module.exports.planEdits(%s, %s).conflicts.length" % (json.dumps(args[0]), json.dumps(args[1])))
    assert n_conflicts == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
