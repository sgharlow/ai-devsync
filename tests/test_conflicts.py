"""Conflicts without a person in the loop (0.2.0).

Two ways out of a real same-line conflict, both under test:

  * policy "newer" (new stores): settled at once for the newer side, the other
    side's file kept in trash, nothing withheld;
  * policy "ask" (every store that existed before the setting): a sidecar and a
    withheld push exactly as before -- plus `devsync resolve`, which settles it in
    one command and fixes all three copies, so the conflict cannot come back.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from devsync.cli import main
from devsync.engine import DEFAULT_MANIFEST, new_store_manifest
from devsync.merge import has_markers, merge_text, reconcile, resolve_markers

PREFIX_A = "C--Users-A-code"
PREFIX_B = "C--Users-B-code"
BASE = "title\nshared line\nfooter\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote_file(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _use(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _touch(path: Path, delta: int):
    """Make `path` unambiguously newer (+) or older (-) than any commit in the test."""
    t = time.time() + delta
    os.utime(path, (t, t))


@pytest.fixture
def two_pcs(tmp_path, monkeypatch):
    """A and B joined to one new store, both holding the same CLAUDE.md."""
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR", "DEVSYNC_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a, b = tmp_path / "a", tmp_path / "b"
    for home in (a, b):
        (home / "code").mkdir(parents=True)
        (home / ".claude").mkdir()
    (a / ".claude" / "CLAUDE.md").write_text(BASE)
    _use(monkeypatch, a)
    assert main(["init", "--seed", "--remote", str(remote), "--root", str(a / "code"),
                 "--claude-home", str(a / ".claude"), "--hostname", "PC-A",
                 "--project-key-prefix", PREFIX_A]) == 0
    _use(monkeypatch, b)
    assert main(["adopt", "--remote", str(remote), "--root", str(b / "code"),
                 "--claude-home", str(b / ".claude"), "--hostname", "PC-B",
                 "--project-key-prefix", PREFIX_B]) == 0
    return a, b, remote, monkeypatch


def _diverge(a, b, mp, b_delta):
    """A rewrites the shared line and syncs; B rewrites it differently and adds a
    line of its own elsewhere. Returns B's file path."""
    _use(mp, a)
    (a / ".claude" / "CLAUDE.md").write_text("title\nshared line (A's wording)\nfooter\n")
    assert main(["sync"]) == 0
    f = b / ".claude" / "CLAUDE.md"
    f.write_text("title\nshared line (B's wording)\nfooter\nadded on B\n")
    _touch(f, b_delta)
    _use(mp, b)
    return f


def _set_policy(home, mp, policy):
    _use(mp, home)
    assert main(["config", "set", "conflict-policy", policy]) == 0
    assert main(["sync"]) == 0


# ── the merge primitive ──────────────────────────────────────────────────────

def test_prefer_settles_the_contested_hunk_and_keeps_everything_else():
    base = b"a\nshared\nc\n"
    ours = b"a\nshared-ours\nc\nours-added\n"
    theirs = b"theirs-added\na\nshared-theirs\nc\n"
    merged, conflict = merge_text(base, ours, theirs, prefer="ours")
    assert not conflict and not has_markers(merged)
    assert merged == b"theirs-added\na\nshared-ours\nc\nours-added\n"
    merged, conflict = merge_text(base, ours, theirs, prefer="theirs")
    assert not conflict
    assert merged == b"theirs-added\na\nshared-theirs\nc\nours-added\n"


def test_without_prefer_the_merge_still_marks_the_conflict():
    merged, conflict = merge_text(b"a\nx\n", b"a\nours\n", b"a\ntheirs\n")
    assert conflict and has_markers(merged)


def test_policy_newer_returns_no_conflict_and_hands_back_the_other_side():
    res = reconcile("text", b"a\nx\n", b"a\nours\n", b"a\ntheirs\n", ours_newer=False, policy="newer")
    assert not res.conflict and res.auto_resolved
    assert res.merged == b"a\ntheirs\n" and res.loser == b"a\nours\n"


def test_policy_ask_is_the_default_and_is_unchanged():
    res = reconcile("text", b"a\nx\n", b"a\nours\n", b"a\ntheirs\n", ours_newer=True)
    assert res.conflict and not res.auto_resolved and has_markers(res.merged)


def test_policy_newer_never_fires_when_there_is_no_conflict():
    res = reconcile("text", b"a\n", b"a\nours\n", b"a\n", ours_newer=True, policy="newer")
    assert res.merged == b"a\nours\n" and not res.auto_resolved and res.loser is None


def test_policy_newer_keeps_json_valid():
    base = b'{"a": 1, "b": 1}'
    res = reconcile("structured", base, b'{"a": 2, "b": 1, "mine": true}',
                    b'{"a": 3, "b": 1, "yours": true}', ours_newer=True, policy="newer")
    obj = json.loads(res.merged)
    assert res.auto_resolved and not res.conflict
    assert obj["a"] == 2 and obj["b"] == 1


def test_resolve_markers_each_way():
    marked = b"top\n<<<<<<< LOCAL\nmine\n=======\ntheirs\n>>>>>>> REMOTE\nbottom\n"
    assert resolve_markers(marked, "local") == b"top\nmine\nbottom\n"
    assert resolve_markers(marked, "remote") == b"top\ntheirs\nbottom\n"
    assert resolve_markers(marked, "both") == b"top\nmine\ntheirs\nbottom\n"
    assert not has_markers(resolve_markers(marked, "local"))
    with pytest.raises(ValueError):
        resolve_markers(marked, "neither")


def test_a_line_of_equals_signs_in_ordinary_text_is_not_a_marker():
    plain = b"Heading\n=======\nbody\n"
    assert not has_markers(plain)
    assert resolve_markers(plain, "local") == plain


# ── which stores get which policy ────────────────────────────────────────────

def test_new_stores_settle_conflicts_and_old_stores_ask():
    assert new_store_manifest()["conflictPolicy"] == "newer"
    assert "conflictPolicy" not in DEFAULT_MANIFEST


def test_config_shows_and_sets_the_policy(two_pcs, capsys):
    a, b, remote, mp = two_pcs
    capsys.readouterr()
    assert main(["config"]) == 0
    assert json.loads(capsys.readouterr().out)["store"]["conflict-policy"] == "newer"
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["config", "set", "conflict-policy", "whenever"]) == 2
    manifest = json.loads((b / "code" / "devsync-store" / "manifest.json").read_text())
    assert manifest["conflictPolicy"] == "ask"


# ── policy "newer", end to end ───────────────────────────────────────────────

def test_newer_local_edit_wins_with_no_sidecar_and_nothing_withheld(two_pcs, capsys):
    a, b, remote, mp = two_pcs
    f = _diverge(a, b, mp, b_delta=+60)
    capsys.readouterr()

    assert main(["sync"]) == 0

    out = capsys.readouterr().out
    assert "autoresolved=1" in out and "pushed=yes" in out and "conflicts=0" in out
    expected = "title\nshared line (B's wording)\nfooter\nadded on B\n"
    assert f.read_text() == expected
    assert _remote_file(remote, "claude/global/CLAUDE.md") == expected
    assert not (b / ".claude" / "CLAUDE.md.conflict").exists()
    # A's version is not lost: it is in B's trash and named in the log
    losers = list((b / ".devsync" / "trash").glob("*/claude/global/CLAUDE.md.loser"))
    assert len(losers) == 1 and "A's wording" in losers[0].read_text()
    log = (b / ".devsync" / "conflicts.log").read_text()
    assert "claude/global/CLAUDE.md" in log and "this PC's copy" in log

    _use(mp, a)
    assert main(["sync"]) == 0
    assert (a / ".claude" / "CLAUDE.md").read_text() == expected


def test_newer_store_copy_wins_but_local_additions_elsewhere_survive(two_pcs, capsys):
    a, b, remote, mp = two_pcs
    f = _diverge(a, b, mp, b_delta=-100000)
    capsys.readouterr()
    assert main(["sync"]) == 0
    assert "autoresolved=1" in capsys.readouterr().out
    merged = f.read_text()
    assert "shared line (A's wording)" in merged and "B's wording" not in merged
    assert "added on B" in merged                       # B's non-conflicting line is kept
    losers = list((b / ".devsync" / "trash").glob("*/claude/global/CLAUDE.md.loser"))
    assert len(losers) == 1 and "B's wording" in losers[0].read_text()


# ── policy "ask" plus `devsync resolve` ──────────────────────────────────────

def _conflicted(two_pcs):
    a, b, remote, mp = two_pcs
    _set_policy(a, mp, "ask")
    _use(mp, b)
    assert main(["sync"]) == 0                          # B picks the policy up
    f = _diverge(a, b, mp, b_delta=+60)
    assert main(["sync"]) == 2                          # sidecar written, push withheld
    assert (b / ".claude" / "CLAUDE.md.conflict").is_file()
    assert has_markers(f.read_bytes())
    assert "A's wording" in _remote_file(remote, "claude/global/CLAUDE.md")
    return a, b, remote, mp, f


@pytest.mark.parametrize("keep, wanted, unwanted", [
    ("local", "B's wording", "A's wording"),
    ("remote", "A's wording", "B's wording"),
])
def test_resolve_all_settles_the_conflict_and_it_does_not_come_back(two_pcs, keep, wanted, unwanted):
    a, b, remote, mp, f = _conflicted(two_pcs)

    assert main(["resolve", "--all", "--keep", keep]) == 0

    text = f.read_text()
    assert wanted in text and unwanted not in text and "added on B" in text
    assert not has_markers(f.read_bytes())
    assert not (b / ".claude" / "CLAUDE.md.conflict").exists()
    assert _remote_file(remote, "claude/global/CLAUDE.md") == text       # pushed
    assert main(["sync"]) == 0                                            # and stays settled
    assert main(["conflicts"]) == 0
    # the marked-up version was kept
    assert list((b / ".devsync" / "trash").glob("*/claude/global/CLAUDE.md.conflicted"))

    _use(mp, a)
    assert main(["sync"]) == 0
    assert (a / ".claude" / "CLAUDE.md").read_text() == text
    assert not (a / ".claude" / "CLAUDE.md.conflict").exists()


def test_resolve_accepts_a_hand_edited_file_and_ends_the_resolve_loop(two_pcs):
    """The trap this command replaces: fix the live file by hand, delete the
    sidecar, sync -- and conflict again, because the store still holds the marker
    text. `resolve` writes the fixed file to live, store and base."""
    a, b, remote, mp, f = _conflicted(two_pcs)
    fixed = "title\nshared line (agreed wording)\nfooter\nadded on B\n"
    f.write_text(fixed)

    assert main(["resolve", str(f)]) == 0               # no --keep needed: no markers left

    assert _remote_file(remote, "claude/global/CLAUDE.md") == fixed
    assert not (b / ".claude" / "CLAUDE.md.conflict").exists()
    assert main(["sync"]) == 0
    _use(mp, a)
    assert main(["sync"]) == 0
    assert (a / ".claude" / "CLAUDE.md").read_text() == fixed


def test_resolve_wants_a_choice_while_markers_remain_and_changes_nothing(two_pcs, capsys):
    a, b, remote, mp, f = _conflicted(two_pcs)
    before = f.read_bytes()
    capsys.readouterr()
    assert main(["resolve", "--all"]) == 2
    assert "--keep" in capsys.readouterr().err
    assert f.read_bytes() == before
    assert (b / ".claude" / "CLAUDE.md.conflict").is_file()


def test_resolve_takes_a_canonical_path_and_lists_when_given_nothing(two_pcs, capsys):
    a, b, remote, mp, f = _conflicted(two_pcs)
    capsys.readouterr()
    assert main(["resolve"]) == 2
    assert "CLAUDE.md.conflict" in capsys.readouterr().err
    assert main(["resolve", "claude/global/CLAUDE.md", "--keep", "both"]) == 0
    text = f.read_text()
    assert "A's wording" in text and "B's wording" in text and not has_markers(f.read_bytes())


def test_resolve_with_no_conflicts_says_so(two_pcs, capsys):
    capsys.readouterr()
    assert main(["resolve", "--all", "--keep", "local"]) == 0
    assert "no unresolved conflicts" in capsys.readouterr().out
