"""Defects found by the third independent pre-publication review.

Blocking:
  1. `devsync encrypt --paths` could report encryption ON while files stayed in
     plain text -- when cmd.exe passed the README's quotes through as part of the
     value, or when the PC running it did not have the agent whose files it named;
  2. an `adopt` run before the first machine had pushed left an empty checkout
     behind, and every later `adopt` then answered "not seeded" for ever;
  3. the README's test command needed a package its install line did not install.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from devsync import crypto
from devsync.cli import _load_engine, main
from devsync.engine import Summary, merge_store_meta

REPO = Path(__file__).resolve().parent.parent
PHRASE = "correct horse battery staple"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", "main"],
                          capture_output=True, text=True).stdout.strip()


def _use(mp, home):
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _pc(tmp_path, name):
    home = tmp_path / name
    (home / "code").mkdir(parents=True)
    (home / ".claude" / "skills" / "k").mkdir(parents=True)
    return home


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "DEVSYNC_CMD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a = _pc(tmp_path, "a")
    (a / ".claude" / "CLAUDE.md").write_text("claude notes\n")
    for n in ("f", "g"):
        (a / ".claude" / "skills" / "k" / f"{n}.md").write_text(f"{n}\n")
    _use(monkeypatch, a)
    return a, remote, tmp_path, monkeypatch


def _seed(home, remote, *extra):
    return main(["init", "--seed", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-A"])


def _adopt(home, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-B"])


# ── 1. "encryption is ON" has to be true ─────────────────────────────────────

def test_quotes_that_a_shell_passed_through_are_not_part_of_the_path(world):
    """cmd.exe hands  --paths 'claude/**,agents/**'  over with the quotes still on."""
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert main(["encrypt", "--all-machines-upgraded", "--paths", "'claude/**,agents/**'"]) == 0
    assert json.loads(_remote(remote, "manifest.json"))["encryption"]["paths"] == \
        ["repos/**", "claude/**", "agents/**"]
    assert _remote(remote, "claude/global/CLAUDE.md").startswith(crypto.MAGIC)


@pytest.mark.parametrize("bad", ["claude", "'nonsense/**'", "/etc/**", "*.env", "../x/**"])
def test_a_path_that_is_not_a_store_path_is_refused_and_nothing_changes(world, capsys, bad):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    head = _head(remote)
    capsys.readouterr()
    assert main(["encrypt", "--all-machines-upgraded", "--paths", bad]) == 2
    assert "not a store path" in capsys.readouterr().err
    assert _head(remote) == head
    assert "encryption" not in json.loads(_remote(remote, "manifest.json"))


def test_encrypting_an_agents_files_from_a_pc_that_does_not_have_that_agent(world):
    a, remote, tmp, mp = world
    (a / ".codex").mkdir()
    (a / ".codex" / "AGENTS.md").write_text("codex rules, sensitive\n")
    assert _seed(a, remote) == 0
    assert _remote(remote, "agents/codex/AGENTS.md") == b"codex rules, sensitive\n"

    b = _pc(tmp, "b")                                    # no ~/.codex here
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert main(["encrypt", "--all-machines-upgraded", "--paths", "agents/**"]) == 0
    blob = _remote(remote, "agents/codex/AGENTS.md")
    assert blob.startswith(crypto.MAGIC) and b"sensitive" not in blob     # true at once, not "eventually"
    assert not (b / ".codex").exists()

    _use(mp, a)
    assert main(["unlock"]) == 0
    assert main(["sync"]) == 0
    assert (a / ".codex" / "AGENTS.md").read_text() == "codex rules, sensitive\n"


# ── 2. an adopt that came too early ──────────────────────────────────────────

def test_an_adopt_before_the_store_is_seeded_works_when_run_again_after(world, capsys):
    a, remote, tmp, mp = world
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 1                        # too early: refused, checkout left behind
    assert (b / "code" / "devsync-store").exists()
    _use(mp, a)
    assert _seed(a, remote) == 0
    _use(mp, b)
    capsys.readouterr()
    assert _adopt(b, remote) == 0                        # used to say "not seeded" for ever
    assert (b / ".claude" / "CLAUDE.md").read_text() == "claude notes\n"


def test_a_remote_whose_default_branch_is_not_main_can_still_be_joined(world, tmp_path):
    a, _, tmp, mp = world
    remote = tmp_path / "master-head.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=master")
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert (b / ".claude" / "CLAUDE.md").read_text() == "claude notes\n"


# ── smaller findings ─────────────────────────────────────────────────────────

def _two(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    return a, b, remote, mp


def test_a_restore_is_not_undone_by_another_machines_delete(world):
    a, b, remote, mp = _two(world)
    _use(mp, a)
    assert main(["rm", "claude/skills/k/f.md"]) == 0
    _use(mp, b)
    assert main(["sync"]) == 0
    assert not (b / ".claude" / "skills" / "k" / "f.md").exists()
    _use(mp, a)
    assert main(["restore", "claude/skills/k/f.md"]) == 0          # A brings f back ...
    _use(mp, b)
    assert main(["rm", "claude/skills/k/g.md"]) == 0                # ... while B deletes g, unaware
    paths = [t["path"] for t in json.loads(_remote(remote, "tombstones.json"))]
    assert paths == ["claude/skills/k/g.md"]                        # f's tombstone did not come back
    assert (b / ".claude" / "skills" / "k" / "f.md").read_text() == "f\n"
    _use(mp, a)
    assert main(["sync"]) == 0
    assert (a / ".claude" / "skills" / "k" / "f.md").read_text() == "f\n"
    assert not (a / ".claude" / "skills" / "k" / "g.md").exists()


def test_tombstone_merge_is_three_way():
    t = lambda p, ts: {"path": p, "ts": ts, "machine": "m"}
    enc = lambda items: json.dumps(items).encode()
    base = enc([t("f", 1)])
    merged = json.loads(merge_store_meta("tombstones.json", base, enc([t("f", 1), t("g", 2)]), enc([])))
    assert [x["path"] for x in merged] == ["g"]                     # removed on one side stays removed
    merged = json.loads(merge_store_meta("tombstones.json", base, enc([t("f", 1), t("g", 2)]),
                                         enc([t("f", 1), t("h", 3)])))
    assert [x["path"] for x in merged] == ["f", "g", "h"]           # untouched entries survive


def test_restore_then_rm_again_works_as_the_verification_guide_says(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert main(["rm", "claude/skills/k/f.md"]) == 0
    assert main(["restore", "claude/skills/k/f.md"]) == 0
    assert _remote(remote, "claude/skills/k/f.md") == b"f\n"        # restore published it
    assert main(["rm", "claude/skills/k/f.md"]) == 0


def test_a_conflict_found_by_the_retry_pull_is_not_pushed(world):
    """Policy `ask`: another machine pushes between this one's pull and its push."""
    a, b, remote, mp = _two(world)
    _use(mp, a)
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["sync"]) == 0
    _use(mp, b)
    assert main(["sync"]) == 0
    (b / ".claude" / "CLAUDE.md").write_text("claude notes (B's wording)\n")
    eng = _load_engine()
    assert not eng.sync(push=False).pushed                          # B: committed, not pushed
    _use(mp, a)
    (a / ".claude" / "CLAUDE.md").write_text("claude notes (A's wording)\n")
    assert main(["sync"]) == 0                                      # A wins the race
    head = _head(remote)
    _use(mp, b)
    summary = Summary()
    eng = _load_engine()
    eng._run_ts = "20260101-000000"
    assert eng._push_with_retry(summary) is False
    assert _head(remote) == head
    assert b"<<<<<<<" not in _remote(remote, "claude/global/CLAUDE.md")
    assert any("push withheld" in n for n in summary.notes)


@pytest.mark.skipif(os.name == "nt", reason="no executable bit on Windows")
def test_the_executable_bit_survives_the_trip(world):
    a, remote, tmp, mp = world
    hook = a / ".claude" / "hooks" / "guard.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    plain = a / ".claude" / "hooks" / "notes.md"
    plain.write_text("not executable\n")
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert os.access(b / ".claude" / "hooks" / "guard.sh", os.X_OK)
    assert not os.access(b / ".claude" / "hooks" / "notes.md", os.X_OK)


# ── the README itself ────────────────────────────────────────────────────────

def test_readme_commands_carry_no_trailing_comment_and_the_test_extra_is_named():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    in_fence, offenders = False, []
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif in_fence and re.search(r"\S\s{2,}#\s", line):
            offenders.append(line)
    assert offenders == []            # cmd.exe passes a trailing "# note" to the command
    assert 'pip install -e ".[test]"' in text
    assert "--paths 'claude" not in text


def test_every_command_the_readme_names_exists():
    from devsync.cli import _main
    text = (REPO / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`devsync ([a-z]+)", text))
    for cmd in sorted(named):
        with pytest.raises(SystemExit) as e:
            _main([cmd, "--help"])
        assert e.value.code == 0, cmd
