#!/bin/bash

# github-push-deploy — clone the repo fresh and run its deploy script.
#
# Invoked by github-hook-listener.php after a valid push webhook. This script
# lives in $BASE_DIR, next to deploy.conf, and is updated on each deploy.

set -euo pipefail

# --- Run markers -------------------------------------------------------------
# The listener appends this script's output to deploy-cmd.log, so without these successive runs would run into each other. Bracket every run
# with a timestamped marker and report how long it took. The EXIT trap fires even when a step fails, so the closing marker (and its exit
# status) is always written.
# Elapsed time is kept in tenths of a second, as integers — bash has no float arithmetic. GNU date's %1N is the tenths digit; if date lacks it
# (busybox, BSD) the probe below appends a literal 0 instead, degrading to whole-second resolution rather than breaking the deploy.
[[ $(date +%1N) == [0-9] ]] && EPOCH_DS='+%s%1N' || EPOCH_DS='+%s0'
RUN_START=$(date "$EPOCH_DS")
printf '\n===== deploy started %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"

on_exit() {
  { set +x; } 2>/dev/null  # ... without tracing the trap's own bookkeeping
  local ds=$(($(date "$EPOCH_DS") - RUN_START))
  printf '===== deploy finished %s — exit %d after %d.%ds =====\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$1" "$((ds / 10))" "$((ds % 10))"
}
trap 'on_exit $?' EXIT

# Locate deploy.conf, which sits next to this script in BASE_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the config WITHOUT tracing: it holds WEBHOOK_SECRET, and `set -x`
# echoes every assignment in a sourced file to stderr — which the listener
# captures into deploy-cmd.log, leaking the secret into a logfile.
source "$SCRIPT_DIR/deploy.conf"

# Trace everything from here on (the interesting part, and free of secrets).
set -x

DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-github-push-deploy/deploy.sh}"

cd "$BASE_DIR"
rm -rf repo

# Relies on CLONE_URL being reachable by this user (e.g. an SSH deploy key
# configured in the deploy user's ~/.ssh/config).
git clone --depth 1 --branch "$BRANCH" "$CLONE_URL" repo

cd repo
# Sourced (not executed) so the deploy script inherits the config variables.
source "$DEPLOY_SCRIPT"
