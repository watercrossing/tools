# tools

A collection of small, self-contained tools, inspired by [simonw/tools](https://github.com/simonw/tools/).
Each tool lives in its own folder and stands alone — no shared build step, no monorepo tooling.

## Formatting

- **Code**: keep lines to a maximum length of 153 characters.
- **Markdown and other text files**: no line-length limit. Write one sentence per line (a semantic line break after each sentence) and let the editor soft-wrap long lines, rather than hard-wrapping mid-sentence. Same applies to commit message. 

## Commits

Do not add a `Co-Authored-By: Claude …` trailer (or any AI co-author line) to commit messages.

## Repository structure

```
tools/
├── tool-name/              # One folder per tool
│   ├── tool-name.*         #   the tool itself (.py, .html, .js, …)
│   ├── README.md           #   what it does and how to run it
│   └── tests/              #   tests and fixtures (optional)
├── .github/workflows/      # CI (planned)
│   ├── test.yml            #   runs pytest / Playwright
│   └── claude.yml
├── README.md               # Master list of all tools
└── .gitignore
```

## Python tools

Write Python tools as a single file that runs under [uv](https://docs.astral.sh/uv/).
Every Python tool starts with this shebang and inline script-metadata block:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
```

Declare any third-party dependencies (Click, sqlite-utils, …) in the same block:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click",
#     "sqlite-utils",
# ]
# ///
```

This lets anyone run the tool with `uv run tool.py` — or `./tool.py` after `chmod +x` — with uv fetching the right Python and dependencies automatically and no virtualenv to manage.

## Web tools (HTML + JS)

Web tools follow a lightweight, stateless HTML5 pattern:

1. Each tool is a **single, self-contained HTML file**.
2. **Mobile-responsive**, with minimal CSS and no frameworks.
3. **Real-time processing** via JavaScript event listeners.
4. **External libraries** loaded from a CDN (jsdelivr, cdnjs, …) only when needed.
5. **No build step** for individual tools.
6. **Tests** use Playwright + pytest.

Never use React — always plain HTML, vanilla JavaScript, and CSS with minimal dependencies.

CSS uses two-space indentation and starts like this:

```html
<style>
* {
  box-sizing: border-box;
}
```

Inputs and textareas should be `font-size: 16px`.
Prefer Helvetica for the font.

JavaScript uses two-space indentation and starts like this:

```html
<script type="module">
// code in here should not be indented at the first level
```
