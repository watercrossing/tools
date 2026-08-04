#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Demote the least-recently-modified files from the fast branch of a mergerfs pool to the slow branch, until the fast branch is back inside a
free-space floor (--min-free), a size budget (--max-hot), or both.

A tiered mergerfs pool (`category.create=ff` over an SSD branch and an HDD branch, with `minfreespace=N`) writes new files to the SSD until it
drops below the floor, then spills to the HDD. That is only half a tiering policy: nothing ever moves back the other way, so the SSD fills once
and every subsequent write lands on the HDD regardless of how cold the data on the SSD has become. This is the missing half -- it walks the fast
branch oldest-mtime-first and moves files to the slow branch until the limit is satisfied again. mergerfs keeps the logical path identical, so
the application on top of the pool never sees a file move.

The two limits answer different questions and the choice is a real one. --min-free measures the *filesystem* holding the branch, exactly as
mergerfs's own minfreespace does, so the two agree on when the pool is under pressure; that gives "fast disk with the slow one as overflow", and
it stays idle until the disk is genuinely full. --max-hot measures the branch itself, so it gives a working set: the newest N bytes stay fast and
everything older is pushed out, pressure or no pressure.

Why mtime and not atime: the branches are mounted `noatime` (atime writes are pure CoW metadata churn on btrfs, and they bloat snapshots), so
mtime is the only age signal on disk. It is the wrong signal for anything that is read often but written once -- thumbnail and preview caches
above all -- which is what `--exclude` is for. Excluding those trees is not an optimisation, it is what stops the mover from demoting exactly
the data whose latency you notice.

The move is the fiddly part, and the reason this is a tool rather than the ten lines of shell it looks like. A naive `rsync --remove-source-files`
per file has three ways to lose data on a live pool, all of which this avoids:

  * Crash between the copy and the delete. Here every byte is fsynced and the destination directory entry is fsynced before the source is
    unlinked, so a power cut can leave a file on both branches (harmless -- mergerfs serves the fast one) but never on neither.
  * A half-written file becoming visible. The copy goes to a staging directory at the root of the slow branch and is renamed into place, so the
    destination path either does not exist or is complete. It is never a growing file that the application could read or the pool could list.
  * Racing a writer. Files modified within `--min-age` are skipped; any file held open by any process is skipped (a snapshot of /proc, refreshed
    as the run proceeds); and the source is re-stat'd immediately before the rename, so a file written during its own copy is abandoned rather
    than moved. The residual risk is a writer that opens a file, goes quiet for longer than --min-age, and then writes -- see README.

Files with more than one link are skipped rather than moved: copying each link separately would silently unshare them and inflate the pool.

Exit status is 0 on a clean run (including "another instance holds the lock"), 1 if any file failed, 2 on a usage error.
"""
import argparse, errno, fcntl, fnmatch, hashlib, os, signal, stat, sys, time, uuid

CHUNK = 4 << 20            # copy buffer; large enough that syscall overhead is irrelevant next to the disks
OPEN_FD_MAX_AGE = 60.0     # re-snapshot /proc at most this often -- a long run's first snapshot goes stale in both directions
STAGING = ".mergerfs-tier-mover.tmp"
UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}

stop = False               # set from SIGINT/SIGTERM; the loop finishes the file in flight and exits cleanly


def parse_size(text):
    """'500G' / '2T' / '1048576' -> bytes. Binary units, because that is what statvfs and mergerfs's minfreespace both count in."""
    s = text.strip().upper().removesuffix("IB").removesuffix("B") or "0"
    unit = s[-1] if s and s[-1] in UNITS else ""
    try:
        return int(float(s[: len(s) - len(unit)] or "0") * UNITS[unit])
    except (KeyError, ValueError):
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (expected e.g. 500G, 2T, 1048576)")


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def free_bytes(path):
    """Free space as the pool sees it. Note this is the whole *filesystem* holding the branch, not the branch (or btrfs subvolume) alone --
    which is deliberate: mergerfs's own minfreespace measures the same thing, so the mover and the pool agree on when the floor is crossed."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def branch_usage(path):
    """On-disk bytes held by a branch. Counts files the excludes will never move -- they occupy the budget just the same -- and uses st_blocks
    rather than st_size, because with btrfs compression the two differ a lot and a budget is about disk. Multiply-linked files are counted per
    link, which overstates; they are skipped by the mover anyway."""
    return sum(os.lstat(os.path.join(dirpath, name)).st_blocks * 512
               for dirpath, _, filenames in os.walk(path, followlinks=False) for name in filenames if os.path.lexists(os.path.join(dirpath, name)))


def open_inodes():
    """(dev, ino) of every file held open by any process. Needs root to see other users' descriptors; as a normal user it sees only its own,
    which makes the check useless rather than wrong -- so the caller warns instead of pretending it passed."""
    found = set()
    for pid in (e for e in os.listdir("/proc") if e.isdigit()):
        d = f"/proc/{pid}/fd"
        try:
            entries = os.listdir(d)
        except OSError:
            continue                                    # process exited, or not ours to look at
        for e in entries:
            try:
                st = os.stat(f"{d}/{e}")
            except OSError:
                continue                                # fd closed under us, or a socket/pipe that stat cannot follow
            found.add((st.st_dev, st.st_ino))
    return found


def excluded(rel, patterns):
    """True if rel, or any directory above it, matches a pattern. Matching an ancestor lets '--exclude appdata_*' drop a whole subtree and lets
    the walk prune it. Patterns are fnmatch, so '*' also matches '/' -- '*/uploads' means "any path ending in /uploads", at any depth."""
    parts = rel.split("/")
    return any(fnmatch.fnmatch("/".join(parts[: i + 1]), pat) for i in range(len(parts)) for pat in patterns)


def candidates(hot, min_age_s, patterns, now):
    """Every regular file on the fast branch eligible to move, as (mtime_ns, rel, stat). Symlinks, specials and multiply-linked files are out."""
    skipped = {"recent": 0, "linked": 0, "special": 0}
    for dirpath, dirnames, filenames in os.walk(hot, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, hot)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not excluded(f"{rel_dir}/{d}".lstrip("/"), patterns)]
        for name in filenames:
            rel = f"{rel_dir}/{name}".lstrip("/")
            if excluded(rel, patterns):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped["special"] += 1
            elif st.st_nlink > 1:
                skipped["linked"] += 1
            elif now - st.st_mtime < min_age_s:
                skipped["recent"] += 1
            else:
                yield st.st_mtime_ns, rel, st
    yield skipped                                       # last item: the tallies, so one pass reports both


def copy_metadata(src_st, dst, src):
    """Ownership, mode, xattrs (POSIX ACLs live in one), then times last -- anything written after utime would move mtime again."""
    try:
        os.chown(dst, src_st.st_uid, src_st.st_gid)
    except PermissionError:
        pass                                            # not root: the copy still belongs to the caller, reported by the caller's warning
    os.chmod(dst, stat.S_IMODE(src_st.st_mode))
    for attr in os.listxattr(src, follow_symlinks=False):
        try:
            os.setxattr(dst, attr, os.getxattr(src, attr, follow_symlinks=False), follow_symlinks=False)
        except OSError:
            pass                                        # namespaces we may not write (system.*) are best-effort, not fatal
    os.utime(dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns), follow_symlinks=False)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_to_staging(src, tmp, checksum):
    """Copy src to tmp and fsync it. Returns the digest of what was written, or None. The hash is of the bytes as read, so comparing it against a
    re-read of tmp checks the whole path through page cache and disk rather than just the length."""
    h = hashlib.sha256() if checksum else None
    with open(src, "rb", buffering=0) as fsrc, open(tmp, "wb", buffering=0) as fdst:
        while chunk := fsrc.read(CHUNK):
            fdst.write(chunk)
            if h:
                h.update(chunk)
        os.fsync(fdst.fileno())
    return h.hexdigest() if h else None


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def ensure_parent(hot, cold, rel, created):
    """Recreate rel's ancestor directories on the slow branch, carrying ownership and mode from the fast branch.

    mtimes cannot be set here -- renaming the next file in would move them again -- so each new directory is recorded in `created` and the times
    are applied once at the end of the run. Worth the bookkeeping: an application that indexes directory mtimes (Nextcloud does) otherwise sees
    every demoted directory as changed and rescans it."""
    parts = rel.split("/")[:-1]
    for i in range(len(parts)):
        sub = "/".join(parts[: i + 1])
        dst = os.path.join(cold, sub)
        if not os.path.isdir(dst):
            os.mkdir(dst)
            try:
                src_st = os.stat(os.path.join(hot, sub))
                os.chown(dst, src_st.st_uid, src_st.st_gid)
                os.chmod(dst, stat.S_IMODE(src_st.st_mode))
                created.append((dst, src_st.st_mtime_ns))
            except (OSError, PermissionError):
                pass


def move_one(hot, cold, staging, rel, st, checksum, created):
    """Copy one file to the slow branch and unlink it from the fast one. Returns None on success or a string saying why it was left alone.

    The ordering is the whole point: fsync data, fsync the destination directory entry, re-check the source, and only then unlink. Interrupted
    anywhere, the file exists on the fast branch (mergerfs serves it) or on both (mergerfs still serves the fast one) -- never on neither."""
    src, dst = os.path.join(hot, rel), os.path.join(cold, rel)
    if os.path.lexists(dst):
        return "conflict"                               # same logical path already on the slow branch; mergerfs hides one of them. Not ours to resolve
    tmp = os.path.join(staging, uuid.uuid4().hex)
    try:
        written = copy_to_staging(src, tmp, checksum)
        tmp_st = os.stat(tmp)
        if tmp_st.st_size != st.st_size:
            return "short-copy"
        if checksum and digest(tmp) != written:
            return "checksum-mismatch"
        copy_metadata(st, tmp, src)
        now_st = os.lstat(src)
        if (now_st.st_size, now_st.st_mtime_ns, now_st.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            return "changed-during-copy"                # somebody wrote it while we were reading; the copy is stale, drop it
        ensure_parent(hot, cold, rel, created)
        os.rename(tmp, dst)
        tmp = None
        fsync_dir(os.path.dirname(dst))
        src_dir_mtime = os.stat(os.path.dirname(src)).st_mtime_ns
        os.unlink(src)
        os.utime(os.path.dirname(src), ns=(src_dir_mtime, src_dir_mtime))   # the entry changed branches; through the pool the directory did not
        return None
    finally:
        if tmp and os.path.lexists(tmp):
            os.unlink(tmp)


def prune_empty_dirs(hot, cold, dry_run):
    """Remove directories emptied by the move -- but only where the same path exists on the slow branch, so a user-created empty folder that
    lives only on the fast branch does not vanish from the pool.

    Deepest first, and emptiness is decided by rmdir itself rather than by the walk's `dirnames`/`filenames`: os.walk captured those before this
    pass removed the children, so a parent whose every child we just deleted still looks non-empty there and the prune would stop one level down.
    rmdir simply fails with ENOTEMPTY when it should, which also closes the race with a writer creating a file mid-pass."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(hot, topdown=False):
        rel = os.path.relpath(dirpath, hot)
        if dirpath == hot or not os.path.isdir(os.path.join(cold, rel)):
            continue
        if dry_run:
            removed += not (dirnames or filenames)      # under-counts: a cascade cannot be simulated without doing it
            continue
        parent = os.path.dirname(dirpath)
        try:
            parent_mtime = os.stat(parent).st_mtime_ns
            os.rmdir(dirpath)
            removed += 1
            os.utime(parent, ns=(parent_mtime, parent_mtime))   # the entry moved branches; through the pool nothing about the parent changed
        except OSError:
            pass                                        # not empty, or not ours to remove
    return removed


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Demote cold files from a mergerfs pool's fast branch to its slow branch.",
                                 epilog="Sizes take binary suffixes: 500G, 2T, 1048576.")
    ap.add_argument("--hot", required=True, metavar="DIR", help="fast branch (the mergerfs branch, not the pool mountpoint)")
    ap.add_argument("--cold", required=True, metavar="DIR", help="slow branch")
    ap.add_argument("--min-free", type=parse_size, metavar="SIZE",
                    help="demote until the fast branch's *filesystem* has this much free. Set it at or above the pool's minfreespace")
    ap.add_argument("--max-hot", type=parse_size, metavar="SIZE",
                    help="demote until the fast *branch* holds no more than this on disk. Combinable with --min-free; whichever is unsatisfied keeps the mover going")
    ap.add_argument("--min-age", type=float, default=120, metavar="MINUTES", help="never touch a file modified this recently (default: 120)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip paths matching GLOB, relative to --hot; matches whole subtrees. Repeatable")
    ap.add_argument("--max-move", type=parse_size, default=0, metavar="SIZE", help="stop after moving this much in one run (default: no limit)")
    ap.add_argument("--checksum", action="store_true", help="verify each copy by SHA-256 as well as by size (slower: reads the copy back)")
    ap.add_argument("--allow-open-files", action="store_true", help="do not skip files currently held open by a process")
    ap.add_argument("--allow-same-filesystem", action="store_true", help="permit --hot and --cold on one filesystem (for testing; moves nothing real)")
    ap.add_argument("--lock", default="/run/mergerfs-tier-mover.lock", metavar="FILE",
                    help="exclusive lock; exits 0 if held. Point it at the same file as any snapshot/backup job (default: %(default)s)")
    ap.add_argument("--no-lock", action="store_true", help="do not lock at all")
    ap.add_argument("-n", "--dry-run", action="store_true", help="report what would move and stop")
    ap.add_argument("-v", "--verbose", action="store_true", help="one line per file")
    ap.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
    args = ap.parse_args(argv)
    if args.min_free is None and args.max_hot is None:
        ap.error("give --min-free, --max-hot, or both: there is otherwise no condition that would stop the mover")
    args.hot, args.cold = os.path.realpath(args.hot), os.path.realpath(args.cold)
    for label, path in (("--hot", args.hot), ("--cold", args.cold)):
        if not os.path.isdir(path):
            ap.error(f"{label}: not a directory: {path}")
    if args.hot == args.cold or args.cold.startswith(args.hot + os.sep) or args.hot.startswith(args.cold + os.sep):
        ap.error("--hot and --cold must be separate trees, neither inside the other")
    if os.stat(args.hot).st_dev == os.stat(args.cold).st_dev and not args.allow_same_filesystem:
        ap.error("--hot and --cold are on the same filesystem: moving between them frees nothing (use --allow-same-filesystem to override)")
    return args


def main(argv=None):
    args = parse_args(argv)
    err = lambda *a: print(*a, file=sys.stderr, flush=True)      # failures are reported even under --quiet: silence is for a clean run only
    log = (lambda *a: None) if args.quiet else err
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: globals().__setitem__("stop", True))

    lock_fd = None
    if not args.no_lock:
        lock_fd = os.open(args.lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if args.verbose:
                log(f"another instance holds {args.lock}; nothing to do")
            return 0

    if not args.allow_open_files and os.geteuid() != 0:
        log("warning: not running as root, so the open-file check sees only this process's descriptors")

    start, free_start = time.monotonic(), free_bytes(args.hot)
    used_start = branch_usage(args.hot) if args.max_hot is not None else None
    moved_files = moved_bytes = moved_blocks = failures = 0
    reasons, open_fds, open_fds_at, created_dirs = {}, set(), 0.0, []
    staging = os.path.join(args.cold, STAGING)
    if not args.dry_run:
        os.makedirs(staging, mode=0o700, exist_ok=True)

    def unsatisfied():
        """Whichever target is still violated keeps the mover going; it stops when every one of them is met."""
        free = free_start + moved_bytes if args.dry_run else free_bytes(args.hot)
        return ((args.min_free is not None and free < args.min_free)
                or (args.max_hot is not None and used_start - moved_blocks > args.max_hot))

    try:
        if not unsatisfied():
            targets = [f"{human(free_bytes(args.hot))} free against a {human(args.min_free)} floor"] if args.min_free is not None else []
            targets += [f"branch holds {human(used_start)} against a {human(args.max_hot)} budget"] if args.max_hot is not None else []
            log("nothing to demote: " + " and ".join(targets))
        else:
            found = list(candidates(args.hot, args.min_age * 60, args.exclude, time.time()))
            tallies = found.pop()
            reasons.update({k: v for k, v in tallies.items() if v})
            for _, rel, st in sorted(found):
                if stop or (args.max_move and moved_bytes >= args.max_move) or not unsatisfied():
                    break
                if not args.allow_open_files:
                    if time.monotonic() - open_fds_at > OPEN_FD_MAX_AGE:
                        open_fds, open_fds_at = open_inodes(), time.monotonic()
                    if (st.st_dev, st.st_ino) in open_fds:
                        reasons["open"] = reasons.get("open", 0) + 1
                        continue
                if args.dry_run:
                    why = None
                    if os.path.lexists(os.path.join(args.cold, rel)):
                        why = "conflict"
                else:
                    try:
                        why = move_one(args.hot, args.cold, staging, rel, st, args.checksum, created_dirs)
                    except OSError as e:
                        why, failures = f"error: {e.strerror}", failures + 1
                        err(f"failed  {rel}: {e}")
                if why:
                    key = why.split(":")[0]
                    reasons[key] = reasons.get(key, 0) + 1
                    failures += key in ("short-copy", "checksum-mismatch")   # a benign skip is not a failure; a bad copy is
                    if args.verbose and not why.startswith("error"):
                        log(f"skip    {rel}: {why}")
                else:
                    moved_files, moved_bytes = moved_files + 1, moved_bytes + st.st_size
                    moved_blocks += st.st_blocks * 512       # what the *branch* got back, which compression makes different from st_size
                    if args.verbose:
                        log(f"{'would move' if args.dry_run else 'moved'}  {rel}  ({human(st.st_size)})")
            pruned = prune_empty_dirs(args.hot, args.cold, args.dry_run)
            if pruned:
                reasons["dirs-pruned"] = pruned
            for path, mtime_ns in reversed(created_dirs):   # deepest last created, so restore in reverse: a child's rename bumps its parent
                try:
                    os.utime(path, ns=(mtime_ns, mtime_ns))
                except OSError:
                    pass
    finally:
        if not args.dry_run and os.path.isdir(staging):
            for leftover in os.listdir(staging):
                os.unlink(os.path.join(staging, leftover))  # only reachable if we died mid-copy; the file is a partial and is not wanted
            os.rmdir(staging)
        if lock_fd is not None:
            os.close(lock_fd)

    detail = ", ".join(f"{v} {k}" for k, v in sorted(reasons.items()))
    targets = [f"free {human(free_start)} -> {human(free_bytes(args.hot))}, floor {human(args.min_free)}"] if args.min_free is not None else []
    targets += [f"branch {human(used_start)} -> {human(used_start - moved_blocks)}, budget {human(args.max_hot)}"] if args.max_hot is not None else []
    summary = (f"{'would move' if args.dry_run else 'moved'} {moved_files} files, {human(moved_bytes)} in {time.monotonic() - start:.1f}s; "
               + "; ".join(targets) + (f" ({detail})" if detail else "") + (" [interrupted]" if stop else ""))
    (err if failures else log)(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
