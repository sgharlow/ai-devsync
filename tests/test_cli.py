"""CLI surface tests. Drives devsync.cli.main() in-process."""
import json
import subprocess
from pathlib import Path

import pytest

from devsync.cli import main


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Simulated machine: HOME, cascade root, claude home, bare remote."""
    home = tmp_path / "home"
    (home / "CascadeProjects").mkdir(parents=True)
    prefix = "C--Users-Test-CascadeProjects"
    (home / ".claude" / "projects" / prefix / "memory").mkdir(parents=True)
    (home / ".claude" / "projects" / prefix / "memory" / "MEMORY.md").write_text("m\n")
    (home / ".claude" / "CLAUDE.md").write_text("global\n")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))
    # These tests pin the layout of the stores that already exist in the field:
    # memory keyed "CascadeProjects". A store seeded today defaults to "projects"
    # (covered in test_adoption.py); the env var is the supported way to choose.
    monkeypatch.setenv("DEVSYNC_MEMORY_KEY_ROOT", "CascadeProjects")

    # 🔴 Without these, four of these tests cannot run at all.
    #
    # The two lines above are the whole cause: git finds its GLOBAL config via
    # HOME (and USERPROFILE on Windows), and both now point at an empty tmp_path.
    # So the developer's real identity is invisible, `git commit` fails with
    # "Please tell me who you are", and engine._git turns that into
    # `RuntimeError: git commit -m sync TEST-PC … failed`.
    #
    # The cost was not four red lines — it was that `test_conflicts_empty_and_rm_restore`
    # is the ONLY coverage of `devsync rm`/`restore`, so nothing exercised them on
    # this machine. A canonical-path bug in `rm` shipped and was found by hand,
    # three syncs deep, rather than by this suite.
    #
    # Env vars rather than a written .gitconfig: git reads these regardless of where
    # config discovery points, so the fix cannot be re-broken by another HOME change.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "devsync tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tests@devsync.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "devsync tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tests@devsync.invalid")
    return home, remote


def test_init_seed_creates_store_and_machine_json(home, capsys):
    h, remote = home
    rc = main(["init", "--seed",
               "--remote", str(remote),
               "--cascade-root", str(h / "CascadeProjects"),
               "--claude-home", str(h / ".claude"),
               "--hostname", "TEST-PC",
               "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    assert rc == 0
    mj = json.loads((h / ".devsync" / "machine.json").read_text())
    assert mj["hostname"] == "TEST-PC"
    store = Path(mj["storeDir"])
    assert (store / "manifest.json").is_file()
    assert (store / "claude" / "global" / "CLAUDE.md").is_file()
    assert (store / "claude" / "memory" / "CascadeProjects" / "MEMORY.md").is_file()
    # base seeded
    assert (h / ".devsync" / "base" / "claude" / "global" / "CLAUDE.md").is_file()


def test_sync_and_status_after_init(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    (h / ".claude" / "CLAUDE.md").write_text("global\nnew line\n")
    assert main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "merged=1" in out
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "TEST-PC" in out


def test_sync_exits_2_while_an_old_sidecar_is_unresolved(home, capsys):
    """A sidecar left from an earlier run withholds the push, so sync must say so.

    The wrappers log CONFLICT only on exit 2. Keyed on this run's count alone,
    a machine holding an old sidecar would log OK pushed=no forever.
    """
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    sidecar = h / ".claude" / "CLAUDE.md.conflict"
    sidecar.write_text("devsync conflict on claude/global/CLAUDE.md\n")
    (h / ".claude" / "CLAUDE.md").write_text("global\nedited\n")
    assert main(["sync"]) == 2
    assert "push withheld" in capsys.readouterr().out
    sidecar.unlink()
    assert main(["sync"]) == 0
    assert "pushed=yes" in capsys.readouterr().out


def test_adopt_on_second_machine(home, tmp_path, monkeypatch, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "PC-A",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])

    h2 = tmp_path / "home2"
    prefix2 = "C--Users-Other-CascadeProjects"
    (h2 / "CascadeProjects").mkdir(parents=True)
    (h2 / ".claude" / "projects" / prefix2 / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h2))
    monkeypatch.setenv("USERPROFILE", str(h2))
    monkeypatch.setenv("DEVSYNC_HOME", str(h2 / ".devsync"))
    rc = main(["adopt", "--remote", str(remote),
               "--cascade-root", str(h2 / "CascadeProjects"),
               "--claude-home", str(h2 / ".claude"),
               "--hostname", "PC-B",
               "--project-key-prefix", prefix2])
    assert rc == 0
    assert (h2 / ".claude" / "CLAUDE.md").read_text() == "global\n"
    assert (h2 / ".claude" / "projects" / prefix2 / "memory" / "MEMORY.md").is_file()


def test_conflicts_empty_and_rm_restore(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    assert main(["conflicts"]) == 0
    canon = "claude/global/CLAUDE.md"
    assert main(["rm", canon]) == 0
    assert not (h / ".claude" / "CLAUDE.md").exists()
    assert main(["restore", canon]) == 0
    assert (h / ".claude" / "CLAUDE.md").read_text() == "global\n"


# ── `rm` / `restore` accept the path a human actually types ────────────────────
#
# 🔴 Found by hand, three syncs deep. `devsync rm <ABSOLUTE PATH>`
# printed "tombstoned ... (recoverable via `devsync restore`)" and did none of it:
# the file came back on the next sync, every time.
#
# `Engine.rm(canon)` wants a CANONICAL store-relative path (`claude/lib/x.md`).
# The CLI passed `args.path` straight through, so an absolute path became the
# tombstone's `path`, and four things failed silently:
#
#   1. `tomb_paths` held the absolute string, which never matches a canonical
#      candidate, so the file was NOT excluded from the merge -> re-added.
#   2. `store_dir / canon` -> pathlib REPLACES the base when the right side is
#      absolute, so it pointed outside the store and the store copy survived.
#   3. `base_dir / canon` -> same escape.
#   4. `canonical_to_local()` raised ValueError into a bare `except: pass`, so
#      the live file was never trashed and never deleted -- which also made the
#      advertised `devsync restore` recovery a lie, since nothing was in trash.
#
# The old test only ever passed a canonical path, so it could not see any of it.
# These tests drive the absolute path, because that is what every listing, error
# message and shell completion hands you.

def test_rm_accepts_an_absolute_path_and_the_file_stays_deleted(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    live = h / ".claude" / "CLAUDE.md"
    assert live.exists()

    assert main(["rm", str(live)]) == 0
    assert not live.exists(), "rm must delete the live file it says it tombstoned"

    # The regression: the next sync used to bring it straight back.
    assert main(["sync"]) == 0
    assert not live.exists(), "a tombstoned file must not return on the next sync"


def test_rm_by_absolute_path_records_a_canonical_tombstone(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    main(["rm", str(h / ".claude" / "CLAUDE.md")])

    tombs = json.loads((h / "CascadeProjects" / "devsync-store" / "tombstones.json").read_text())
    paths = [t["path"] for t in tombs]
    assert "claude/global/CLAUDE.md" in paths, f"expected a canonical tombstone, got {paths}"
    # No absolute path may ever reach the store: another machine cannot act on it.
    for p in paths:
        assert not (len(p) > 1 and p[1] == ":"), f"absolute path in tombstones.json: {p}"
        assert not p.startswith("/"), f"absolute path in tombstones.json: {p}"


def test_rm_by_absolute_path_puts_the_file_in_trash_so_restore_works(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    live = h / ".claude" / "CLAUDE.md"
    main(["rm", str(live)])
    # `rm` promises "recoverable via devsync restore" — hold it to that.
    assert main(["restore", str(live)]) == 0
    assert live.read_text() == "global\n"


def test_rm_refuses_a_path_outside_every_managed_root(home, capsys):
    h, remote = home
    main(["init", "--seed", "--remote", str(remote),
          "--cascade-root", str(h / "CascadeProjects"),
          "--claude-home", str(h / ".claude"),
          "--hostname", "TEST-PC",
          "--project-key-prefix", "C--Users-Test-CascadeProjects"])
    outside = h / "not-managed.md"
    outside.write_text("x\n")
    # Fail loudly. Silently tombstoning an unmanaged path is how this bug read as success.
    assert main(["rm", str(outside)]) != 0
    assert outside.exists(), "a refused rm must not delete anything"


# ── `adopt --follower`: join taking the store's state, publishing nothing ──────
#
# 🔴 A plain `adopt` is NOT a union. On first contact the engine uses the store
# copy as the merge base, so base == repo and every local difference reads as a
# local EDIT: the joining PC's copy wins and is pushed to the fleet. A joining
# machine's short MEMORY.md would replace the shared index, and its settings.json
# everyone's settings.

PREFIX_A = "C--Users-Test-CascadeProjects"
PREFIX_B = "C--Users-Other-CascadeProjects"


def _seed_pc_a(h, remote):
    (h / ".claude" / "settings.json").write_text('{"hooks": {"Stop": []}}\n')
    assert main(["init", "--seed", "--remote", str(remote),
                 "--cascade-root", str(h / "CascadeProjects"),
                 "--claude-home", str(h / ".claude"),
                 "--hostname", "PC-A",
                 "--project-key-prefix", PREFIX_A]) == 0


def _pc_b(tmp_path, monkeypatch):
    """A joining PC whose config diverges from the store, plus a local-only memory."""
    h2 = tmp_path / "home2"
    mem = h2 / ".claude" / "projects" / PREFIX_B / "memory"
    (h2 / "CascadeProjects").mkdir(parents=True)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("b-only index\n")
    (mem / "local-only.md").write_text("written on B\n")
    (h2 / ".claude" / "CLAUDE.md").write_text("B's global\n")
    (h2 / ".claude" / "settings.json").write_text('{"theme": "dark"}\n')
    monkeypatch.setenv("HOME", str(h2))
    monkeypatch.setenv("USERPROFILE", str(h2))
    monkeypatch.setenv("DEVSYNC_HOME", str(h2 / ".devsync"))
    return h2, mem


def _adopt_b(h2, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote),
                 "--cascade-root", str(h2 / "CascadeProjects"),
                 "--claude-home", str(h2 / ".claude"),
                 "--hostname", "PC-B",
                 "--project-key-prefix", PREFIX_B])


def _remote_file(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _remote_head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def test_merge_adopt_publishes_the_joining_pcs_divergent_config(home, tmp_path, monkeypatch):
    """Characterizes the hazard --follower exists for: on first contact this PC's
    differences win and are pushed. Until 0.2.0 a PLAIN adopt did this; it now has
    to be asked for with --merge (the refusal is covered in test_adoption.py)."""
    h, remote = home
    _seed_pc_a(h, remote)
    h2, _ = _pc_b(tmp_path, monkeypatch)
    assert _adopt_b(h2, remote, "--merge") == 0
    assert _remote_file(remote, "claude/global/CLAUDE.md") == "B's global\n"


def test_follower_takes_the_store_state_and_publishes_nothing_local(
        home, tmp_path, monkeypatch, capsys):
    h, remote = home
    _seed_pc_a(h, remote)
    h2, mem = _pc_b(tmp_path, monkeypatch)

    assert _adopt_b(h2, remote, "--follower") == 0

    # this PC now holds exactly the store's state
    assert (h2 / ".claude" / "CLAUDE.md").read_text() == "global\n"
    assert (mem / "MEMORY.md").read_text() == "m\n"
    assert json.loads((h2 / ".claude" / "settings.json").read_text()) == {"hooks": {"Stop": []}}
    assert not (mem / "local-only.md").exists()

    # the fleet is untouched: store content unchanged, local-only never published
    assert _remote_file(remote, "claude/global/CLAUDE.md") == "global\n"
    assert _remote_file(remote, "claude/memory/CascadeProjects/MEMORY.md") == "m\n"
    assert _remote_file(remote, "claude/memory/CascadeProjects/local-only.md") is None
    # ...and the one thing pushed is PC-B's heartbeat, so the dead-man switch sees it
    changed = subprocess.run(
        ["git", "--git-dir", str(remote), "diff", "--name-only", "main~1", "main"],
        capture_output=True, text=True).stdout.split()
    assert changed == [".heartbeat/PC-B.json"]

    # a second sync is converged
    assert main(["sync"]) == 0
    assert "merged=0" in capsys.readouterr().out


def test_follower_set_aside_is_listed_and_restorable(home, tmp_path, monkeypatch):
    h, remote = home
    _seed_pc_a(h, remote)
    h2, mem = _pc_b(tmp_path, monkeypatch)
    assert _adopt_b(h2, remote, "--follower") == 0

    listing = json.loads((h2 / ".devsync" / "follower-set-aside.json").read_text())
    assert "claude/memory/CascadeProjects/local-only.md" in listing["paths"]
    assert "claude/global/settings.json" in listing["paths"]

    assert main(["restore", "claude/memory/CascadeProjects/local-only.md"]) == 0
    assert (mem / "local-only.md").read_text() == "written on B\n"


def test_follower_refuses_after_first_contact(home, tmp_path, monkeypatch, capsys):
    h, remote = home
    _seed_pc_a(h, remote)
    h2, mem = _pc_b(tmp_path, monkeypatch)
    assert _adopt_b(h2, remote, "--follower") == 0
    (mem / "later-edit.md").write_text("real local work\n")

    assert _adopt_b(h2, remote, "--follower") == 1
    assert "already synced" in capsys.readouterr().err
    assert (mem / "later-edit.md").read_text() == "real local work\n"


def test_follower_refuses_an_empty_store(home, tmp_path, monkeypatch, capsys):
    _, remote = home   # bare remote, never seeded
    h2, mem = _pc_b(tmp_path, monkeypatch)

    assert _adopt_b(h2, remote, "--follower") == 1
    assert "no managed files" in capsys.readouterr().err
    assert (mem / "local-only.md").read_text() == "written on B\n"
    assert (h2 / ".claude" / "CLAUDE.md").read_text() == "B's global\n"


def test_follower_withholds_push_if_local_content_would_leave(
        home, tmp_path, monkeypatch, capsys):
    """Defence in depth: if set-aside ever misses a file, the push gate catches it."""
    from devsync.engine import Engine
    h, remote = home
    _seed_pc_a(h, remote)
    h2, mem = _pc_b(tmp_path, monkeypatch)
    monkeypatch.setattr(Engine, "set_aside_local",
                        lambda self: ([], self.trash_dir / "none"))
    before = _remote_head(remote)

    assert _adopt_b(h2, remote, "--follower") == 1
    assert "push WITHHELD" in capsys.readouterr().err
    assert _remote_head(remote) == before
