#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest"]
# ///
"""
Tests for sanitise.sh.

Each test drives the real CLI end-to-end in a tmp_path, so exit codes, the derived output name, the file mode and the
stderr warnings are exercised as a user sees them. Every invocation passes an explicit --map, so no test can touch a
real ~/.config/sanitise/map.

    uv run tests/test_sanitise.py   # self-contained (installs pytest via uv)
    pytest tests/                   # if pytest is already available
"""
import stat, subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "sanitise.sh"

MAP = {"s3cr3t-db-pass!": "password1", "hunter2": "password2", "p@ss*w[ord?": "password3"}
CONFIG = 'db: "s3cr3t-db-pass!"\nadmin: hunter2\nweird: p@ss*w[ord?\nplain: nothing\n'
SANITISED = 'db: "password1"\nadmin: password2\nweird: password3\nplain: nothing\n'


def write_map(tmp_path, pairs=MAP, name="map"):
    path = tmp_path / name
    path.write_text("".join(f"{secret}\t{placeholder}\n" for secret, placeholder in pairs.items()))
    path.chmod(0o600)
    return path


def run(*args, cwd=None, check=None):
    proc = subprocess.run([str(SCRIPT), *map(str, args)], capture_output=True, text=True, cwd=cwd)
    if check is not None:
        assert proc.returncode == check, f"exit {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return proc


@pytest.fixture
def env(tmp_path):
    """A map file, a config full of secrets, and the two paths."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    return write_map(tmp_path), config


def test_sanitise_writes_derived_name(env, tmp_path):
    mapfile, config = env
    run("-m", mapfile, config, check=0)
    assert (tmp_path / "config-sanitised.yaml").read_text() == SANITISED


def test_existing_output_is_never_overwritten(env, tmp_path):
    mapfile, config = env
    for expected in ("config-sanitised.yaml", "config-sanitised-2.yaml", "config-sanitised-3.yaml"):
        run("-m", mapfile, config, check=0)
        assert (tmp_path / expected).exists()


def test_round_trip_is_lossless(env, tmp_path):
    mapfile, config = env
    run("-m", mapfile, config, check=0)
    run("-m", mapfile, "-r", tmp_path / "config-sanitised.yaml", check=0)
    assert (tmp_path / "config-sanitised-unsanitised.yaml").read_text() == CONFIG


def test_glob_metacharacters_are_literal(env, tmp_path):
    """`p@ss*w[ord?` is a valid glob and would match half the line if the secret were used as a pattern."""
    mapfile, _ = env
    target = tmp_path / "t.txt"
    target.write_text("a p@ss*w[ord? b\n")
    assert run("-m", mapfile, target, "-o", "-", check=0).stdout == "a password3 b\n"


def test_overlapping_placeholders_round_trip(tmp_path):
    """password1 must not be substituted inside the password10 that another pair just wrote, in either direction."""
    mapfile = write_map(tmp_path, {"alpha-secret": "password10", "beta-secret": "password1"})
    target = tmp_path / "t.txt"
    target.write_text("one: alpha-secret\ntwo: beta-secret\nboth: alpha-secretbeta-secret\n")
    assert run("-m", mapfile, target, "-o", "-", check=0).stdout == "one: password10\ntwo: password1\nboth: password10password1\n"
    run("-m", mapfile, target, check=0)
    run("-m", mapfile, "-r", tmp_path / "t-sanitised.txt", check=0)
    assert (tmp_path / "t-sanitised-unsanitised.txt").read_text() == target.read_text()


def test_secret_containing_another_secret(tmp_path):
    """The longer secret wins where both start at the same place, so the shorter one cannot bite off a prefix."""
    mapfile = write_map(tmp_path, {"topsecret": "PH_LONG", "top": "PH_SHORT"})
    target = tmp_path / "t.txt"
    target.write_text("topsecret and top\n")
    assert run("-m", mapfile, target, "-o", "-", check=0).stdout == "PH_LONG and PH_SHORT\n"


@pytest.mark.parametrize("text", ["a: hunter2", "a: hunter2\n", "", "hunter2\n\n\n"])
def test_trailing_newline_is_preserved(env, tmp_path, text):
    mapfile, _ = env
    target = tmp_path / "t.txt"
    target.write_text(text)
    assert run("-m", mapfile, target, "-o", "-", check=0).stdout == text.replace("hunter2", "password2")


def test_dotfile_keeps_its_leading_dot(env, tmp_path):
    mapfile, _ = env
    target = tmp_path / ".env"
    target.write_text("PASS=hunter2\n")
    run("-m", mapfile, target, check=0)
    assert (tmp_path / ".env-sanitised").read_text() == "PASS=password2\n"


def test_reversed_output_is_not_world_readable(env, tmp_path):
    """The reversed file holds the real secrets, so it is 600 whatever the umask says."""
    mapfile, config = env
    run("-m", mapfile, config, check=0)
    run("-m", mapfile, "-r", tmp_path / "config-sanitised.yaml", check=0)
    mode = (tmp_path / "config-sanitised-unsanitised.yaml").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)


def test_summary_counts_each_placeholder(env):
    mapfile, config = env
    err = run("-m", mapfile, config, "-o", "-", check=0).stderr
    assert "password1: 1" in err and "3 replacement(s)" in err
    assert run("-m", mapfile, config, "-o", "-", "-q", check=0).stderr == ""


def test_options_may_follow_the_filename(env, tmp_path):
    mapfile, config = env
    assert run("-m", mapfile, config, "-o", "-", "-q", check=0).stdout == SANITISED


# ---- the safety checks ----------------------------------------------------

def test_check_reports_lines_and_exits_2(env):
    mapfile, config = env
    proc = run("-m", mapfile, "-c", config, check=2)
    assert proc.stdout.splitlines() == [
        f"{config}:1: secret for placeholder password1",
        f"{config}:2: secret for placeholder password2",
        f"{config}:3: secret for placeholder password3",
    ]


def test_check_never_prints_the_secret_itself(env):
    """A scanner that echoes the secret into a CI log has leaked it somewhere new."""
    mapfile, config = env
    proc = run("-m", mapfile, "-c", config, check=2)
    assert not any(secret in proc.stdout + proc.stderr for secret in MAP)


def test_check_is_clean_on_a_sanitised_file(env, tmp_path):
    mapfile, config = env
    run("-m", mapfile, config, check=0)
    assert run("-m", mapfile, "-c", tmp_path / "config-sanitised.yaml", check=0).stdout == ""


def test_placeholder_already_in_input_is_reported(env, tmp_path):
    """Reversing would put a real secret on a line that never had one, so the round trip is not lossless."""
    mapfile, _ = env
    target = tmp_path / "t.txt"
    target.write_text("note: password1 is the placeholder\npass: hunter2\n")
    proc = run("-m", mapfile, target, check=2)
    assert "already contained the placeholder(s) password1" in proc.stderr


def test_secret_surviving_sanitisation_is_reported(tmp_path):
    mapfile = write_map(tmp_path, {"pass": "password9"})
    target = tmp_path / "t.txt"
    target.write_text("x: pass\n")
    proc = run("-m", mapfile, target, check=2)
    assert "STILL present" in proc.stderr


def test_world_readable_map_warns(env):
    mapfile, config = env
    mapfile.chmod(0o644)
    assert "readable by others" in run("-m", mapfile, config, "-o", "-", check=0).stderr


# ---- map file errors ------------------------------------------------------

@pytest.mark.parametrize("body,message", [
    ("no-tab-on-this-line\n",           "no tab"),
    ("a\tp1\nb\tp1\n",                  "used twice"),
    ("a\tp1\na\tp2\n",                  "already mapped"),
    ("\tp1\n",                          "empty secret"),
    ("a\t\n",                           "empty placeholder"),
    ("a\ta\n",                          "same string"),
    ("a\tb\tc\n",                       "more than one tab"),
    ("# only a comment\n",              "no replacements defined"),
])
def test_bad_map_is_rejected(tmp_path, body, message):
    mapfile = tmp_path / "map"
    mapfile.write_text(body)
    mapfile.chmod(0o600)
    target = tmp_path / "t.txt"
    target.write_text("x\n")
    assert message in run("-m", mapfile, target, check=1).stderr


def test_comments_and_blank_lines_and_hash_secrets(tmp_path):
    """A '#' line with no tab is a comment; a '#' line with one is a secret that happens to start with '#'."""
    mapfile = tmp_path / "map"
    mapfile.write_text("# a comment\n\n   \n#hash-secret\tpassword4\nhunter2\tpassword2\n")
    mapfile.chmod(0o600)
    target = tmp_path / "t.txt"
    target.write_text("a: #hash-secret\nb: hunter2\n")
    assert run("-m", mapfile, target, "-o", "-", "-q", check=0).stdout == "a: password4\nb: password2\n"


def test_missing_map_is_an_error(tmp_path):
    target = tmp_path / "t.txt"
    target.write_text("x\n")
    assert "cannot read map file" in run("-m", tmp_path / "nope", target, check=1).stderr


def test_missing_input_is_an_error(env, tmp_path):
    mapfile, _ = env
    assert "not a regular file" in run("-m", mapfile, tmp_path / "nope", check=1).stderr


# ---- --init ---------------------------------------------------------------

def test_init_writes_a_private_template(tmp_path):
    target = tmp_path / "cfg" / "map"
    run("--init", "-m", target, check=0)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "\t" in target.read_text()
    assert "already exists" in run("--init", "-m", target, check=1).stderr


def test_init_template_is_a_usable_map(tmp_path):
    """Whatever --init writes has to load, or the first thing a new user does is hit an error."""
    target = tmp_path / "map"
    run("--init", "-m", target, check=0)
    probe = tmp_path / "t.txt"
    probe.write_text("x\n")
    run("-m", target, probe, "-o", "-", check=0)


def test_shipped_example_map_is_a_usable_map(tmp_path):
    probe = tmp_path / "t.txt"
    probe.write_text("x\n")
    run("-m", SCRIPT.parent / "sanitise.map.example", probe, "-o", "-", check=0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
