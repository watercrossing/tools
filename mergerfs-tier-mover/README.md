# mergerfs-tier-mover

Demote the least-recently-modified files from the fast branch of a [mergerfs](https://github.com/trapexit/mergerfs) pool to the slow branch, until the fast branch is back above a free-space floor.

A tiered pool built the usual way —

```
/mnt/ssd/hot:/mnt/hdd/cold  /srv/pool  fuse.mergerfs  category.create=ff,minfreespace=500G,moveonenospc=true
```

— writes new files to the SSD until it drops below `minfreespace`, then spills to the HDD.
That is only half a tiering policy.
Nothing ever moves the other way, so the SSD fills once and stays full: every write after that lands on the HDD no matter how cold the data occupying the SSD has become, and the pool degrades to "an HDD with a small, permanently stale cache".

This is the missing half.
It walks the fast branch oldest-mtime-first and moves files to the slow branch until the floor is satisfied.
mergerfs keeps the logical path identical, so the application on top of the pool never sees a file move — no rescan, no broken links, no downtime, and no need to stop anything while it runs.

```bash
mergerfs-tier-mover.py --hot /mnt/ssd/hot --cold /mnt/hdd/cold --min-free 600G
```

Nothing is printed unless something happened; the summary goes to stderr, so it works as a cron job or a systemd timer either way.

```
moved 412 files, 38.71 GiB in 94.3s; free 561.02 GiB -> 600.44 GiB, floor 600.00 GiB (37 recent, 2 open, 118 dirs-pruned)
```

## Why a tool and not ten lines of shell

The obvious implementation is `find | sort | rsync --remove-source-files`, and on a *live* pool it has three ways to lose a file.
All three are cheap to avoid once, and impossible to remember to avoid every time:

- **Crash between the copy and the delete.**
  Every byte is `fsync`ed and the destination directory entry is `fsync`ed *before* the source is unlinked.
  A power cut can therefore leave a file on both branches — harmless, mergerfs serves the fast one — but never on neither.
- **A half-written file becoming visible.**
  The copy goes to a staging directory at the root of the slow branch and is `rename`d into place, so the destination path is either absent or complete.
  It is never a growing file that the application could read or that would show up in a directory listing of the pool.
- **Racing a writer.**
  Three guards, because one is not enough: files modified within `--min-age` (default 2h) are skipped; files currently held open by *any* process are skipped; and the source is re-`stat`ed immediately before the rename, so a file written during its own copy is abandoned rather than moved.

Hard-linked files are skipped rather than moved — copying each link separately would silently unshare them and inflate the pool.
Symlinks and special files are left alone.
A path that already exists on the slow branch is reported as a conflict and skipped, never overwritten.

Ownership, mode, POSIX ACLs and other xattrs, and mtime all survive the move.

**Directory mtimes survive it too**, which takes more work than it sounds and is worth the trouble.
Moving a file out of a directory bumps that directory's mtime on the fast branch; creating its counterpart on the slow branch stamps the new directory with *now*; and pruning an emptied directory bumps its parent.
All three are restored, so an application that indexes directory mtimes — Nextcloud does — does not see every directory the mover touched as changed and rescan it.
The one thing that cannot be preserved is a directory's *size* in bytes, which is a property of the entries physically in it and legitimately differs once they are split across two branches; mergerfs reports the first branch's.

## Verifying a run

The invariant is that the application cannot tell.
Check it by reading the tree back through the pool mountpoint and comparing against a manifest taken before, on **type, path, size, mtime, mode and owner** for files and directories alike, plus checksums for files.
Compare directory *sizes* and you will get a false alarm, for the reason above.

## mtime is the age signal, and it is wrong for caches

The branches want to be mounted `noatime` (atime writes are pure CoW metadata churn on btrfs, and they bloat snapshots), which leaves mtime as the only age signal on disk.
mtime is a good proxy for "cold" for documents and media, and a **bad** one for anything written once and read constantly — thumbnail and preview caches above all.
Those have an ancient mtime and are exactly the files whose latency you notice.

So excluding them is not tuning, it is the difference between a mover that helps and one that quietly makes browsing slow:

```bash
--exclude 'appdata_*' --exclude '*/cache'
```

`--exclude` takes an `fnmatch` glob, matched against the path relative to `--hot`, and it matches whole subtrees: a pattern that matches a directory excludes everything under it (and prunes the walk).
`*` also matches `/`, so `*/uploads` means "any path ending in `/uploads`, at any depth".

## The floor

`--min-free` is measured on the **filesystem** holding the fast branch, not on the branch or btrfs subvolume alone.
That is deliberate: mergerfs's own `minfreespace` measures the same thing, so the mover and the pool agree on when the floor has been crossed.
The consequence is worth stating plainly — if the fast branch shares a filesystem with anything else, both numbers are about that whole filesystem, and the mover cannot demote a single file until the *filesystem* dips below the floor, however full the branch's own data is.

Set `--min-free` **above** the pool's `minfreespace`, not equal to it.
Equal means the mover only starts once the pool is already spilling to the slow branch, and stops the moment it is one byte back over the line — so it runs every night and moves a trickle.
A floor 100–200 G above `minfreespace` gives the pool headroom to keep writing to the fast branch between runs.

## Options

| | |
|---|---|
| `--hot DIR` / `--cold DIR` | the two mergerfs *branches*, not the pool mountpoint. Refused if they are the same tree, nested, or on one filesystem (`--allow-same-filesystem` overrides, for testing) |
| `--min-free SIZE` | required. Binary suffixes: `500G`, `2T`, `1048576` |
| `--min-age MINUTES` | never touch a file modified this recently. Default 120 |
| `--exclude GLOB` | repeatable. See above |
| `--max-move SIZE` | stop after moving this much in one run — bounds the first run, or a nightly window |
| `--checksum` | verify each copy by SHA-256 as well as by size. Reads the copy back, so it roughly doubles the I/O |
| `--allow-open-files` | do not skip files held open by a process |
| `--lock FILE` | exclusive `flock`; exits 0 if held. Default `/run/mergerfs-tier-mover.lock` |
| `-n`, `--dry-run` | report what would move and stop |
| `-v` / `-q` | one line per file / print nothing on success |

Exit status is 0 on a clean run (**including** "another instance holds the lock"), 1 if any file failed to copy, 2 on a usage error.

### Point `--lock` at your snapshot job

If anything else snapshots or backs up the branches, give it and the mover the same `--lock` file.
Otherwise a file can be moved *between* the two branch snapshots and land in neither, which is the one failure the mover cannot see and the backup cannot report.

## Running it

It needs to read, write and `chown` files it does not own, so in practice it runs as root.
Run it as a normal user and it still works, but it cannot preserve ownership, and the open-file check degrades to seeing only its own descriptors — it says so on stderr rather than pretending the check passed.

Stdlib only, so `--script` metadata aside it runs under any system `python3` ≥ 3.12 as well as under `uv`.
That is why `requires-python` is `>=3.12` rather than the `>=3.13` the other tools here use: a root cron job or systemd unit typically has no `uv` on its `PATH`, and the installed copy of this script has to run under whatever `/usr/bin/python3` is.

A systemd timer is a better fit than cron — output lands in the journal rather than in mail, and a failure shows up in `systemctl --failed`:

```ini
# /etc/systemd/system/tier-mover.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/mergerfs-tier-mover.py --hot /mnt/ssd/hot --cold /mnt/hdd/cold --min-free 600G --exclude 'appdata_*'
IOSchedulingClass=idle
Nice=10
```

Install a **root-owned copy** at `/usr/local/sbin/`; do not point root's unit at a checkout inside a user's home directory, where anyone with write access to that directory can choose what root executes.

## Proving it on a live pool

The mover is hard to trigger on purpose, because a healthy pool is by definition above the floor.
Rather than filling the disk, raise the floor past what the machine has, and let the eligible-file list bound the run:

```bash
mergerfs-tier-mover.py --hot … --cold … --min-free 999P --dry-run --verbose   # what would move
mergerfs-tier-mover.py --hot … --cold … --min-free 999P --checksum            # move it
```

Then read the tree back **through the pool mountpoint** — paths, sizes, mtimes and checksums — and confirm it is unchanged.
That is the property that matters: the application must not be able to tell.
Do this while the pool is small and its contents are expendable, not after it holds the data you care about.

## Nextcloud

The pool this was written for is a Nextcloud data directory.
Two things there must never be demoted, and neither is obvious from the outside:

```bash
--exclude 'appdata_*'      # previews and thumbnails: written once, read constantly, latency you notice
--exclude '*/uploads'      # chunked-upload staging for uploads in flight
--exclude '*/cache'        # per-user scratch
--exclude 'nextcloud.log' --exclude '.htaccess' --exclude '.ncdata' --exclude 'index.html'
```

`files_versions/` and `files_trashbin/` are deliberately *not* excluded — old versions and deleted files are the best demotion candidates in the tree.

Nextcloud stores mtime and size in its database, and the mover changes neither, so no `occ files:scan` is needed after a run.
Do not run it against the *app* directory (`config/`, `apps/`, `themes/`) — only the data directory is a pool.

## Tests

```bash
uv run tests/test_mergerfs_tier_mover.py   # self-contained
pytest tests/                              # if pytest is already available
```

The tests drive the real CLI end-to-end against real directory trees, including the two race guards: a file held open across a run, and a file rewritten during its own copy.

## Limits

- **Demotion only.** Nothing is promoted back to the fast branch when cold data turns hot again. mergerfs will read it in place from the slow branch, which is the point, but a file that becomes busy stays slow until it is rewritten.
- **The open-file check is a snapshot** of `/proc`, refreshed at most once a minute during a run. A file opened in the gap is still protected by the re-`stat` before the rename, but a writer that opens a file, stays quiet for longer than `--min-age`, and only then writes can still have its write land on the unlinked inode. Set `--min-age` longer than your application's longest idle-but-open window.
- **Sparse files are expanded** by the copy. Nothing sparse belongs in the pools this targets, but a VM image would grow.
- **`rename` is not `RENAME_NOREPLACE`** — there is a race in principle between the conflict check and the rename. In practice only this tool writes into the slow branch at that path, and it holds a lock against itself.
