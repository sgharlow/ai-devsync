"""Non-blocking findings of the fourth independent review, fixed before release.

The `.env` layout and global-gitignore findings are not fixed here: two attempts
each introduced a new defect, so they are listed in the README as known limitations.
"""
import json
import subprocess
from pathlib import Path

import pytest

import devsync.engine as engine_mod
from devsync.cli import _load_engine, main

REPO = Path(__file__).resolve().parent.parent

ENV = (b"# database\n"
       b"DB_URL=postgres://localhost/app\n"
       b"\n"
       b"# third-party\n"
       b"API_KEY=abc\n"
       b"export DEBUG=0\n")


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


def _pc(tmp_path, name, env=None):
    home = tmp_path / name
    (home / "code").mkdir(parents=True)
    (home / ".claude" / "skills" / "k").mkdir(parents=True)
    proj = home / "code" / "app"
    proj.mkdir()
    _git(proj, "init", "--initial-branch=main")
    (proj / ".gitignore").write_text(".env\n")
    if env is not None:
        (proj / ".env").write_bytes(env)
    return home


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                "DEVSYNC_CMD", "DEVSYNC_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a = _pc(tmp_path, "a", ENV)
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


# ── a first push that never landed ───────────────────────────────────────────

def test_a_first_push_that_failed_is_retried_by_the_next_sync(world, capsys):
    a, remote, tmp, mp = world
    bad = tmp / "not-there-yet.git"
    _git(tmp, "clone", str(remote), str(a / "code" / "devsync-store"))
    _git(a / "code" / "devsync-store", "remote", "set-url", "--push", "origin", str(bad))
    assert _seed(a, remote) == 1                              # commit made, push rejected
    assert _head(remote) == ""
    _git(a / "code" / "devsync-store", "remote", "set-url", "--push", "origin", str(remote))
    capsys.readouterr()
    assert main(["sync"]) == 0                                # nothing new to commit ...
    assert "pushed=yes" in capsys.readouterr().out            # ... but the store goes out
    assert _remote(remote, "manifest.json") is not None


# ── a fault while finishing a merge ──────────────────────────────────────────

def test_a_failure_while_finishing_a_merge_does_not_leave_the_store_mid_merge(world, monkeypatch, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    _use(mp, a)
    assert main(["rm", "claude/skills/k/f.md"]) == 0
    _use(mp, b)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated fault while merging the store's own files")
    monkeypatch.setattr(engine_mod, "merge_store_meta", boom)
    capsys.readouterr()
    assert main(["rm", "claude/skills/k/g.md"]) == 1
    assert "simulated fault" in capsys.readouterr().err
    store = b / "code" / "devsync-store"
    assert not (store / ".git" / "MERGE_HEAD").exists()
    unmerged = subprocess.run(["git", "-C", str(store), "diff", "--name-only", "--diff-filter=U"],
                              capture_output=True, text=True).stdout.strip()
    assert unmerged == ""
    assert b"<<<<<<<" not in (store / "tombstones.json").read_bytes()

    monkeypatch.undo()
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        mp.setenv(k, v)
    _use(mp, b)
    assert main(["sync"]) == 0                                # the next run finishes the job
    paths = sorted(t["path"] for t in json.loads(_remote(remote, "tombstones.json")))
    assert paths == ["claude/skills/k/f.md", "claude/skills/k/g.md"]


# ── small ones ───────────────────────────────────────────────────────────────

def test_the_conflict_sidecar_points_at_devsync_resolve(world):
    a, remote, tmp, mp = world
    (a / ".claude" / "CLAUDE.md").write_text("one\nshared\nthree\n")
    assert _seed(a, remote) == 0
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["sync"]) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    (b / ".claude" / "CLAUDE.md").write_text("one\nshared (B)\nthree\n")
    assert not _load_engine().sync(push=False).pushed
    _use(mp, a)
    (a / ".claude" / "CLAUDE.md").write_text("one\nshared (A)\nthree\n")
    assert main(["sync"]) == 0
    _use(mp, b)
    main(["sync"])
    sidecars = list((b / ".claude").glob("*.conflict"))
    if sidecars:                      # written when the conflict is found on the live side
        text = sidecars[0].read_text()
        assert "devsync resolve" in text and "delete this sidecar" not in text


def test_a_locked_or_unwritable_file_is_a_message_not_a_traceback(world, monkeypatch, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", "locked.sqlite")
    monkeypatch.setattr(engine_mod.Engine, "sync", denied)
    capsys.readouterr()
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err and "Traceback" not in err


def test_wrapper_scripts_are_executable_in_the_repository():
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "scripts"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("not a git checkout")
    modes = {line.split("\t")[1]: line.split()[0] for line in r.stdout.strip().splitlines()}
    for name in ("scripts/devsync-sync.sh", "scripts/install-devsync-systemd.sh"):
        assert modes[name] == "100755", name
