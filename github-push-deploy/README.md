# github-push-deploy

Deploy a website automatically whenever you push to GitHub, using a repo **push webhook** and a small PHP listener — no CI runner, no GitHub Actions, no third-party service.

It suits a plain Apache + PHP-FPM box where you can serve a directory and run shell: the classic "I have an SSH login and `/var/www`" setup.

## How it works

1. You add a **webhook** to your GitHub repo (Settings → Webhooks) pointing at a public URL that maps to `github-hook-listener.php`.
2. On every push, GitHub POSTs the event to that listener, signed with a shared secret.
3. [`github-hook-listener.php`](github-hook-listener.php) verifies the HMAC-SHA256 signature and checks the event is a push to the configured branch of the configured repo. If so, it runs [`update.sh`](update.sh).
4. [`update.sh`](update.sh) clones the repo fresh and sources the repo's own [`deploy.sh`](deploy.sh).
5. [`deploy.sh`](deploy.sh) copies the (possibly updated) machinery into place and publishes the site into the web root.

Because `deploy.sh` lives in your repo, a push can change how deployment itself works — the machinery updates itself.

All configuration lives in a single [`deploy.conf`](deploy.conf.example) read by both the PHP listener and the shell scripts, so nothing is configured in two places.

## Layout

Put the three machinery files in your website repository under `github-push-deploy/`:

```
your-repo/
├── github-push-deploy/
│   ├── github-hook-listener.php   # the webhook receiver
│   ├── update.sh                  # clone + dispatch
│   └── deploy.sh                  # YOUR publish logic (customise this)
└── ... the rest of your site ...
```

On the server, one base directory per site holds everything else (this is `BASE_DIR`):

```
/var/www/tools/          # BASE_DIR — not served directly
├── deploy.conf              # your config + secret (never served, never in git)
├── update.sh                # copied here by deploy.sh
├── deploy.log               # listener log
├── deploy-cmd.log           # deploy output log
├── repo/                    # fresh clone, recreated on each deploy
├── update-scripts/          # holds github-hook-listener.php — the ONLY web-exposed part
└── html/                    # DocumentRoot — the published site
```

Only `html/` (as `DocumentRoot`) and `update-scripts/` (via an `Alias`) are reachable from the web. `deploy.conf`, the logs, `update.sh` and `repo/` sit in `BASE_DIR` above both and are never served.

## Setup

Examples below use `tools` / `watercrossing/tools` / `tools.example.com`; substitute your own.

1. **Create the base directory** and its `html/` and `update-scripts/` subfolders:
   ```bash
   sudo mkdir -p /var/www/tools/{html,update-scripts}
   ```
   Make everything owned by the user PHP-FPM/Apache runs as (so it can clone, write logs and publish) — e.g. `sudo chown -R apache:apache /var/www/tools`.

2. **Give the deploy user a way to clone.** Create an SSH key for that user (e.g. in `/usr/share/httpd/.ssh`), add it as a **deploy key** on the repo, and point `CLONE_URL` at it. Using an SSH host alias keeps the URL tidy — in that user's `~/.ssh/config`:
   ```
   Host gh
     HostName github.com
     User git
     IdentityFile /usr/share/httpd/.ssh/id_ed25519
   ```
   then `CLONE_URL="gh:watercrossing/tools.git"`. (A public repo can use an `https://` URL and skip the key.)

3. **Write the config.** Copy [`deploy.conf.example`](deploy.conf.example) to `/var/www/tools/deploy.conf` and fill it in. Generate the secret with `openssl rand -hex 32` (hex keeps it safe for both shell and PHP parsing). It holds the webhook secret, so lock it down so other local users can't read it: `sudo chmod 600 /var/www/tools/deploy.conf` (and make sure it's owned by the deploy user).

4. **Expose the listener in Apache.** In your site's `<VirtualHost>`, serve `html/` as the `DocumentRoot` and alias a public path to `update-scripts/` — this is the only extra Apache config the tool needs:
   ```apache
   DocumentRoot "/var/www/tools/html"

   Alias "/github-webhook/tools" "/var/www/tools/update-scripts"
   <Directory "/var/www/tools/update-scripts">
       AllowOverride None
       Options -Indexes
       Require all granted
   </Directory>
   ```
   The listener is then at `https://tools.example.com/github-webhook/tools/github-hook-listener.php`.

5. **Add the webhook** on GitHub (repo → Settings → Webhooks → Add webhook):
   - **Payload URL**: the listener URL from step 4.
   - **Content type**: `application/json`.
   - **Secret**: the same value as `WEBHOOK_SECRET` in `deploy.conf`.
   - **Events**: just the push event.

6. **Bootstrap once by hand**, running as `apache` so every file created is owned by the deploy user from the start:
   ```bash
   sudo -u apache git clone gh:watercrossing/tools.git /var/www/tools/repo
   cd /var/www/tools/repo
   sudo -u apache env BASE_DIR=/var/www/tools bash github-push-deploy/deploy.sh
   ```
   `BASE_DIR` is passed inline only because this manual run bypasses `update.sh`, which would otherwise source it from `deploy.conf`; it is the one value `deploy.sh` needs. (`env` is just the portable way to set it through `sudo`.)

From now on, every push to `main` publishes automatically.

## Configuration reference

All keys live in `deploy.conf` (see [`deploy.conf.example`](deploy.conf.example)):

| Key | Meaning |
| --- | --- |
| `BASE_DIR` | Per-site base directory holding `html/`, `update-scripts/`, `repo/`, logs and this config. |
| `REPO_FULL_NAME` | `owner/name` of the repo allowed to trigger a deploy. |
| `BRANCH` | Only pushes to this branch deploy. |
| `CLONE_URL` | Git URL used to clone (SSH alias or full URL). |
| `DEPLOY_SCRIPT` | Path to the deploy script within the repo (default `github-push-deploy/deploy.sh`). |
| `WEBHOOK_SECRET` | Shared secret; must match the GitHub webhook's Secret field. |

## Debugging

- The listener logs to `$BASE_DIR/deploy.log`; the deploy output goes to `$BASE_DIR/deploy-cmd.log`. Both sit in `BASE_DIR`, so they are easy to find and are not web-accessible.
- **Run boundaries and timing.** `update.sh` brackets every run in `deploy-cmd.log` with a marker, so successive deploys don't run into each other:
  ```
  ===== deploy started 2026-07-29 14:57:02 +0100 =====
  ...traced output of update.sh and deploy.sh...
  ===== deploy finished 2026-07-29 14:57:04 +0100 — exit 0 after 1.7s =====
  ```
  The closing marker is written from an `EXIT` trap, so a failed deploy is still delimited and its exit status recorded. `deploy.log` gets the matching one-line summary from the listener (`update.sh exited 0 after 1.7s`). Manual runs print the same markers to the terminal.
  Elapsed time is reported to a tenth of a second. That needs GNU `date` (for `%1N`); on a `date` without it the timing degrades to whole seconds — always `.0` — rather than failing the deploy.
- **Log trimming.** Both logs are capped: whenever the listener handles a request, any log over 5 MB is cut back to its last 4 MB (at a line boundary) and marked with a `===== log trimmed … =====` line. So each file stays between 4 MB and roughly 5 MB plus one deploy's output.
- GitHub → repo → Settings → Webhooks → *Recent Deliveries* shows each POST, its response, and a **Redeliver** button to retry without pushing.
- Run the deploy path by hand as the deploy user: `sudo -u apache /var/www/tools/update.sh`.
- A `403` means the signature failed (wrong/absent secret). A `200` with `Ignored` means the signature was fine but it wasn't a push to the configured branch/repo.
- Rejected (bad-signature) requests are **not** written to `deploy.log` on purpose — the endpoint is public, so logging every probe would let anyone grow the log without bound. Inspect them in GitHub → Webhooks → *Recent Deliveries* instead; only authenticated requests reach the log.

## Notes and caveats

- **Blocking deploy.** The listener runs `update.sh` synchronously, so a very slow deploy can exceed GitHub's ~10s webhook timeout. The deploy still finishes server-side; GitHub just records a timeout and can redeliver. Background the command in the listener if this bites.
- **Non-atomic publish.** `deploy.sh` clears `html/` before repopulating it, so the site is briefly incomplete mid-deploy. For zero-downtime you could build into a new directory and swap a symlink (with `FollowSymLinks` enabled), since `renameat2(RENAME_EXCHANGE)` is not exposed to userspace.
- **Trust.** Anyone who can push to the branch can run arbitrary shell on the server via `deploy.sh` — that is the whole point, but scope the deploy key and secret accordingly.
