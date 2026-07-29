#!/bin/bash

# github-push-deploy — example deploy script.
#
# Runs from the root of a freshly-cloned copy of your repository, with the variables from deploy.conf already in the environment (BASE_DIR, ...).
# It is sourced by update.sh; you can also run it by hand for the initial bootstrap:
#
#     BASE_DIR=/var/www/tools bash github-push-deploy/deploy.sh
#
# Customise the "Publish the site" section for your project (build step, etc.).

set -eux

# Guard against an unset/empty BASE_DIR before any destructive step below.
: "${BASE_DIR:?BASE_DIR is not set — run via update.sh or set it explicitly}"

# --- Update the deploy machinery itself (so future pushes can change it) -----
# Install both files by writing beside the target and renaming, never by overwriting in place. update.sh is *executing right now* (it sourced
# this script), and bash reads a script lazily by byte offset: overwrite it and bash resumes at that offset in the new file, mid-statement, and
# dies with a syntax error. rename(2) swaps the directory entry while the running copy keeps the original inode open, so it reads out intact.
# The same applies to the listener, which a concurrent webhook could be compiling. Both temp files live in BASE_DIR — same filesystem, so the
# rename is atomic, and never inside the web-exposed update-scripts/.
mkdir -p "$BASE_DIR/update-scripts"
cp "github-push-deploy/github-hook-listener.php" "$BASE_DIR/github-hook-listener.php.new"
mv -f "$BASE_DIR/github-hook-listener.php.new" "$BASE_DIR/update-scripts/github-hook-listener.php"
cp "github-push-deploy/update.sh" "$BASE_DIR/update.sh.new"
chmod +x "$BASE_DIR/update.sh.new"
mv -f "$BASE_DIR/update.sh.new" "$BASE_DIR/update.sh"

# --- Publish the site --------------------------------------------------------
# Two example strategies; this repo uses (B). Swap the comments to choose.

# (A) Simplest: copy the repo's files as-is into html/ (a plain static site), skipping dotfiles/dirs (.git, .github, .gitignore, ...) and the
#     ./github-push-deploy folder — otherwise the source of these deploy scripts, and a runnable (though inert, config-less) copy of
#     github-hook-listener.php, would be served publicly. Drop the `-name github-push-deploy` clause to include it, or add your own exclusions.
# mkdir -p "$BASE_DIR/html"
# rm -rf "$BASE_DIR/html/"*
# find . -mindepth 1 -maxdepth 1 \( -path './.*' -o -name github-push-deploy \) -prune -o -exec cp -r "{}" "$BASE_DIR/html/" \;

# (B) Rendered, GitHub-style browsable view via the repo-web-view tool: an index.html per folder (rendered README + listing) plus a .htaccess
#     that downloads plain files — scripts included, so github-hook-listener.php is served as a download and never executed — while folders
#     and .html tools render. Needs uv on the deploy user's PATH. Build into html-new, then swap it in with renames so the live site is
#     never served mid-rebuild.
#     Every page's footer is stamped with the commit being deployed, linked to it on GitHub. `git rev-parse` works fine in update.sh's shallow
#     clone; both values are allowed to come out empty (a run from a .git-less copy, or a deploy.conf without REPO_FULL_NAME), which leaves
#     the footer unstamped rather than failing the deploy.
SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
COMMIT_URL=""
if [ -n "$SHORT_SHA" ] && [ -n "${REPO_FULL_NAME:-}" ]; then COMMIT_URL="https://github.com/$REPO_FULL_NAME/commit/$(git rev-parse HEAD)"; fi

rm -rf "$BASE_DIR/html-new" "$BASE_DIR/html-old"
uv run repo-web-view/repo-web-view.py . "$BASE_DIR/html-new" --footer-note "$SHORT_SHA" --footer-note-url "$COMMIT_URL"
if [ -d "$BASE_DIR/html" ]; then mv "$BASE_DIR/html" "$BASE_DIR/html-old"; fi
mv "$BASE_DIR/html-new" "$BASE_DIR/html"
rm -rf "$BASE_DIR/html-old"

# --- Optional: post-deploy steps ---------------------------------------------
# e.g. build assets, reload apache (needs a sudoers rule), warm caches, etc.
