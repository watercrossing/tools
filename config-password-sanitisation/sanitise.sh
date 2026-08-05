#!/usr/bin/env bash
#
# sanitise.sh — swap the real secrets in a config file for stable placeholders, and put them back again.
#
#   sanitise.sh config.yaml                 ->  config-sanitised.yaml    (safe to paste into a ticket)
#   sanitise.sh -r config-sanitised.yaml    ->  config-sanitised-unsanitised.yaml
#   sanitise.sh --check config.yaml         ->  exit 2 if a secret is in there (pre-commit hook)
#
# The secrets live in a private map file, never in this script. See --init.
# Requires bash 4.3+ (namerefs); no other dependencies.

set -euo pipefail

PROG="${0##*/}"
TAB=$'\t'
DEFAULT_MAP="${SANITISE_MAP:-${XDG_CONFIG_HOME:-$HOME/.config}/sanitise/map}"

usage() {
  cat <<EOF
Usage:
  $PROG [-m MAP] [-o OUT] [--force] FILE       real secrets -> placeholders   (FILE-sanitised.EXT)
  $PROG -r [-m MAP] [-o OUT] [--force] FILE    placeholders -> real secrets   (FILE-unsanitised.EXT)
  $PROG -c [-m MAP] FILE...                    report secrets, write nothing
  $PROG --init [-m MAP]                        create a template map file

Options:
  -r, --reverse    put the real secrets back
  -c, --check      report which secrets appear in each FILE, exit 2 if any do
  -m, --map FILE   map file to use  (default: \$SANITISE_MAP, else $DEFAULT_MAP)
  -o, --output F   write to F instead of a derived name; "-" is stdout
      --force      allow -o to overwrite an existing file
  -q, --quiet      no per-secret summary on stderr
  -h, --help       show this help

Derived output names are never overwritten: if the name is taken a counter is appended, e.g.
config-sanitised.yaml, config-sanitised-2.yaml, config-sanitised-3.yaml, ...

The map is one replacement per line, a single TAB between the real secret and its placeholder:

    s3cr3t-db-pass!${TAB}password1

Exit: 0 clean, 1 usage or map error, 2 a secret is still in the output (or --check found one).
EOF
}

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }

# ---- map file -------------------------------------------------------------
# The map, as two parallel arrays: SECRET[i] is replaced by PLACEHOLDER[i], and the reverse. COUNT[i] is how often
# the last transform did it.
declare -a SECRET=() PLACEHOLDER=() COUNT=()

load_map() {
  local file="$1" lineno=0 raw secret placeholder perms i
  [[ -f "$file" && -r "$file" ]] || die "cannot read map file '$file' (run '$PROG --init' to create one)"

  perms="$(stat -c '%a' "$file" 2>/dev/null || stat -f '%Lp' "$file" 2>/dev/null || true)"
  [[ -n "$perms" && "${perms: -2}" != "00" ]] && warn "warning: map file $file is readable by others (mode $perms); chmod 600 it"

  while IFS= read -r raw || [[ -n "$raw" ]]; do
    lineno=$((lineno + 1))
    raw="${raw%$'\r'}"                                     # tolerate CRLF
    [[ -z "${raw//[[:space:]]/}" ]] && continue            # blank line
    [[ "$raw" == '#'* && "$raw" != *"$TAB"* ]] && continue  # comment: a '#' line with no tab, so a secret may start with '#'

    [[ "$raw" == *"$TAB"* ]] || die "$file:$lineno: no tab — a map line is 'secret<TAB>placeholder'"
    secret="${raw%%"$TAB"*}"
    placeholder="${raw#*"$TAB"}"
    [[ "$placeholder" == *"$TAB"* ]] && die "$file:$lineno: more than one tab"
    [[ -n "$secret" ]] || die "$file:$lineno: empty secret"
    [[ -n "$placeholder" ]] || die "$file:$lineno: empty placeholder"
    [[ "$secret" != "$placeholder" ]] || die "$file:$lineno: secret and placeholder are the same string"

    for i in "${!SECRET[@]}"; do
      [[ "${SECRET[$i]}" == "$secret" ]] && die "$file:$lineno: this secret is already mapped to '${PLACEHOLDER[$i]}'"
      [[ "${PLACEHOLDER[$i]}" == "$placeholder" ]] && die "$file:$lineno: placeholder '$placeholder' is used twice, reversing would be ambiguous"
    done
    SECRET+=("$secret"); PLACEHOLDER+=("$placeholder")
  done < "$file"

  (( ${#SECRET[@]} )) || die "$file: no replacements defined"

  # A secret that occurs inside somebody's placeholder is a bad pairing: the substitution itself copes, but the
  # leak check below cannot tell that "password1" contains the secret "a" innocently, and will cry wolf every run.
  local j
  for i in "${!SECRET[@]}"; do
    for j in "${!PLACEHOLDER[@]}"; do
      [[ "${PLACEHOLDER[$j]}" == *"${SECRET[$i]}"* ]] &&
        warn "warning: placeholder '${PLACEHOLDER[$j]}' contains the secret mapped to '${PLACEHOLDER[$i]}'; expect false leak warnings"
    done
  done
  return 0
}

write_template() {
  local file="$1" dir
  [[ -e "$file" ]] && die "$file already exists — not overwriting it"
  dir="$(dirname -- "$file")"
  mkdir -p -- "$dir" || die "cannot create $dir"
  (umask 077; cat > "$file" <<EOF
# Map file for $PROG — one replacement per line, real secret and placeholder separated by a single TAB.
#
# Keep this file private: it is the only place the real secrets live.
# A line starting with '#' and containing no tab is a comment, so a secret may itself start with '#'.
# Placeholders must be unique, and should be strings that do not otherwise occur in your configs.
#
#real-secret${TAB}placeholder
s3cr3t-db-pass!${TAB}password1
hunter2${TAB}password2
EOF
  ) || die "cannot write $file"
  chmod 600 -- "$file" 2>/dev/null || true
  warn "wrote $file (mode 600) — edit it, it currently holds example secrets"
}

# ---- literal replacement --------------------------------------------------
# One left-to-right pass per line, taking the leftmost match and, where several start at the same place, the longest
# of them. Text that has already been replaced is never looked at again, which is the only way to be sure a
# replacement cannot be eaten by a later one: applying the pairs in sequence instead — even longest secret first —
# lets a short secret match inside a placeholder that a previous pair just wrote, and quietly corrupts both.
#
# Quoting the needle inside ${s%%"$n"*} makes bash match it literally, so a secret full of glob metacharacters
# ( * ? [ \ ) needs no escaping and can never be misread as a pattern.
_out=""
replace_line() {                                           # from/to are the caller's namerefs (bash locals are dynamically scoped)
  local s="$1" out="" i n pre pos len best_pos best_len best_i
  while [[ -n "$s" ]]; do
    best_pos=-1; best_len=-1; best_i=-1
    for i in "${!from[@]}"; do
      n="${from[$i]}"
      [[ "$s" == *"$n"* ]] || continue
      pre="${s%%"$n"*}"; pos=${#pre}; len=${#n}
      if (( best_i < 0 || pos < best_pos || (pos == best_pos && len > best_len) )); then
        best_pos=$pos; best_len=$len; best_i=$i
      fi
    done
    (( best_i < 0 )) && break
    out+="${s:0:best_pos}${to[$best_i]}"
    s="${s:best_pos+best_len}"
    COUNT[best_i]=$(( COUNT[best_i] + 1 ))
  done
  _out="$out$s"
}

# Rewrite stdin to stdout, counting hits per replacement. The input's trailing newline is preserved — a file that
# ended without one still does, which matters when the config is checksummed or diffed against the original.
transform() {
  local -n from="$1" to="$2"
  local line i first=1 trailing_nl=1
  for i in "${!SECRET[@]}"; do COUNT[$i]=0; done

  while IFS= read -r line || { [[ -n "$line" ]] && trailing_nl=0; }; do
    replace_line "$line"
    if (( first )); then printf '%s' "$_out"; first=0; else printf '\n%s' "$_out"; fi
  done
  (( first || ! trailing_nl )) || printf '\n'
}

# ---- output naming --------------------------------------------------------
# Insert "-SUFFIX" before the extension and create the file atomically (noclobber), appending -2, -3, ... until one
# sticks. Atomic because the alternative — test, then write — happily overwrites a file created in between.
create_output() {
  local input="$1" suffix="$2" mask="$3"
  local dir name stem ext rest candidate path n=1

  dir="$(dirname -- "$input")"; name="$(basename -- "$input")"
  if [[ "$name" == *.* ]]; then
    rest="${name#.}"
    if [[ "$name" == .* && "$rest" != *.* ]]; then
      stem="$name"; ext=""                                 # pure dotfile, e.g. .env
    else
      stem="${name%.*}"; ext=".${name##*.}"
    fi
  else
    stem="$name"; ext=""
  fi

  candidate="${stem}-${suffix}${ext}"
  while :; do
    path="${dir}/${candidate}"
    [[ "$dir" == "." ]] && path="$candidate"
    if (umask "$mask"; set -o noclobber; : > "$path") 2>/dev/null; then
      printf '%s' "$path"; return 0
    fi
    [[ -e "$path" ]] || die "cannot create '$path'"
    n=$((n + 1)); candidate="${stem}-${suffix}-${n}${ext}"
  done
}

# ---- check mode -----------------------------------------------------------
# Reports file:lines and the *placeholder*, never the matched line: a secret scanner that echoes the secret into a
# terminal, a CI log or a pre-commit hook's output has just leaked it somewhere new.
check_files() {
  local found=0 file i lines hits
  for file in "$@"; do
    [[ -f "$file" && -r "$file" ]] || die "cannot read file: '$file'"
    hits=()
    for i in "${!SECRET[@]}"; do
      lines="$(grep -nF -e "${SECRET[$i]}" -- "$file" | cut -d: -f1 | paste -sd, - || true)"
      [[ -n "$lines" ]] || continue
      hits+=("${lines%%,*}$TAB$file:$lines: secret for placeholder ${PLACEHOLDER[$i]}")   # sort key: first line it appears on
      found=1
    done
    (( ${#hits[@]} )) && printf '%s\n' "${hits[@]}" | sort -n | cut -f2-
  done
  (( found )) && return 2
  return 0
}

# ---- arguments ------------------------------------------------------------
reverse=false; check=false; init=false; force=false; quiet=false
map=""; output=""; declare -a POSITIONAL=()

while (( $# )); do                                         # options may follow the filename: `sanitise.sh f.yaml -r` works
  case "$1" in
    -h|--help)              usage; exit 0 ;;
    -r|--reverse|--recover) reverse=true; shift ;;
    -c|--check)             check=true; shift ;;
    --init)                 init=true; shift ;;
    --force)                force=true; shift ;;
    -q|--quiet)             quiet=true; shift ;;
    -m|--map)               [[ $# -ge 2 ]] || die "--map needs a file"; map="$2"; shift 2 ;;
    -o|--output)            [[ $# -ge 2 ]] || die "--output needs a file"; output="$2"; shift 2 ;;
    --)                     shift; POSITIONAL+=("$@"); break ;;
    -*)                     usage >&2; die "unknown option: $1" ;;
    *)                      POSITIONAL+=("$1"); shift ;;
  esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

$reverse && $check && die "--reverse and --check do not go together"
map="${map:-$DEFAULT_MAP}"
$init && { write_template "$map"; exit 0; }

load_map "$map"

if $check; then
  (( $# )) || { usage >&2; exit 1; }
  check_files "$@" || exit $?
  exit 0
fi

[[ $# -eq 1 ]] || { usage >&2; exit 1; }
input="$1"
[[ -f "$input" ]] || die "not a regular file: '$input' (the input is read twice, so a pipe will not do)"
[[ -r "$input" ]] || die "cannot read file: '$input'"

# ---- transform ------------------------------------------------------------
# Reversing writes real secrets to disk, so that file is created 600 whatever the umask says. A sanitised file is
# meant to be handed to someone else, and inherits the normal umask.
if $reverse; then
  suffix="unsanitised"; mask=077; from=PLACEHOLDER; to=SECRET
else
  suffix="sanitised"; mask="$(umask)"; from=SECRET; to=PLACEHOLDER
fi

if [[ "$output" == "-" ]]; then
  dest=""; label="(stdout)"
  transform "$from" "$to" < "$input"
else
  if [[ -n "$output" ]]; then
    [[ -e "$output" ]] && ! $force && die "'$output' exists — pass --force to overwrite it"
    (umask "$mask"; : > "$output") || die "cannot create '$output'"
    dest="$output"
  else
    dest="$(create_output "$input" "$suffix" "$mask")"
  fi
  label="$dest"
  transform "$from" "$to" < "$input" > "$dest"
fi

# ---- report ---------------------------------------------------------------
total=0
for i in "${!SECRET[@]}"; do total=$(( total + COUNT[i] )); done

if ! $quiet; then
  for i in "${!SECRET[@]}"; do
    (( COUNT[i] )) && printf '%s: %d\n' "${PLACEHOLDER[$i]}" "${COUNT[$i]}" >&2
  done
  printf '%s: %d replacement(s)\n' "$label" "$total" >&2
fi

status=0
if [[ -n "$dest" ]] && ! $reverse; then
  # A secret surviving sanitisation means one secret contains another's placeholder, or the map disagrees with
  # itself. Either way the file is not safe to hand over, and saying so is the whole point of the tool.
  leaked=()
  for i in "${!SECRET[@]}"; do
    grep -qF -e "${SECRET[$i]}" -- "$dest" && leaked+=("${PLACEHOLDER[$i]}")
  done
  if (( ${#leaked[@]} )); then
    warn "WARNING: a real secret is STILL present in $dest (placeholders: ${leaked[*]})"
    warn "         do NOT upload this file."
    status=2
  fi

  # A placeholder that was already in the input turns into a secret on the way back, inventing a credential in a
  # place that never had one. The round trip is lossless only if this is empty.
  collided=()
  for i in "${!SECRET[@]}"; do
    grep -qF -e "${PLACEHOLDER[$i]}" -- "$input" && collided+=("${PLACEHOLDER[$i]}")
  done
  if (( ${#collided[@]} )); then
    warn "WARNING: $input already contained the placeholder(s) ${collided[*]}"
    warn "         reversing would put a real secret where there was none — pick unused placeholders."
    status=2
  fi
fi

exit $status
