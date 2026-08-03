#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest"]
# ///
"""
Tests for markdown-docs-lint.py.

Each test drives the real CLI end-to-end via `uv run markdown-docs-lint.py`, so exit codes and output format are exercised as a user sees them.

    uv run tests/test_markdown_docs_lint.py   # self-contained (installs pytest via uv)
    pytest tests/                             # if pytest is already available
"""
import subprocess, sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "markdown-docs-lint.py"


def lint(root, *extra):
    return subprocess.run(["uv", "run", str(SCRIPT), str(root), *extra], capture_output=True, text=True)


def tree(tmp_path, **files):
    for name, body in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return tmp_path


def test_clean_tree_exits_zero(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\nSee [a](a.md).\n", "a.md": "# A\n\nBack to [R](README.md).\n"})
    r = lint(root)
    assert r.returncode == 0 and "error" not in r.stdout


def test_missing_file_is_an_error(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\nSee [gone](nope.md).\n"})
    r = lint(root)
    assert r.returncode == 1
    assert "README.md:3: error: [link] missing file: nope.md" in r.stdout


def test_missing_anchor_is_an_error(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](a.md#no-such)\n", "a.md": "# A\n\n## Real Heading\n"})
    r = lint(root)
    assert r.returncode == 1 and "[anchor] no such heading: a.md#no-such" in r.stdout


def test_existing_anchor_passes(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](a.md#real-heading)\n", "a.md": "# A\n\n## Real Heading\n"})
    assert lint(root).returncode == 0


def test_em_dash_leaves_two_hyphens(tmp_path):
    """The regression that motivated the tool: whitespace must not be collapsed, so an em dash yields a double hyphen."""
    root = tree(tmp_path, **{"README.md": "# R\n\n[ok](a.md#m--minden-kielmibeckereu)\n[bad](a.md#m-minden-kielmibeckereu)\n",
                             "a.md": "# A\n\n## `m` — Minden (kiel.m.ibecker.eu)\n"})
    r = lint(root)
    assert r.returncode == 1
    assert "README.md:4: error" in r.stdout          # the single-hyphen form is genuinely wrong
    assert "README.md:3: error" not in r.stdout      # the double-hyphen form must not be a false positive


def test_inline_code_in_heading_contributes_its_text(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](a.md#the-l-names)\n", "a.md": "# A\n\n## The `.l.` names\n"})
    assert lint(root).returncode == 0


def test_links_inside_fenced_code_are_ignored(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n```sh\nsee [nope](does-not-exist.md)\n```\n"})
    assert lint(root).returncode == 0


def test_hash_inside_fence_is_not_a_heading(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](a.md#comment)\n",
                             "a.md": "# A\n\n```\n# comment\n```\n"})
    assert lint(root).returncode == 1, "a '#' in a code fence must not define an anchor"


def test_tilde_fence_and_inline_span_are_handled(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n~~~\n[nope](gone.md)\n~~~\n\nAlso `[nope](gone.md)` inline.\n"})
    assert lint(root).returncode == 0


def test_duplicate_headings_get_numeric_suffixes(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[a](a.md#dup) [b](a.md#dup-1)\n", "a.md": "# A\n\n## Dup\n\n## Dup\n"})
    assert lint(root).returncode == 0


def test_external_and_absolute_targets_are_skipped(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[a](https://example.invalid/x) [b](mailto:x@y.z) [c](/etc/hosts)\n"})
    assert lint(root).returncode == 0


def test_image_targets_are_not_treated_as_links(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n![alt](missing.png)\n"})
    assert lint(root).returncode == 0


def test_orphan_is_a_warning_not_an_error(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n", "lonely.md": "# L\n"})
    r = lint(root)
    assert r.returncode == 0
    assert "lonely.md:1: warning: [orphan]" in r.stdout
    assert lint(root, "--strict").returncode == 1
    assert "[orphan]" not in lint(root, "--no-orphans").stdout


def test_reachability_is_transitive(tmp_path):
    """A page linked only from an orphan is itself unreachable."""
    root = tree(tmp_path, **{"README.md": "# R\n", "a.md": "# A\n\n[b](b.md)\n", "b.md": "# B\n"})
    out = lint(root).stdout
    assert "a.md:1: warning: [orphan]" in out and "b.md:1: warning: [orphan]" in out


def test_size_caps_and_exemption(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[big](big.md) [mid](mid.md)\n",
                             "big.md": "# Big\n" + "line\n" * 200, "mid.md": "# Mid\n" + "line\n" * 120})
    r = lint(root)
    assert r.returncode == 1
    assert "big.md:201: error: [size]" in r.stdout and "mid.md:121: warning: [size]" in r.stdout
    assert lint(root, "--exempt", "big.md").returncode == 0
    assert lint(root, "--max-lines", "0", "--warn-lines", "0").returncode == 0


def test_custom_entry_point(tmp_path):
    root = tree(tmp_path, **{"index.md": "# I\n\n[a](a.md)\n", "a.md": "# A\n"})
    assert lint(root, "--entry", "index.md", "--no-orphans").returncode == 0
    assert "[orphan]" not in lint(root, "--entry", "index.md").stdout
    assert "entry point not found" in lint(root).stderr


def test_anchor_only_link_checks_own_file(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[here](#section) [gone](#nope)\n\n## Section\n"})
    r = lint(root)
    assert r.returncode == 1 and r.stdout.count("error") == 1 and "#nope" in r.stdout


def test_url_encoded_fragment_resolves(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](a.md#caf%C3%A9)\n", "a.md": "# A\n\n## Café\n"})
    assert lint(root).returncode == 0


def test_quiet_suppresses_summary_only(tmp_path):
    root = tree(tmp_path, **{"README.md": "# R\n\n[x](nope.md)\n"})
    r = lint(root, "-q")
    assert "missing file" in r.stdout and "files," not in r.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
