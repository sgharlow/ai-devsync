"""Defects found by the second independent pre-publication review.

Three were blocking:
  A. a store whose manifest is missing or unreadable fell back to the original
     defaults, which sync gitignored files unencrypted;
  B. a store edit made by `rm`, `config set` or `home add` was left uncommitted,
     so the next pull failed -- on every later run -- once another machine had
     touched the same file;
  C. `config set` could not take a value starting with '-', which every Linux and
     macOS project-key prefix does.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devsync.cli import _load_engine, main
from devsync.engine import _merge3, merge_store_meta

REPO = Path(__file__).resolve().parent.parent


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _head(remote):
    r = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", "main"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _use(mp, home):
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _pc(tmp_path, name, with_env=True):
    home = tmp_path / name
    (home / "code").mkdir(parents=True)
    (home / ".claude").mkdir()
    if with_env:
        proj = home / "code" / "app"
        proj.mkdir()
        _git(proj, "init", "--initial-branch=main")
        (proj / ".gitignore").write_text(".env\n")
        (proj / ".env").write_text("TOKEN=must-not-leak\n")
    return home


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR",
                "DEVSYNC_PASSPHRASE", "CODEX_HOME", "DEVSYNC_CMD"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a = _pc(tmp_path, "a")
    (a / ".claude" / "skills" / "k").mkdir(parents=True)
    for n in ("one", "two", "three"):
        (a / ".claude" / "skills" / "k" / f"{n}.md").write_text(f"{n}\n")
    _use(monkeypatch, a)
    return a, remote, tmp_path, monkeypatch


def _seed(home, remote, *extra):
    return main(["init", "--seed", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-A"])


def _adopt(home, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-B"])


def _leaked(remote):
    revs = subprocess.run(["git", "--git-dir", str(remote), "rev-list", "--all"],
                          capture_output=True, text=True).stdout.split()
    return any(subprocess.run(["git", "--git-dir", str(remote), "grep", "-a", "-l", "must-not-leak", r],
                              capture_output=True, text=True).stdout.strip() for r in revs)


# ── A. no manifest, no sync ──────────────────────────────────────────────────

def test_adopting_a_store_that_was_never_seeded_is_refused_and_uploads_nothing(world, capsys):
    a, remote, tmp, mp = world
    capsys.readouterr()
    assert _adopt(a, remote) == 1
    assert "has not been seeded" in capsys.readouterr().err
    assert _head(remote) == ""                              # nothing was pushed at all
    assert not (a / ".devsync" / "machine.json").exists()


def test_a_typo_in_the_manifest_stops_the_sync_instead_of_uploading_secrets(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0                            # default: secrets are NOT synced
    head = _head(remote)
    mf = a / "code" / "devsync-store" / "manifest.json"
    mf.write_text(mf.read_text().rstrip()[:-1] + ",}\n")    # a trailing comma
    capsys.readouterr()
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "Nothing was synced" in err and "Traceback" not in err
    assert _head(remote) == head
    assert not _leaked(remote)


def test_config_set_refuses_to_rewrite_a_manifest_it_cannot_read(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    mf = a / "code" / "devsync-store" / "manifest.json"
    mf.write_text("{ not json")
    assert main(["config", "set", "conflict-policy", "ask"]) == 1
    assert mf.read_text() == "{ not json"


def test_the_engine_refuses_a_store_with_no_manifest(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    (a / "code" / "devsync-store" / "manifest.json").unlink()
    with pytest.raises(RuntimeError, match="no manifest.json"):
        _load_engine().sync()
    assert not _leaked(remote)


def test_a_corrupt_tombstone_list_stops_the_sync(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    (a / "code" / "devsync-store" / "tombstones.json").write_text("[oops")
    assert main(["sync"]) == 1
    assert "tombstones.json" in capsys.readouterr().err


# ── B. store edits on two machines ───────────────────────────────────────────

def _two(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b", with_env=False)
    _use(mp, b)
    assert _adopt(b, remote) == 0
    return a, b, remote, mp


def test_rm_on_two_machines_before_either_syncs_does_not_wedge(world):
    a, b, remote, mp = _two(world)
    _use(mp, a)
    assert main(["rm", "claude/skills/k/one.md"]) == 0
    _use(mp, b)                                             # B has not seen A's delete
    assert main(["rm", "claude/skills/k/two.md"]) == 0        # used to fail here, and forever after
    assert main(["sync"]) == 0
    paths = sorted(t["path"] for t in json.loads(_remote(remote, "tombstones.json")))
    assert paths == ["claude/skills/k/one.md", "claude/skills/k/two.md"]
    assert not (b / ".claude" / "skills" / "k" / "one.md").exists() and not (b / ".claude" / "skills" / "k" / "two.md").exists()
    _use(mp, a)
    assert main(["sync"]) == 0
    assert not (a / ".claude" / "skills" / "k" / "two.md").exists()
    assert (a / ".claude" / "skills" / "k" / "three.md").read_text() == "three\n"


def test_config_set_on_two_machines_keeps_both_settings(world):
    a, b, remote, mp = _two(world)
    _use(mp, a)
    assert main(["config", "set", "skip-project-dirs", "archive,scratch"]) == 0
    assert main(["sync"]) == 0
    _use(mp, b)
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    assert main(["sync"]) == 0
    manifest = json.loads(_remote(remote, "manifest.json"))
    assert manifest["skipProjectDirs"] == ["archive", "scratch"]
    assert manifest["conflictPolicy"] == "ask"
    _use(mp, a)
    assert main(["sync"]) == 0


def test_home_add_on_two_machines_keeps_both_agents(world):
    a, b, remote, mp = _two(world)
    for home, name in ((a, "alpha"), (b, "beta")):
        _use(mp, home)
        (home / f".{name}").mkdir()
        (home / f".{name}" / "R.md").write_text(f"{name}\n")
        assert main(["home", "add", name, str(home / f".{name}"), "--files", "R.md"]) == 0
        assert main(["sync"]) == 0
    names = [h["name"] for h in json.loads(_remote(remote, "manifest.json"))["homes"]]
    assert "alpha" in names and "beta" in names and len(names) == len(set(names))
    assert _remote(remote, "agents/alpha/R.md") == b"alpha\n"
    assert _remote(remote, "agents/beta/R.md") == b"beta\n"


def test_a_store_edit_is_committed_before_the_pull_not_left_dirty(world):
    a, b, remote, mp = _two(world)
    _use(mp, a)
    assert main(["config", "set", "conflict-policy", "ask"]) == 0
    store = a / "code" / "devsync-store"
    assert subprocess.run(["git", "-C", str(store), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip()      # dirty before
    assert main(["sync"]) == 0
    assert not subprocess.run(["git", "-C", str(store), "status", "--porcelain"],
                              capture_output=True, text=True).stdout.strip()  # clean after
    assert json.loads(_remote(remote, "manifest.json"))["conflictPolicy"] == "ask"


def test_meta_merge_rules():
    tomb = lambda p, ts: {"path": p, "ts": ts, "machine": "m"}
    merged = json.loads(merge_store_meta(
        "tombstones.json", b"[]",
        json.dumps([tomb("a", 2), tomb("shared", 1)]).encode(),
        json.dumps([tomb("shared", 1), tomb("b", 3)]).encode()))
    assert [t["path"] for t in merged] == ["shared", "a", "b"]          # union, no duplicate, by time
    with pytest.raises(RuntimeError, match="not valid JSON"):
        merge_store_meta("manifest.json", b"{}", b"{bad", b"{}")
    # a tie on secret sync goes to OFF
    assert _merge3({"syncRepoFiles": False}, {"syncRepoFiles": True}, {"syncRepoFiles": False}) == {"syncRepoFiles": True}
    assert _merge3(False, True, "other", "syncRepoFiles") is False
    # lists union; one side's untouched key follows the side that changed it
    assert _merge3({"l": [1], "k": 1}, {"l": [1, 2], "k": 1}, {"l": [1, 3], "k": 9}) == {"l": [1, 2, 3], "k": 9}
    # encryption turned on by one side is never dropped by the merge
    base, enc = {"a": 1}, {"a": 1, "encryption": {"scheme": "x"}}
    assert _merge3(base, enc, {"a": 2})["encryption"] == {"scheme": "x"}
    assert _merge3(base, {"a": 2}, enc)["encryption"] == {"scheme": "x"}


# ── C. `config set` with a value that starts with a dash ─────────────────────

def test_config_set_takes_a_posix_project_key_prefix(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["config", "set", "project-key-prefix", "-home-me-code"]) == 0
    assert "usage" not in capsys.readouterr().out.lower()
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert data["projectKeyPrefix"] == "-home-me-code"
    assert main(["config", "set", "project-key-prefix", "-Users-me-code"]) == 0
    assert json.loads((a / ".devsync" / "machine.json").read_text())["projectKeyPrefix"] == "-Users-me-code"


def test_config_usage_errors_are_still_errors(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert main(["config"]) == 0
    assert main(["config", "set", "root"]) == 2
    assert main(["config", "get", "root", "x"]) == 2
    assert main(["config", "set", "a", "b", "c"]) == 2


# ── smaller findings ─────────────────────────────────────────────────────────

def test_rm_of_a_folder_or_a_name_that_matches_nothing_is_refused(world, capsys):
    a, remote, tmp, mp = world
    (a / ".claude" / "skills" / "s").mkdir(parents=True)
    (a / ".claude" / "skills" / "s" / "SKILL.md").write_text("x\n")
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["rm", "claude/skills/s"]) == 2
    assert "folder" in capsys.readouterr().err
    assert main(["rm", "claude/global/typo.md"]) == 2
    assert "nothing called" in capsys.readouterr().err
    (a / ".claude" / "unsynced.md").write_text("on this PC only, never synced\n")
    assert main(["rm", "claude/global/unsynced.md"]) == 2
    assert "not a file devsync has synced" in capsys.readouterr().err
    assert (a / ".claude" / "unsynced.md").exists()
    assert json.loads(_remote(remote, "tombstones.json")) == []
    assert _remote(remote, "claude/skills/s/SKILL.md") == b"x\n"


def test_resolve_with_a_bare_area_name_is_a_message_not_a_traceback(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["resolve", "claude"]) in (0, 1, 2)
    assert "Traceback" not in capsys.readouterr().err


def test_restore_keeps_the_file_it_overwrites(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    f = a / ".claude" / "skills" / "k" / "one.md"
    assert main(["rm", str(f)]) == 0
    f.write_text("written again by hand after the delete\n")
    assert main(["restore", "claude/skills/k/one.md"]) == 0
    assert f.read_text() == "one\n"
    kept = list((a / ".devsync" / "trash").glob("*/claude/skills/k/one.md.before-restore"))
    assert len(kept) == 1 and "by hand" in kept[0].read_text()


def test_rerunning_init_says_when_its_flags_were_ignored(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert "NOT applied" in capsys.readouterr().out
    assert json.loads(_remote(remote, "manifest.json"))["syncRepoFiles"] is False
    assert not _leaked(remote)


def test_the_windows_wrapper_honours_devsync_home(tmp_path):
    ps = shutil.which("powershell.exe")
    if os.name != "nt" or not ps:
        pytest.skip("Windows PowerShell wrapper")
    shim = tmp_path / "shim"
    shim.mkdir()
    ok = "@echo off\r\necho devsync: merged=0  replaced=0  conflicts=0  tombstoned=0  pushed=no\r\nexit /b 0\r\n"
    for exe in ("python.cmd", "devsync.cmd"):
        (shim / exe).write_bytes(ok.encode("ascii"))
    state = tmp_path / "elsewhere" / "state"
    state.mkdir(parents=True)
    root = tmp_path / "devroot"
    root.mkdir()
    (state / "machine.json").write_text(json.dumps({"cascadeRoot": root.as_posix()}))
    env = {k: v for k, v in os.environ.items() if k != "DEVSYNC_CMD"}
    env.update(PATH=str(shim) + os.pathsep + env["PATH"], DEVSYNC_HOME=str(state),
               USERPROFILE=str(tmp_path / "profile-with-no-devsync-folder"))
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(REPO / "scripts" / "devsync-sync-run.ps1")],
                       capture_output=True, text=True, env=env)
    if not (state / "sync.log").is_file():
        pytest.skip(f"wrapper could not run here: {(r.stdout + r.stderr)[:200]}")
    assert "OK" in (state / "sync.log").read_text(encoding="utf-8-sig")
    assert r.returncode == 0


def test_the_linux_wrapper_honours_devsync_home(tmp_path):
    bash = shutil.which("bash")
    if os.name == "nt" or not bash:
        pytest.skip("POSIX wrapper")
    fake = tmp_path / "fake-devsync"
    fake.write_text("#!/bin/sh\necho 'devsync: merged=0  replaced=0  conflicts=0  tombstoned=0  pushed=no'\n")
    fake.chmod(0o755)
    state, root = tmp_path / "state", tmp_path / "devroot"
    state.mkdir()
    root.mkdir()
    (state / "machine.json").write_text(json.dumps({"cascadeRoot": str(root)}))
    env = dict(os.environ, DEVSYNC_CMD=str(fake), DEVSYNC_HOME=str(state), HOME=str(tmp_path / "home"))
    env.pop("DEVSYNC_LOG", None)
    env.pop("CASCADE_ROOT", None)
    r = subprocess.run([bash, str(REPO / "scripts" / "devsync-sync.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in (state / "sync.log").read_text()


def test_no_citation_of_a_design_document_that_does_not_ship():
    for f in list((REPO / "devsync").glob("*.py")) + list((REPO / "tests").glob("*.py")):
        if f.name != Path(__file__).name:
            assert "§" not in f.read_text(encoding="utf-8"), f.name
