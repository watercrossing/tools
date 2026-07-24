# claude-render-transcripts

Renders a Claude Code session `.jsonl` transcript into readable plain text.

Claude Code stores every session as newline-delimited JSON.
Headless runs (`claude -p ...`, `entrypoint: sdk-cli`) are written to disk but filtered out of the VS Code `/resume` picker, so this tool is how you read them back.

## Usage

```sh
uv run claude-render-transcripts.py <transcript.jsonl> [more.jsonl ...] > out.txt
```

Takes one or more `.jsonl` paths as arguments and writes to stdout.
Pure standard library — `uv` just handles the Python version; there are no third-party dependencies.

In practice you rarely invoke it by hand: start a fresh Claude Code session in the repo and ask it to, for example, "use claude-render-transcripts to render the transcripts from the job that ran overnight", and it finds the session files and runs the tool for you.

## Where the transcripts live

Under `~/.claude/projects/<slugified-cwd>/`, where the slug is the working directory with `/` turned into `-` (so a cwd of `/home/you/dev/tools` becomes `-home-you-dev-tools`):

```
<session-id>.jsonl                       # the main (orchestrator) session
<session-id>/subagents/agent-*.jsonl     # one file per spawned subagent
<session-id>/subagents/agent-*.meta.json # {agentType, description, model, ...}
```

Each subagent's `.meta.json` gives its `description` and `model` — handy for naming the rendered output.

## Output

For every `user` / `assistant` message it prints a header (`ROLE  HH:MM:SS`) followed by the body.
Block types are flattened as:

- `text` — printed verbatim
- `thinking` — prefixed with `[thinking]`
- `tool_use` — `[tool_use: <name>]` plus the input as pretty JSON
- `tool_result` — `[tool_result]` plus the result text

`tool_use` inputs and `tool_result` bodies are truncated at 2000 chars (`... [truncated]`).
Queue operations, attachments, `ai-title`, and API-error records are skipped.

## Convention in this folder

Rendered transcripts are grouped one directory per night (`transcripts/YYYY-MM-DD/`), with the orchestrator as `00-main.txt` and each subagent named `<description> [<model>].txt`.
The whole `transcripts/` folder is gitignored.
