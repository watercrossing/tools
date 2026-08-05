# config-password-sanitisation

Swap the real secrets in a config file for stable placeholders so it can be pasted into a ticket, a chat, a mailing list or an LLM prompt — and swap them back afterwards, so the answer you get returned is a file you can actually deploy.

```bash
sanitise.sh --init                       # write ~/.config/sanitise/map, then edit it
sanitise.sh config.yaml                  # -> config-sanitised.yaml       (safe to hand over)
sanitise.sh -r config-sanitised.yaml     # -> config-sanitised-unsanitised.yaml
```

The point of *stable* placeholders is the round trip.
Blanking secrets out with `sed -i 's/password: .*/password: REDACTED/'` is a one-way door: whoever helps you edits the redacted file, hands it back, and now you have to re-apply their changes to the real one by hand — a merge, done by eye, in the one file where a mistake locks you out of your own database.
With a map, the same secret always becomes the same placeholder, so `-r` reconstructs the deployable file byte for byte.

## The map file

The secrets live in the map, never in the script — which is why the script can be in this repository and the map cannot.

```
s3cr3t-db-pass!	password1
hunter2	password2
p@ss*w[ord?	password3
```

One replacement per line: the real secret, **a single TAB**, then its placeholder.
A tab because it is the one character a password essentially never contains, unlike a space, `=`, `:`, `#` or a quote — no quoting rules to learn and no secret you cannot express.
A line starting with `#` that contains no tab is a comment, so a secret may itself start with `#`.
See [sanitise.map.example](sanitise.map.example).

`--init` writes a template with mode `600` and creates the directory if needed.
The map is looked for at `--map`, else `$SANITISE_MAP`, else `${XDG_CONFIG_HOME:-~/.config}/sanitise/map`; the current directory is deliberately not searched, so a map cannot be picked up by accident from a repository you happen to be standing in.
If the map is readable by anyone but you, the tool says so on every run.

## Substitution that survives its own output

Replacements are applied in **one left-to-right pass**, taking the leftmost match and, where several start at the same position, the longest of them.
Text that has been replaced is never looked at again.

That is not the obvious implementation, and the obvious one is wrong in two ways that a config file will find:

- **A short secret matching inside a placeholder that was just written.**
  Apply the pairs one after another — `sed -e` … `-e`, or a loop over the map — and a secret like `word` will happily match the middle of the `password2` a previous pair produced.
  Sorting the pairs by length does not help; nothing helps except not rescanning.
- **A short secret biting off the front of a longer one.**
  With `top` and `topsecret` both in the map, whichever is tried first wins, and if that is `top` then `topsecret` never matches again.
  Leftmost-longest picks `topsecret`, and the same rule makes `password10` survive a map that also contains `password1` on the way back.

Secrets are matched literally, so `p@ss*w[ord?` is a password and not a glob.

## Three checks, because a silent failure here is a leaked credential

After sanitising, before you upload anything:

- **Did anything survive?**
  Every secret is grepped for in the output.
  If one is still there the tool says `do NOT upload this file` and exits 2.
- **Was a placeholder already in the input?**
  If `password1` appears in the config for its own reasons, reversing turns it into a real secret on a line that never had one — a credential invented out of nothing, in a file you are about to deploy.
  Reported, exit 2.
- **Is the map self-defeating?**
  Duplicate placeholders (whose reverse would be ambiguous), duplicate secrets, and placeholders containing one of your secrets are all refused or flagged when the map loads, not after the file is written.

A per-secret count goes to stderr — `password1: 3` — because the count you did not expect is how you find out that the map is stale, or that a secret is in the file twice as often as you thought.

```
$ sanitise.sh config.yaml
password1: 1
password2: 1
config-sanitised.yaml: 2 replacement(s)
```

Output files are never overwritten.
If `config-sanitised.yaml` exists you get `config-sanitised-2.yaml`, then `-3`, and the file is created with `noclobber` rather than tested-then-written, so two runs at once cannot land on the same name.
`-o FILE` overrides the name (`--force` to overwrite, `-o -` for stdout).

The reversed file is created mode `600` whatever your umask is: it holds the real secrets.
The sanitised file is not, because handing it to someone is the entire point.

## As a pre-commit hook

`--check` reports which secrets appear in the files you give it and exits 2 if any do, writing nothing:

```bash
$ sanitise.sh --check config.yaml
config.yaml:1,7: secret for placeholder password1
config.yaml:2: secret for placeholder password2
```

It prints the file, the lines and the *placeholder* — never the matched line.
A secret scanner that echoes the secret into a terminal, a CI log or a hook's output has just leaked it somewhere new.

```bash
#!/bin/sh
# .git/hooks/pre-commit
files=$(git diff --cached --name-only --diff-filter=ACM)
[ -n "$files" ] && exec sanitise.sh --check $files
```

## Tests

```bash
uv run tests/test_sanitise.py   # self-contained
pytest tests/                   # if pytest is already available
```

The tests drive the real CLI end-to-end in a temporary directory, including both overlap hazards, the glob-metacharacter secret, the file modes and every map-file error.

## Limits

- **Line-based**, so a secret cannot contain a newline, and the input must be text — no NUL bytes.
- **The input must be a regular file**, not a pipe: it is read twice, once to transform and once to check what came out.
- **It is not a secret *detector*.** It replaces the strings you list and nothing else; a password you forgot to put in the map goes through untouched. `--check` has the same blind spot. Something like [gitleaks](https://github.com/gitleaks/gitleaks) or [detect-secrets](https://github.com/Yelp/detect-secrets) finds unknown secrets by pattern; this tool round-trips known ones.
- **Structure is not understood.** It is a string substitution, not a YAML/JSON/INI parser, which is exactly why it works on all of them — and why a secret that appears base64-encoded, URL-escaped or split across lines is not found.
- **Pure bash 4.3+**, so it is slow on large files (a few thousand lines a second). Configs are small; do not point it at a log.
