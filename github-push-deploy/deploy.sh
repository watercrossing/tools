#!/bin/bash

# github-push-deploy — example deploy script.
#
# Runs from the root of a freshly-cloned copy of your repository, with the
# variables from deploy.conf already in the environment (BASE_DIR, ...).
# It is sourced by update.sh; you can also run it by hand for the initial
# bootstrap:
#
#     BASE_DIR=/var/www/tools bash github-push-deploy/deploy.sh
#
# Customise the "Publish the site" section for your project (build step, etc.).

set -eux

# Guard against an unset/empty BASE_DIR before any destructive step below.
: "${BASE_DIR:?BASE_DIR is not set — run via update.sh or set it explicitly}"

# --- Update the deploy machinery itself (so future pushes can change it) -----
mkdir -p "$BASE_DIR/update-scripts"
cp "github-push-deploy/github-hook-listener.php" "$BASE_DIR/update-scripts/github-hook-listener.php"
cp "github-push-deploy/update.sh" "$BASE_DIR/update.sh"
chmod +x "$BASE_DIR/update.sh"

# --- Publish the site --------------------------------------------------------
# Replace the contents of html/ with the repo contents, skipping dotfiles/dirs
# (.git, .github, .gitignore, ...). Add your own exclusions or a build step to
# taste.
#
# NOTE: as written this also copies ./github-push-deploy into html/, so the
# source of these scripts — and a runnable (though inert, config-less) copy of
# github-hook-listener.php — get served publicly. Harmless for a public repo,
# but most sites should exclude it: add `-o -name github-push-deploy -prune` to
# the find below, or switch to an explicit allow-list of what to publish.
mkdir -p "$BASE_DIR/html"
rm -rf "$BASE_DIR/html/"*
find . -mindepth 1 -maxdepth 1 -path './.*' -prune \
  -o -exec cp -r "{}" "$BASE_DIR/html/" \;

# --- Optional: post-deploy steps ---------------------------------------------
# e.g. build assets, reload apache (needs a sudoers rule), warm caches, etc.
