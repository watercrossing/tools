#!/bin/bash

# github-push-deploy — clone the repo fresh and run its deploy script.
#
# Invoked by github-hook-listener.php after a valid push webhook. This script
# lives in $BASE_DIR, next to deploy.conf, and is updated on each deploy.

set -euo pipefail

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
