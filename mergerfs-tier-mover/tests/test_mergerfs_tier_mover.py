#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest"]
# ///
"""
Tests for mergerfs-tier-mover.py.

Each test drives the real CLI end-to-end against two real directory trees, so exit codes, the on-disk result and the summary line are exercised
as a user sees them. The trees are on one filesystem (tmp_path), which is why every invocation passes --allow-same-filesystem; that flag exists
for exactly this. --min-free is therefore steered explicitly rather than by really filling a disk: a floor of 0 means "already satisfied, move
nothing" and a floor larger than the machine has means "move everything eligible", which is also how the mover is proved on a live pool.

    uv run tests/test_mergerfs_tier_mover.py   # self-contained (installs pytest via uv)
    pytest tests/                              # if pytest is already available
"""
import importlib.util, os, subprocess, sys, time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "mergerfs-tier-mover.py"
_spec = importlib.util.spec_from_file_location("mover", SCRIPT)             # hyphenated filename: import it by path for the unit-level checks
mover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mover)
HUGE = "999P"          # a floor no filesystem satisfies -> the run is bounded by eligible files, not by free space


def run(hot, cold, *extra, lock=None):
    locking = ["--lock", str(lock)] if lock else ["--no-lock"]
    cmd = [sys.executable, str(SCRIPT), "--hot", str(hot), "--cold", str(cold), "--allow-same-filesystem", *locking, *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


def branches(tmp_path, files, age_minutes=180):
    """Two branch trees; every file is backdated so it is older than the default --min-age."""
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    for d in (hot, cold):
        d.mkdir(exist_ok=True)
    for rel, body in files.items():
        f = hot / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body.encode() if isinstance(body, str) else body)
        os.utime(f, (time.time() - age_minutes * 60, time.time() - age_minutes * 60))
    return hot, cold


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_moves_everything_eligible_when_the_floor_cannot_be_met(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa", "u/files/sub/b.txt": "bbb"})
    r = run(hot, cold, "--min-free", HUGE)
    assert r.returncode == 0, r.stderr
    assert tree(cold) == {"u/files/a.txt": b"aaa", "u/files/sub/b.txt": b"bbb"}
    assert tree(hot) == {}
    assert "moved 2 files" in r.stderr


def test_a_satisfied_floor_moves_nothing(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    r = run(hot, cold, "--min-free", "0")
    assert r.returncode == 0 and tree(cold) == {} and tree(hot) == {"u/files/a.txt": b"aaa"}
    assert "nothing to demote" in r.stderr


def test_oldest_files_go_first(tmp_path):
    hot, cold = branches(tmp_path, {"old.txt": "o" * 4096, "new.txt": "n" * 4096})
    os.utime(hot / "new.txt", (time.time() - 7200, time.time() - 7200))      # 2h old: eligible, but younger than old.txt
    os.utime(hot / "old.txt", (time.time() - 86400, time.time() - 86400))
    r = run(hot, cold, "--min-free", HUGE, "--max-move", "1")                 # one file, then the cap trips
    assert r.returncode == 0 and list(tree(cold)) == ["old.txt"], r.stderr


def test_recent_files_are_never_touched(tmp_path):
    hot, cold = branches(tmp_path, {"cold.txt": "c"}, age_minutes=180)
    (hot / "hot.txt").write_bytes(b"h")                                       # written now -> inside the default 120 min window
    r = run(hot, cold, "--min-free", HUGE)
    assert tree(cold) == {"cold.txt": b"c"} and tree(hot) == {"hot.txt": b"h"}
    assert "1 recent" in r.stderr


def test_excludes_drop_whole_subtrees(tmp_path):
    hot, cold = branches(tmp_path, {"appdata_x/preview/1.png": "p", "u/uploads/part": "q", "u/files/keep.txt": "k"})
    r = run(hot, cold, "--min-free", HUGE, "--exclude", "appdata_*", "--exclude", "*/uploads")
    assert tree(cold) == {"u/files/keep.txt": b"k"}
    assert set(tree(hot)) == {"appdata_x/preview/1.png", "u/uploads/part"}


def test_metadata_survives_the_move(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    src = hot / "u/files/a.txt"
    os.chmod(src, 0o640)
    before = src.stat()
    assert run(hot, cold, "--min-free", HUGE).returncode == 0
    after = (cold / "u/files/a.txt").stat()
    assert after.st_mtime_ns == before.st_mtime_ns and after.st_mode == before.st_mode and after.st_size == before.st_size


def test_hardlinked_files_are_left_alone(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    os.link(hot / "u/files/a.txt", hot / "u/files/b.txt")
    r = run(hot, cold, "--min-free", HUGE)
    assert tree(cold) == {} and "2 linked" in r.stderr                        # both links counted, neither moved


def test_symlinks_are_left_alone(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    (hot / "u/files/link").symlink_to("a.txt")
    r = run(hot, cold, "--min-free", HUGE)
    assert tree(cold) == {"u/files/a.txt": b"aaa"} and (hot / "u/files/link").is_symlink() and "1 special" in r.stderr


def test_a_path_present_on_both_branches_is_a_conflict_not_an_overwrite(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "hot version"})
    (cold / "u/files").mkdir(parents=True)
    (cold / "u/files/a.txt").write_bytes(b"cold version")
    r = run(hot, cold, "--min-free", HUGE)
    assert tree(cold) == {"u/files/a.txt": b"cold version"} and tree(hot) == {"u/files/a.txt": b"hot version"}
    assert "1 conflict" in r.stderr


def test_dry_run_changes_nothing(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    r = run(hot, cold, "--min-free", HUGE, "--dry-run")
    assert r.returncode == 0 and tree(hot) == {"u/files/a.txt": b"aaa"} and tree(cold) == {}
    assert "would move 1 files" in r.stderr


def test_emptied_dirs_are_pruned_but_only_where_cold_has_them(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    (hot / "u/files/empty-on-hot-only").mkdir()                               # a user-created empty folder: must survive
    assert run(hot, cold, "--min-free", HUGE).returncode == 0
    assert (hot / "u/files/empty-on-hot-only").is_dir()
    assert not (hot / "u/files/a.txt").exists() and (cold / "u/files/a.txt").exists()


def test_pruning_cascades_up_through_nested_emptied_dirs(tmp_path):
    """os.walk's dirnames are a snapshot taken before the children were removed, so a prune that trusts them stops one level down."""
    hot, cold = branches(tmp_path, {"u/files/a/b/c/deep.txt": "d"})
    assert run(hot, cold, "--min-free", HUGE).returncode == 0
    assert not (hot / "u").exists(), sorted(str(p.relative_to(hot)) for p in hot.rglob("*"))
    assert (cold / "u/files/a/b/c/deep.txt").read_bytes() == b"d"


def test_created_directories_keep_their_mtime(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    for d in (hot / "u", hot / "u/files"):
        os.utime(d, (1_000_000_000, 1_000_000_000))
    assert run(hot, cold, "--min-free", HUGE).returncode == 0
    assert (cold / "u").stat().st_mtime == 1_000_000_000 and (cold / "u/files").stat().st_mtime == 1_000_000_000


def test_checksum_verification_accepts_a_good_copy(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/big.bin": os.urandom(1 << 20).hex()})
    r = run(hot, cold, "--min-free", HUGE, "--checksum")
    assert r.returncode == 0 and tree(cold) and "moved 1 files" in r.stderr


def test_the_lock_makes_a_second_run_a_no_op(tmp_path):
    import fcntl
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    lock = tmp_path / "lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = run(hot, cold, "--min-free", HUGE, lock=lock)
        assert r.returncode == 0 and tree(cold) == {} and tree(hot) == {"u/files/a.txt": b"aaa"}
    finally:
        os.close(fd)
    assert run(hot, cold, "--min-free", HUGE, lock=lock).returncode == 0 and tree(cold) == {"u/files/a.txt": b"aaa"}


def test_a_file_held_open_is_skipped(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa", "u/files/b.txt": "bbb"})
    with open(hot / "u/files/a.txt", "rb"):                                   # our own fd, so /proc shows it even unprivileged
        r = run(hot, cold, "--min-free", HUGE)
    assert tree(cold) == {"u/files/b.txt": b"bbb"} and "1 open" in r.stderr
    assert run(hot, cold, "--min-free", HUGE, "--allow-open-files").returncode == 0 and "u/files/a.txt" in tree(cold)


def test_a_file_written_during_its_own_copy_is_abandoned_not_moved(tmp_path):
    """The guard that matters most: the copy is stale, so the source must survive intact and the destination must not appear."""
    hot, cold = branches(tmp_path, {"u/files/a.txt": "original"})
    staging = cold / "staging"
    staging.mkdir()
    real_copy = mover.copy_to_staging

    def racing_copy(src, tmp, checksum):
        result = real_copy(src, tmp, checksum)
        Path(src).write_bytes(b"a writer got there first")                    # mutates size and mtime behind the copy's back
        return result

    mover.copy_to_staging = racing_copy
    try:
        st = os.lstat(hot / "u/files/a.txt")
        why = mover.move_one(str(hot), str(cold), str(staging), "u/files/a.txt", st, False, [])
    finally:
        mover.copy_to_staging = real_copy
    assert why == "changed-during-copy"
    assert (hot / "u/files/a.txt").read_bytes() == b"a writer got there first"
    assert not (cold / "u/files/a.txt").exists() and list(staging.iterdir()) == []


def test_no_staging_directory_is_left_behind(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    assert run(hot, cold, "--min-free", HUGE).returncode == 0
    assert [p.name for p in cold.iterdir()] == ["u"]


def test_same_filesystem_is_refused_without_the_override(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--hot", str(hot), "--cold", str(cold), "--min-free", "1G", "--no-lock"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "same filesystem" in r.stderr


def test_nested_branches_are_refused(tmp_path):
    hot, cold = branches(tmp_path, {"u/files/a.txt": "aaa"})
    nested = hot / "inner"
    nested.mkdir()
    r = run(hot, nested, "--min-free", "1G")
    assert r.returncode == 2 and "neither inside the other" in r.stderr


@pytest.mark.parametrize("text,expected", [("500G", 500 << 30), ("2T", 2 << 40), ("1048576", 1048576), ("500GiB", 500 << 30), ("1.5G", 1610612736)])
def test_size_suffixes(text, expected):
    assert mover.parse_size(text) == expected


def test_a_bad_size_is_a_usage_error(tmp_path):
    hot, cold = branches(tmp_path, {})
    assert run(hot, cold, "--min-free", "lots").returncode == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
