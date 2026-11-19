"""End-to-end two-machine acceptance tests.

Each 'machine' gets its own cascade_root, claude_home, state dir, and clone of
a shared bare remote. We drive devsync.engine.Engine directly.
"""
import json
import subprocess
from pathlib import Path

import pytest

from devsync.engine import Engine
from devsync.paths import Machine

MANIFEST = {
    "version": 1,
    "generatedDirs": ["node_modules", ".venv", "dist"],
    "blocklistGlobs": ["*.log"],
    "allowlistGlobs": [".env*", "*.pem", "*.sqlite"],
    "captureAllNonBlocked": False,
    "claudePaths": {
        "global": ["CLAUDE.md", "settings.json"],
        "trees": ["skills", "commands", "hooks", "lib"],
        "memoryGlob": "projects/*/memory/*.md",
    },
    "categoryByExt": {
        ".md": "text", ".txt": "text",
        ".env": "env",
        ".json": "structured",
        ".png": "binary", ".sqlite": "binary", ".pem": "binary",
    },
    "trashRetentionDays": 90,
}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Box:
    """One simulated PC."""

    def __init__(self, root: Path, name: str, user: str, remote: Path):
        self.dir = root / name
        cascade = self.dir / "CascadeProjects"
        claude = self.dir / ".claude"
        self.state = self.dir / ".devsync"
        store = self.dir / "devsync-store"
        prefix = f"C--Users-{user}-CascadeProjects"
        (claude / "projects" / prefix / "memory").mkdir(parents=True)
        (claude / "skills").mkdir(parents=True)
        cascade.mkdir(parents=True)
        subprocess.run(["git", "clone", str(remote), str(store)],
                       check=True, capture_output=True)
        _git(store, "config", "user.email", f"{user}@test")
        _git(store, "config", "user.name", user)
        self.machine = Machine(hostname=name, cascade_root=cascade,
                               claude_home=claude, project_key_prefix=prefix)
        self.engine = Engine(machine=self.machine, store_dir=store,
                             state_dir=self.state)
        self.memory = claude / "projects" / prefix / "memory" / "MEMORY.md"

    def make_project(self, name="proj"):
        proj = self.machine.cascade_root / name
        proj.mkdir(exist_ok=True)
        if not (proj / ".git").exists():
            _git(proj, "init")
            (proj / ".gitignore").write_text(".env*\n*.sqlite\n")
            (proj / "main.py").write_text("pass\n")
            _git(proj, "add", ".")
            _git(proj, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-m", "init")
        return proj


@pytest.fixture
def boxes(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a = Box(tmp_path, "PC-A", "alice", remote)
    b = Box(tmp_path, "PC-B", "bob", remote)
    # seed manifest from A
    (a.engine.store_dir / "manifest.json").write_text(json.dumps(MANIFEST, indent=2))
    _git(a.engine.store_dir, "add", "-A")
    _git(a.engine.store_dir, "commit", "-m", "manifest")
    _git(a.engine.store_dir, "push", "origin", "HEAD:main")
    _git(b.engine.store_dir, "pull", "origin", "main")
    return a, b


def test_memory_additive_union(boxes):
    """the core requirement."""
    a, b = boxes
    a.memory.write_text("- fact 1\n- fact 2\n")
    b.memory.write_text("- fact 1\n- fact 2\n")
    a.engine.sync()
    b.engine.sync()   # b now has base
    a.memory.write_text("- fact 1\n- fact 2\n- fact A\n")
    b.memory.write_text("- fact 1\n- fact 2\n- fact B\n")
    a.engine.sync()
    b.engine.sync()
    a.engine.sync()
    a_txt = a.memory.read_text()
    b_txt = b.memory.read_text()
    for txt in (a_txt, b_txt):
        assert "fact 1" in txt and "fact 2" in txt
        assert "fact A" in txt and "fact B" in txt
    assert a_txt == b_txt


def test_same_line_conflict_sidecar(boxes):
    """same line edited differently → sidecar, nothing lost."""
    a, b = boxes
    a.memory.write_text("status: green\n")
    b.memory.write_text("status: green\n")
    a.engine.sync()
    b.engine.sync()
    a.memory.write_text("status: red\n")
    b.memory.write_text("status: blue\n")
    a.engine.sync()
    summary = b.engine.sync()
    assert summary.conflicts >= 1
    txt = b.memory.read_text()
    assert "red" in txt and "blue" in txt
    sidecars = list(b.memory.parent.glob("*.conflict"))
    assert sidecars


def test_env_key_union(boxes):
    """new .env key on each side → both present."""
    a, b = boxes
    pa = a.make_project()
    pb = b.make_project()
    (pa / ".env").write_text("SHARED=1\n")
    (pb / ".env").write_text("SHARED=1\n")
    a.engine.sync()
    b.engine.sync()
    (pa / ".env").write_text("SHARED=1\nFROM_A=a\n")
    (pb / ".env").write_text("SHARED=1\nFROM_B=b\n")
    a.engine.sync()
    b.engine.sync()
    a.engine.sync()
    for p in (pa, pb):
        txt = (p / ".env").read_text()
        assert "SHARED=1" in txt and "FROM_A=a" in txt and "FROM_B=b" in txt


def test_rm_tombstone_trash_restore(boxes):
    """devsync rm → trash on the other box, restorable."""
    a, b = boxes
    a.memory.write_text("m\n")
    b.memory.write_text("m\n")
    a.engine.sync()
    b.engine.sync()
    canon = "claude/memory/CascadeProjects/MEMORY.md"
    a.engine.rm(canon)
    a.engine.sync()
    b.engine.sync()
    assert not b.memory.exists()
    trashed = list((b.state / "trash").rglob("MEMORY.md"))
    assert trashed and trashed[0].read_text() == "m\n"
    b.engine.restore(canon)
    assert b.memory.exists() and b.memory.read_text() == "m\n"


def test_one_sided_file_propagates_never_deleted(boxes):
    """file existing only on A appears on B; absence ≠ delete."""
    a, b = boxes
    skill = a.machine.claude_home / "skills" / "myskill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("only on A\n")
    a.engine.sync()
    b.engine.sync()
    b_skill = b.machine.claude_home / "skills" / "myskill" / "SKILL.md"
    assert b_skill.read_text() == "only on A\n"
    # deleting locally without a tombstone → resurrected on next sync
    b_skill.unlink()
    b.engine.sync()
    assert b_skill.exists()


def test_binary_newer_wins_loser_in_trash(boxes):
    a, b = boxes
    pa = a.make_project()
    pb = b.make_project()
    (pa / "data.sqlite").write_bytes(b"v1")
    a.engine.sync()
    b.engine.sync()
    assert (pb / "data.sqlite").read_bytes() == b"v1"
    # both change; A syncs first, B's copy is newer (written later)
    (pa / "data.sqlite").write_bytes(b"from-A")
    a.engine.sync()
    import time
    time.sleep(0.05)
    (pb / "data.sqlite").write_bytes(b"from-B")
    b.engine.sync()
    a.engine.sync()
    assert (pa / "data.sqlite").read_bytes() == b"from-B"
    # A's overwritten copy preserved in trash somewhere
    trashed = list((a.state / "trash").rglob("data.sqlite"))
    assert any(t.read_bytes() == b"from-A" for t in trashed)


def test_sync_is_idempotent(boxes):
    a, _ = boxes
    a.memory.write_text("x\n")
    a.engine.sync()
    s2 = a.engine.sync()
    assert s2.merged == 0 and s2.conflicts == 0 and s2.pushed is False


def test_push_race_converges_without_loss(boxes, monkeypatch):
    """A commit lands on the remote AFTER this sync's pull but BEFORE its push
    (the two-machines-syncing-at-once race). _push_with_retry must pull,
    auto-resolve, and re-push — keeping BOTH sides' edits."""
    a, b = boxes
    a.memory.write_text("- shared\n")
    b.memory.write_text("- shared\n")
    a.engine.sync()
    b.engine.sync()

    a.memory.write_text("- shared\n- from A\n")
    b.memory.write_text("- shared\n- from B\n")

    # Interleave: when A pushes for the first time, B's sync has already
    # landed on the remote. Simulate by running B's full sync from inside
    # A's first push attempt.
    real_git = a.engine._git
    raced = {"done": False}

    def racing_git(*args, check=True):
        if args[0] == "push" and not raced["done"]:
            raced["done"] = True
            b.engine.sync()          # B wins the race to the remote
        return real_git(*args, check=check)

    monkeypatch.setattr(a.engine, "_git", racing_git)
    summary = a.engine.sync()
    monkeypatch.setattr(a.engine, "_git", real_git)
    assert summary.pushed

    b.engine.sync()
    a.engine.sync()
    a_txt = a.memory.read_text()
    b_txt = b.memory.read_text()
    for txt in (a_txt, b_txt):
        assert "from A" in txt and "from B" in txt and "shared" in txt
    assert a_txt == b_txt
    # and the remote agrees
    r = subprocess.run(["git", "-C", str(a.engine.store_dir), "rev-parse",
                        "HEAD", "origin/main"], capture_output=True, text=True)
    head, remote = r.stdout.split()
    assert head == remote


def test_kill_mid_sync_recovers(boxes, monkeypatch):
    """a sync that dies partway must leave the next run
    able to converge — lock reclaimable, no lost content, no stuck repo state."""
    a, b = boxes
    a.memory.write_text("- m1\n")
    skill = a.machine.claude_home / "skills" / "s1" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("- s1\n")
    a.engine.sync()
    b.engine.sync()

    a.memory.write_text("- m1\n- m2\n")
    skill.write_text("- s1\n- s2\n")

    # Crash after the FIRST file reconcile (some bases advanced, some not,
    # nothing committed/pushed, lock left behind).
    calls = {"n": 0}
    real = type(a.engine)._reconcile_one

    def crashing(self, canon, managed, manifest, summary):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("simulated kill")
        return real(self, canon, managed, manifest, summary)

    monkeypatch.setattr(type(a.engine), "_reconcile_one", crashing)
    with pytest.raises(KeyboardInterrupt):
        a.engine.sync()
    monkeypatch.setattr(type(a.engine), "_reconcile_one", real)

    # A KeyboardInterrupt still runs the finally-release; a real kill -9 does
    # not. Simulate the hard-kill aftermath explicitly: a leftover lock whose
    # pid no longer exists. Sync must reclaim it, not refuse.
    lock = a.state / "devsync.lock"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "pid").write_text("999999999")

    summary = a.engine.sync()   # must reclaim lock and converge
    assert summary.pushed
    b.engine.sync()
    assert "m2" in (b.machine.claude_home / "projects" /
                    b.machine.project_key_prefix / "memory" / "MEMORY.md").read_text()
    assert "s2" in (b.machine.claude_home / "skills" / "s1" / "SKILL.md").read_text()


def test_lock_prevents_overlap(boxes):
    a, _ = boxes
    a.state.mkdir(exist_ok=True)
    lock = a.state / "devsync.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999999")
    import os
    (lock / "pid").write_text(str(os.getpid()))  # live pid → must refuse
    with pytest.raises(RuntimeError):
        a.engine.sync()


# ── heartbeat stamp ───────────────────────────────────────────────
# The store's dead-man switch derives each machine's last-seen time from
# 'sync <MACHINE>' commit subjects. A sync that only PULLS changes nothing in
# the store, so it commits nothing, and a faithfully-syncing machine is
# indistinguishable from a dead one.

def _sync_commits(box, name):
    out = subprocess.run(["git", "log", "--format=%s"], cwd=box.engine.store_dir,
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l.startswith(f"sync {name} ")]


def test_pull_only_sync_still_signals_heartbeat(boxes):
    """A machine that only consumes must still leave a per-machine signal."""
    a, b = boxes
    a.memory.write_text("- fact 1\n")
    a.engine.sync()

    assert not _sync_commits(b, "PC-B"), "precondition: B has never synced"
    summary = b.engine.sync()

    # B genuinely had nothing of its own to contribute...
    assert summary.merged >= 1, "B should have pulled A's file"
    # ...but must still be visible to the dead-man switch.
    assert _sync_commits(b, "PC-B"), \
        "pull-only sync left no 'sync PC-B' commit — heartbeat cannot see it"


def test_heartbeat_stamp_not_rewritten_within_interval(boxes):
    """At most one stamp per machine per interval — no commit flood."""
    a, b = boxes
    a.memory.write_text("- fact 1\n")
    a.engine.sync()
    b.engine.sync()
    before = len(_sync_commits(b, "PC-B"))
    assert before == 1

    b.engine.sync()
    b.engine.sync()
    assert len(_sync_commits(b, "PC-B")) == before, \
        "a fresh stamp was rewritten — 4-hourly automation would flood the store"


def test_heartbeat_stamp_is_not_synced_as_content(boxes):
    """The stamp lives outside repos/ and claude/ — never mapped to a live path."""
    a, b = boxes
    a.memory.write_text("- fact 1\n")
    a.engine.sync()
    b.engine.sync()

    stamp = b.engine.store_dir / ".heartbeat" / "PC-B.json"
    assert stamp.is_file(), "stamp not written"
    assert json.loads(stamp.read_text())["machine"] == "PC-B"

    a.engine.sync()
    # A must not materialise B's stamp anywhere in its live tree.
    strays = list(a.machine.cascade_root.rglob(".heartbeat")) + \
             list(a.machine.claude_home.rglob(".heartbeat"))
    assert not strays, f"stamp leaked into a live tree: {strays}"


def _conflicted_box(boxes):
    """Drive both boxes to a genuine same-line conflict on B and return B."""
    a, b = boxes
    a.memory.write_text("status: green\n")
    b.memory.write_text("status: green\n")
    a.engine.sync()
    b.engine.sync()          # b now has a base
    a.memory.write_text("status: red\n")
    b.memory.write_text("status: blue\n")
    a.engine.sync()          # a publishes 'red'
    return b


def _base_memory(box):
    hits = list((box.state / "base").rglob("MEMORY.md"))
    return hits[0] if hits else None


def test_conflict_does_not_poison_base(boxes):
    """A conflicted merge must NOT become the recorded ancestor.

    Regression: writing marker text to ~/.devsync/base made the corruption
    the agreed base, so every later run truthfully reported conflicts=0 and
    the file stayed corrupt.
    """
    b = _conflicted_box(boxes)
    summary = b.engine.sync()
    assert summary.conflicts >= 1, "expected a real conflict"
    base = _base_memory(b)
    if base is not None:
        txt = base.read_text(encoding="utf-8")
        assert "<<<<<<<" not in txt and ">>>>>>>" not in txt, \
            "base was poisoned with conflict markers"


def test_conflict_withholds_push(boxes):
    """Conflicted content must not be pushed to the shared store.

    Regression: both corrupting runs logged conflicts=1 pushed=yes, so the
    markers reached the other machine before any human saw the sidecar.
    """
    b = _conflicted_box(boxes)
    summary = b.engine.sync()
    assert summary.conflicts >= 1, "expected a real conflict"
    assert summary.pushed is False, "conflicted run pushed to the shared store"


def _remote_has_local_head(box) -> bool:
    store = box.engine.store_dir
    _git(store, "fetch", "origin", "main")
    r = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"],
                       cwd=store, check=True, capture_output=True, text=True)
    return r.stdout.strip() == "0"


def test_resolved_conflict_publishes_withheld_commit_on_rerun(boxes):
    """The sidecar says 'resolve, then re-run to push' -- the re-run must push.

    Regression: the human accepted the merged files as
    they stood and deleted the sidecars. The re-run had nothing new to commit,
    so the push branch never ran and the withheld commit stayed local.
    """
    b = _conflicted_box(boxes)
    assert b.engine.sync().pushed is False
    for sidecar in b.engine.conflicts():
        sidecar.unlink()
    summary = b.engine.sync()
    assert summary.pushed is True, "resolved re-run left the withheld commit local"
    assert _remote_has_local_head(b)


def test_unresolved_sidecar_still_withholds_on_rerun(boxes):
    """Re-running WITHOUT resolving must not publish the withheld commit.

    The conflicted run keeps the old base, so the next 3-way merge sees
    live == store and truthfully reports conflicts=0. Only the sidecar on
    disk says a human has not looked yet.
    """
    b = _conflicted_box(boxes)
    b.engine.sync()
    assert b.engine.conflicts(), "expected an unresolved sidecar"
    summary = b.engine.sync()
    assert summary.pushed is False, "unresolved conflict was pushed on re-run"
    assert not _remote_has_local_head(b)


def test_unresolved_sidecar_withholds_even_with_new_changes(boxes):
    """An unrelated edit on the re-run must not carry the conflicted commit out."""
    b = _conflicted_box(boxes)
    b.engine.sync()
    proj = b.make_project()
    (proj / ".env").write_text("NEW=1\n")
    summary = b.engine.sync()
    assert summary.pushed is False, "new change smuggled the conflict out"
    assert not _remote_has_local_head(b)


def test_blocklisted_claude_file_is_never_reconciled(boxes):
    """Per-machine state (Claude's skills/synced manifest) must not be merged.

    Regression: the blocklist was only applied to repos/, so each PC's
    server-refreshed manifest.json conflicted with every other PC's. A copy
    already in the store (from before the blocklist) must not be written back
    over a live file either.
    """
    a, b = boxes
    rel = Path("skills") / "synced" / "abc_def" / "manifest.json"
    canon = "claude/skills/synced/abc_def/manifest.json"
    # legacy: the store already carries a copy from before the blocklist
    legacy = a.engine.store_dir / canon
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"lastUpdated": 1}\n')
    manifest = dict(MANIFEST, blocklistGlobs=MANIFEST["blocklistGlobs"] + [
        "claude/skills/synced/*/manifest.json"])
    (a.engine.store_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _git(a.engine.store_dir, "add", "-A")
    _git(a.engine.store_dir, "commit", "-m", "blocklist + legacy copy")
    _git(a.engine.store_dir, "push", "origin", "HEAD:main")

    for box, stamp in ((a, 2), (b, 3)):
        live = box.machine.claude_home / rel
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(f'{{"lastUpdated": {stamp}}}\n')
    for box in (a, b, a, b):
        assert box.engine.sync().conflicts == 0
    # each machine keeps its own copy; the store copy is left untouched
    assert (a.machine.claude_home / rel).read_text() == '{"lastUpdated": 2}\n'
    assert (b.machine.claude_home / rel).read_text() == '{"lastUpdated": 3}\n'
    assert (b.engine.store_dir / canon).read_text() == '{"lastUpdated": 1}\n'
    assert not list((b.machine.claude_home / rel.parent).glob("*.conflict"))
