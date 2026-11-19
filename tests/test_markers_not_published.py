"""A conflict found while PULLING must be remembered, exactly like one found on the live side.

Before this, under conflict policy `ask`: a machine with an unpushed store commit pulled
another machine's change to the same lines, got conflict markers in its store copy and
no sidecar. That run withheld the push. The next run saw no sidecar, reported
conflicts=0 and pushed the marked file to every machine.
"""
import subprocess

from devsync.cli import _load_engine, main

from test_review_round4 import _adopt, _git, _pc, _remote, _seed, _use, world  # noqa: F401

CANON = "claude/global/CLAUDE.md"
MARK = b"<<<<<<<"


def _two_pcs_with_a_pull_conflict(world):
    """A and B share a store; B holds an unpushed change, A pushed a change to the same line."""
    a, remote, tmp, mp = world
    (a / ".claude" / "CLAUDE.md").write_text("one\nshared\nthree\n")
    assert _seed(a, remote) == 0
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["sync"]) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    (b / ".claude" / "CLAUDE.md").write_text("one\nshared (B)\nthree\n")
    assert not _load_engine().sync(push=False).pushed        # committed locally, never pushed
    _use(mp, a)
    (a / ".claude" / "CLAUDE.md").write_text("one\nshared (A)\nthree\n")
    assert main(["sync"]) == 0
    _use(mp, b)
    return a, b, remote, mp


def test_a_pull_conflict_leaves_a_sidecar_and_is_never_pushed(world, capsys):
    a, b, remote, mp = _two_pcs_with_a_pull_conflict(world)
    sidecar = b / ".claude" / "CLAUDE.md.conflict"

    assert main(["sync"]) == 2                               # found while pulling
    assert sidecar.is_file() and CANON in sidecar.read_text()
    assert "devsync resolve" in sidecar.read_text()
    assert _remote(remote, CANON) == b"one\nshared (A)\nthree\n"

    for _ in range(2):                                       # and it stays withheld, run after run
        capsys.readouterr()
        assert main(["sync"]) == 2
        assert "pushed=no" in capsys.readouterr().out
        assert MARK not in _remote(remote, CANON)
    assert [p.name for p in _load_engine().conflicts()] == ["CLAUDE.md.conflict"]

    _use(mp, a)                                              # the other machine never sees markers
    assert main(["sync"]) == 0
    assert (a / ".claude" / "CLAUDE.md").read_bytes() == b"one\nshared (A)\nthree\n"


def test_resolving_a_pull_conflict_publishes_the_settled_file(world):
    a, b, remote, mp = _two_pcs_with_a_pull_conflict(world)
    assert main(["sync"]) == 2
    assert MARK in (b / ".claude" / "CLAUDE.md").read_bytes()

    assert main(["resolve", "--all", "--keep", "local"]) == 0
    assert not (b / ".claude" / "CLAUDE.md.conflict").exists()
    assert main(["sync"]) == 0
    assert _remote(remote, CANON) == b"one\nshared (B)\nthree\n"
    _use(mp, a)
    assert main(["sync"]) == 0
    assert (a / ".claude" / "CLAUDE.md").read_bytes() == b"one\nshared (B)\nthree\n"


def test_a_pull_conflict_on_a_file_this_machine_has_no_place_for_takes_the_other_copy(world, capsys):
    """Nobody on this machine could settle it, so it must not become markers that get pushed."""
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["sync"]) == 0
    store_a = a / "code" / "devsync-store"
    (store_a / "extra").mkdir()
    (store_a / "extra" / "note.txt").write_bytes(b"one\nshared\nthree\n")
    assert main(["sync"]) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    store_b = b / "code" / "devsync-store"
    (store_b / "extra" / "note.txt").write_bytes(b"one\nshared (B)\nthree\n")
    _git(store_b, "commit", "-am", "B, unpushed")
    _use(mp, a)
    (store_a / "extra" / "note.txt").write_bytes(b"one\nshared (A)\nthree\n")
    assert main(["sync"]) == 0                               # commits the store edit and pushes it
    _use(mp, b)

    capsys.readouterr()
    assert main(["sync"]) == 0
    assert "kept the other machine's copy" in capsys.readouterr().out
    assert (store_b / "extra" / "note.txt").read_bytes() == b"one\nshared (A)\nthree\n"
    assert _remote(remote, "extra/note.txt") == b"one\nshared (A)\nthree\n"
    unmerged = subprocess.run(["git", "-C", str(store_b), "diff", "--name-only", "--diff-filter=U"],
                              capture_output=True, text=True).stdout.strip()
    assert unmerged == "" and not (store_b / ".git" / "MERGE_HEAD").exists()
    trashed = [p for p in (b / ".devsync").rglob("*") if p.is_file() and "note.txt" in p.name]
    assert trashed and any(p.read_bytes() == b"one\nshared (B)\nthree\n" for p in trashed)
