# markdown-docs-lint

Lints a tree of Markdown documentation for the four failures that break **silently**: dead relative links, dead `#anchors`, pages nothing links to, and files that have grown too big to read.

No dependencies, no config file, no network.
One file, run with [uv](https://docs.astral.sh/uv/).

```sh
./markdown-docs-lint.py ~/docs
uv run markdown-docs-lint.py ~/docs --exempt README.md --exempt CONVENTIONS.md
```

Output is the usual `path:line: level: [check] message`, so it is greppable and clickable in an editor:

```
hosts/bob/OVERVIEW.md:64: error: [anchor] no such heading: ../../migration.md#1-two-keycloaks-share-one-name
todo/orphan.md:1: warning: [orphan] not reachable by links from README.md
network.md:125: warning: [size] 125 lines, over the 100-line target
```

Exit status is 1 if there were errors, 0 otherwise, so it drops straight into a pre-commit hook or CI.
`--strict` makes warnings count too.

## Why

Broken *anchors* are the interesting case.
A dead link to a missing file is at least discoverable — something eventually 404s.
But rename a heading and every `#anchor` pointing at it dies instantly, with **no signal at all**: nothing errors, nothing renders differently, and `git diff` shows a tidy one-line heading change.
The inbound links are in other files and are not part of the diff.

Measured on a real docs tree: renaming two headings broke **five inbound links across five files**.

That matters more when the docs are read by coding agents than when they are read by people.
A human who lands on the wrong section scrolls.
An agent that follows a link into nothing has no fallback and no one to ask, and the failure is invisible in whatever it produces next.

### Why this isn't ten lines of shell

Because the heading → anchor slug has a trap in it, and the naive version fails in the direction that makes you stop trusting the tool.

GitHub builds an anchor by lowercasing the heading, dropping inline markup, deleting every character that is not alphanumeric / space / hyphen / underscore, then replacing spaces with hyphens.
Nothing collapses runs of whitespace.
So a character that gets deleted from between two spaces leaves **two** hyphens behind:

| Heading | Anchor |
|---|---|
| `` ## `m` — Minden (kiel.m.ibecker.eu) `` | `#m--minden-kielmibeckereu` |

The em dash vanishes; its two neighbouring spaces each become a hyphen.
A hand-rolled checker that reaches for `re.sub(r'\s+', '-', ...)` reports every such heading as broken.
On the tree this was written for that was four false positives out of four findings — and a checker that cries wolf is worse than no checker, because you learn to skip its output.

The other traps it handles, all of which produced wrong answers in the throwaway version that preceded it:

- **Fenced code blocks** — a `[x](y)` in a shell example is not a link, and a `# comment` inside a fence is not a heading.
  Both `` ``` `` and `~~~` fences, with closing fences at least as long as the opener.
  Blanking preserves line numbers, so diagnostics still point at the right line.
- **Inline code spans** — `` `[x](y)` `` is not a link either.
  But inside a *heading*, backticked text still contributes to the slug, so spans are stripped for link extraction and kept for anchors.
- **Duplicate headings** — the second `## Notes` is `#notes-1`, the third `#notes-2`.
- **Images** — `![alt](missing.png)` is not a link to check.
- **Unicode** — `## Café` is `#café`, and a `%C3%A9`-encoded fragment matches it.

## Checks

| Check | Level | What it means |
|---|---|---|
| `link` | error | A relative link points at a file that does not exist |
| `anchor` | error | A `#fragment` names no heading in the target file |
| `size` | error over `--max-lines`, warning over `--warn-lines` | The file is too long to be read whole cheaply |
| `orphan` | warning | Nothing links to this page, directly or transitively, from the entry point |

`orphan` is a warning because a page reached only by grep can be legitimate.
Reachability is transitive: a page linked only from an orphan is itself reported.

External URLs (`https:`, `mailto:`, protocol-relative `//`) are skipped — checking those needs the network and wants a different cadence.
Site-root-relative links (`/foo`) are skipped too, since their meaning depends on a publish root the tool cannot know.

## Options

```
root                  directory to scan (default: current)
--entry PATH          reachability entry point, repeatable (default: README.md)
--max-lines N         hard cap; over this is an error (default: 150, 0 disables)
--warn-lines N        soft cap; over this is a warning (default: 100, 0 disables)
--exempt GLOB         glob, relative to root, exempt from the size caps; repeatable
--no-orphans          skip the unreachable-page check
--strict              exit non-zero on warnings too
-q, --quiet           print problems only, no summary
```

Size caps suit a docs tree meant to be read whole by an agent, where a long file is a real cost.
Set `--max-lines 0 --warn-lines 0` to turn them off and use this purely as a link checker.

## As a git hook

```sh
cat > .git/hooks/pre-commit <<'EOF'
#!/bin/sh
exec /path/to/markdown-docs-lint.py "$(git rev-parse --show-toplevel)" --exempt README.md
EOF
chmod +x .git/hooks/pre-commit
```

Catching this at commit time is most of the value: heading renames and file moves are exactly the edits that break links, and they are the edits you are making when the hook fires.

## Limitations

- **ATX headings only** (`## Foo`), not Setext (`Foo` over `---`).
- **Inline links only** — reference-style `[text][ref]` and its `[ref]: url` definitions are not resolved.
- The slug follows GitHub's rules, which are the de-facto standard but not universal; other renderers differ, notably in how they treat `_` and non-Latin scripts.

## Tests

```sh
uv run tests/test_markdown_docs_lint.py     # self-contained, installs pytest via uv
pytest tests/
```

19 tests, driving the real CLI end to end.
The em-dash case is a named regression test.
