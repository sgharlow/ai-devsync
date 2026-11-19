"""Defects found by an independent pre-publication review, each pinned here.

Every test in this file describes something a first-time user could hit that the
earlier suite did not catch: files that uploaded and never came back, a command
that reported success and did nothing, a secret that could reach the store in
plain text, a real fault logged as "offline".
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devsync import crypto
from devsync.cli import _load_engine, main
from devsync.collect import collect_claude_files
from devsync.engine import DEFAULT_MANIFEST
from devsync.merge import reconcile
from devsync.paths import Machine, canonical_to_local, local_to_canonical

REPO = Path(__file__).resolve().parent.parent
PHRASE = "correct horse battery staple"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def _use(mp, home):
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _pc(tmp_path, name):
    home = tmp_path / name
    (home / "code").mkdir(parents=True)
    (home / ".claude").mkdir()
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
    (a / ".claude" / "CLAUDE.md").write_text("notes\n")
    _use(monkeypatch, a)
    return a, remote, tmp_path, monkeypatch


def _seed(home, remote, *extra):
    return main(["init", "--seed", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-A"])


def _adopt(home, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-B"])


def _project(home, name="app", files=None):
    proj = home / "code" / name
    proj.mkdir(parents=True)
    _git(proj, "init", "--initial-branch=main")
    (proj / ".gitignore").write_text(".env\ncerts/\n")
    for rel, data in (files or {".env": b"KEY=1\n"}).items():
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return proj


# ── 1. every synced folder comes back down, not only four of them ────────────

def test_any_folder_under_the_agent_home_maps_both_ways():
    m = Machine(hostname="T", cascade_root=Path("/r"), claude_home=Path("/c"), project_key_prefix="p")
    for tree in ("skills", "agents", "rules", "output-styles", "something-new"):
        canon = f"claude/{tree}/x/y.md"
        assert local_to_canonical(canonical_to_local(canon, m), m) == canon
    assert local_to_canonical(Path("/c/projects/other/session.jsonl"), m) is None
    assert local_to_canonical(Path("/c/CLAUDE.md"), m) == "claude/global/CLAUDE.md"


@pytest.mark.parametrize("how", [(), ("--follower",)])
def test_agents_rules_and_output_styles_reach_the_second_pc(world, how):
    a, remote, tmp, mp = world
    for tree in ("agents", "rules", "output-styles", "skills"):
        (a / ".claude" / tree).mkdir()
        (a / ".claude" / tree / "one.md").write_text(f"{tree} from A\n")
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    if how:
        (b / ".claude" / "agents").mkdir()
        (b / ".claude" / "agents" / "one.md").write_text("B's own, to be set aside\n")
    _use(mp, b)
    assert _adopt(b, remote, *how) == 0
    for tree in ("agents", "rules", "output-styles", "skills"):
        assert (b / ".claude" / tree / "one.md").read_text() == f"{tree} from A\n"


# ── 2. `init --seed` on a second machine ─────────────────────────────────────

def test_seeding_a_store_that_already_has_files_is_refused(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    head = _head(remote)
    b = _pc(tmp, "b")
    (b / ".claude" / "CLAUDE.md").write_text("B would have overwritten everyone\n")
    _use(mp, b)
    capsys.readouterr()
    assert _seed(b, remote) == 1
    assert "devsync adopt" in capsys.readouterr().err
    assert _head(remote) == head
    assert (b / ".claude" / "CLAUDE.md").read_text() == "B would have overwritten everyone\n"
    assert not (b / ".devsync" / "machine.json").exists()


# ── 3. `rm` / `restore` with a relative or home-relative path ────────────────

def test_rm_with_a_relative_path_deletes_the_file_it_names(world, monkeypatch):
    a, remote, tmp, mp = world
    (a / ".claude" / "skills" / "s").mkdir(parents=True)
    (a / ".claude" / "skills" / "s" / "SKILL.md").write_text("skill\n")
    assert _seed(a, remote) == 0
    monkeypatch.chdir(a / ".claude" / "skills")
    assert main(["rm", os.path.join("s", "SKILL.md")]) == 0
    assert _remote(remote, "claude/skills/s/SKILL.md") is None
    assert not (a / ".claude" / "skills" / "s" / "SKILL.md").exists()
    tombs = json.loads(_remote(remote, "tombstones.json"))
    assert [t["path"] for t in tombs] == ["claude/skills/s/SKILL.md"]


def test_rm_of_something_devsync_does_not_manage_is_refused_not_tombstoned(world, monkeypatch, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    stray = tmp / "elsewhere"
    stray.mkdir()
    (stray / ".env").write_text("X=1\n")
    monkeypatch.chdir(stray)
    capsys.readouterr()
    assert main(["rm", ".env"]) == 2
    assert "not under any" in capsys.readouterr().err
    assert json.loads(_remote(remote, "tombstones.json")) == []


def test_rm_accepts_a_tilde_path(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert main(["rm", "~/.claude/CLAUDE.md"]) == 0
    assert _remote(remote, "claude/global/CLAUDE.md") is None


# ── 4. YAML and TOML are not JSON ────────────────────────────────────────────

@pytest.mark.parametrize("policy", ["ask", "newer"])
def test_a_one_sided_toml_or_yaml_edit_is_not_a_conflict(policy):
    for base, edited in ((b'prompt = "a"\n', b'prompt = "b"\n'), (b"key: 1\nother: x\n", b"key: 2\nother: x\n")):
        theirs = reconcile("structured", base, base, edited, ours_newer=True, policy=policy)
        assert not theirs.conflict and theirs.merged == edited          # remote edit arrives
        ours = reconcile("structured", base, edited, base, ours_newer=False, policy=policy)
        assert not ours.conflict and ours.merged == edited              # local edit survives


def test_two_sided_yaml_edits_on_different_lines_merge():
    base = b"a: 1\nb: 1\nc: 1\n"
    res = reconcile("structured", base, b"a: 2\nb: 1\nc: 1\n", b"a: 1\nb: 1\nc: 2\n", ours_newer=True)
    assert not res.conflict and res.merged == b"a: 2\nb: 1\nc: 2\n"


def test_json_is_still_validated_as_json():
    res = reconcile("structured", b'{"a": 1}', b'{"a": 2}', b'{"a": 3}', ours_newer=True)
    assert res.conflict and json.loads(res.merged) == {"a": 2}


# ── 5. `devsync encrypt --paths` ─────────────────────────────────────────────

def test_encrypt_paths_adds_to_the_default_instead_of_replacing_it(world):
    a, remote, tmp, mp = world
    mp.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    _project(a)
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert main(["encrypt", "--all-machines-upgraded", "--paths", "claude/**"]) == 0
    paths = json.loads(_remote(remote, "manifest.json"))["encryption"]["paths"]
    assert paths == ["repos/**", "claude/**"]
    assert _remote(remote, "repos/app/.env").startswith(crypto.MAGIC)        # still covered
    assert _remote(remote, "claude/global/CLAUDE.md").startswith(crypto.MAGIC)


def test_encrypt_paths_widens_a_store_that_is_already_encrypted(world, capsys):
    a, remote, tmp, mp = world
    mp.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    _project(a)
    assert _seed(a, remote, "--sync-repo-files") == 0
    assert _remote(remote, "claude/global/CLAUDE.md") == b"notes\n"
    capsys.readouterr()
    assert main(["encrypt", "--paths", "claude/**"]) == 0
    assert "now also encrypting claude/**" in capsys.readouterr().out
    assert _remote(remote, "claude/global/CLAUDE.md").startswith(crypto.MAGIC)
    assert (a / ".claude" / "CLAUDE.md").read_text() == "notes\n"
    assert main(["encrypt", "--paths", "claude/**"]) == 0                   # idempotent


# ── 6. expected failures are messages, not tracebacks ────────────────────────

def test_restore_of_something_not_in_trash_is_a_message(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["restore", "claude/global/NOPE.md"]) == 1
    assert "not found in trash" in capsys.readouterr().err


def test_sync_while_another_sync_holds_the_lock_is_a_message_the_wrapper_can_read(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    lock = a / ".devsync" / "devsync.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))          # a live process: this one
    capsys.readouterr()
    assert main(["sync"]) == 1
    assert "already running (pid" in capsys.readouterr().err
    shutil.rmtree(lock)


def test_a_remote_that_cannot_be_cloned_stops_before_anything_is_written(world, capsys):
    a, remote, tmp, mp = world
    capsys.readouterr()
    assert _seed(a, tmp / "no-such-remote.git") == 1
    assert "could not clone" in capsys.readouterr().err
    assert not (a / ".devsync" / "machine.json").exists()
    assert not (a / "code" / "devsync-store").exists()


# ── 7. nothing relative is stored ────────────────────────────────────────────

def test_relative_paths_are_stored_absolute(world, monkeypatch):
    a, remote, tmp, mp = world
    monkeypatch.chdir(a)
    assert main(["init", "--seed", "--remote", str(remote), "--root", "code",
                 "--claude-home", ".claude", "--hostname", "PC-A"]) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    for key in ("cascadeRoot", "claudeHome", "storeDir"):
        assert Path(data[key]).is_absolute(), key
    (a / ".x").mkdir()
    (a / ".x" / "R.md").write_text("r\n")
    assert main(["home", "add", "x", ".x", "--files", "R.md"]) == 0
    saved = json.loads((a / ".devsync" / "machine.json").read_text())["homes"]["x"]
    assert Path(saved).is_absolute() and Path(saved) == a / ".x"
    monkeypatch.chdir(tmp)                                # a scheduled run starts elsewhere
    assert main(["sync"]) == 0
    assert _remote(remote, "agents/x/R.md") == b"r\n"


# ── 9. `sync-repo-files off` means off ───────────────────────────────────────

def test_turning_repo_file_sync_off_leaves_existing_repo_files_alone(world):
    a, remote, tmp, mp = world
    proj = _project(a)
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert _remote(remote, "repos/app/.env") == b"KEY=1\n"
    assert main(["config", "set", "sync-repo-files", "off"]) == 0
    (proj / ".env").write_text("KEY=changed-after-off\n")
    assert main(["sync"]) == 0
    assert _remote(remote, "repos/app/.env") == b"KEY=1\n"            # store copy not updated
    assert (proj / ".env").read_text() == "KEY=changed-after-off\n"   # live copy not overwritten


def test_turning_repo_file_sync_on_in_an_unencrypted_store_needs_a_decision(world, capsys):
    a, remote, tmp, mp = world
    _project(a)
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["config", "set", "sync-repo-files", "on"]) == 2
    assert "plain text" in capsys.readouterr().err
    assert main(["sync"]) == 0
    assert _remote(remote, "repos/app/.env") is None
    assert main(["config", "set", "sync-repo-files", "plain"]) == 0
    assert main(["sync"]) == 0
    assert _remote(remote, "repos/app/.env") == b"KEY=1\n"


def test_on_is_accepted_once_the_store_is_encrypted(world):
    a, remote, tmp, mp = world
    mp.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    _project(a)
    assert _seed(a, remote) == 0
    assert main(["encrypt", "--all-machines-upgraded"]) == 0
    assert main(["config", "set", "sync-repo-files", "on"]) == 0
    assert main(["sync"]) == 0
    assert _remote(remote, "repos/app/.env").startswith(crypto.MAGIC)


# ── 11. the store keeps bytes exactly, whatever git is configured to do ──────

def test_new_stores_turn_line_ending_conversion_off(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    assert b"* -text" in _remote(remote, ".gitattributes")


def test_a_crlf_key_file_survives_a_machine_with_autocrlf(world):
    a, remote, tmp, mp = world
    pem = b"-----BEGIN X-----\r\nabc\r\n-----END X-----\r\n"
    script = b"#!/bin/sh\necho hi\n"
    _project(a, files={"certs/dev.pem": pem, ".env": b"K=1\n"})
    (a / ".claude" / "hooks").mkdir()
    (a / ".claude" / "hooks" / "run.sh").write_bytes(script)
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0

    b = _pc(tmp, "b")
    (b / ".gitconfig").write_text("[core]\n\tautocrlf = true\n")
    proj_b = b / "code" / "app"
    proj_b.mkdir()
    _git(proj_b, "init", "--initial-branch=main")
    (proj_b / ".gitignore").write_text(".env\ncerts/\n")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert (proj_b / "certs" / "dev.pem").read_bytes() == pem
    assert (b / ".claude" / "hooks" / "run.sh").read_bytes() == script


# ── 12. a path with a space in a git-level conflict ──────────────────────────

def test_a_conflicted_path_containing_a_space_is_resolved(world):
    a, remote, tmp, mp = world
    skill = Path(".claude") / "skills" / "my skill" / "SKILL.md"
    (a / skill).parent.mkdir(parents=True)
    (a / skill).write_text("one\nshared\nthree\n")
    assert _seed(a, remote) == 0
    b = _pc(tmp, "b")
    _use(mp, b)
    assert _adopt(b, remote) == 0
    (b / skill).write_text("one\nshared (B)\nthree\n")
    assert not _load_engine().sync(push=False).pushed          # committed locally, not pushed
    _use(mp, a)
    (a / skill).write_text("one\nshared (A)\nthree\n")
    assert main(["sync"]) == 0
    _use(mp, b)
    assert main(["sync"]) == 0
    text = (b / skill).read_text()
    assert "<<<<<<<" not in text and "shared (" in text
    assert _remote(remote, "claude/skills/my skill/SKILL.md").decode() == text


# ── 13. login files and caches inside a synced folder ────────────────────────

def test_a_new_store_skips_login_files_and_caches_inside_synced_folders(world):
    a, remote, tmp, mp = world
    ch = a / ".claude"
    (ch / "hooks" / "__pycache__").mkdir(parents=True)
    (ch / "hooks" / "__pycache__" / "guard.cpython-313.pyc").write_bytes(b"\x00\x01")
    (ch / "hooks" / "guard.py").write_text("print(1)\n")
    (ch / "lib").mkdir()
    (ch / "lib" / "credentials.json").write_text('{"k": "SECRET"}\n')
    (ch / "lib" / "helper.sh").write_text("echo\n")
    (ch / "skills" / "s" / "bin").mkdir(parents=True)
    (ch / "skills" / "s" / "bin" / "tool").write_text("a real file in a folder called bin\n")
    assert _seed(a, remote) == 0
    assert _remote(remote, "claude/hooks/guard.py") is not None
    assert _remote(remote, "claude/lib/helper.sh") is not None
    assert _remote(remote, "claude/skills/s/bin/tool") is not None       # ordinary folder names survive
    assert _remote(remote, "claude/hooks/__pycache__/guard.cpython-313.pyc") is None
    assert _remote(remote, "claude/lib/credentials.json") is None


def test_a_legacy_store_still_collects_synced_folders_whole(tmp_path):
    """Pre-0.2 behaviour, pinned: turning the filter on for an existing store would
    silently stop syncing files it has always carried."""
    ch = tmp_path / ".claude"
    (ch / "lib").mkdir(parents=True)
    (ch / "lib" / "credentials.json").write_text("{}\n")
    (ch / "hooks" / "__pycache__").mkdir(parents=True)
    (ch / "hooks" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    m = Machine(hostname="OLD", cascade_root=tmp_path / "code", claude_home=ch, project_key_prefix="X")
    got = collect_claude_files(m, DEFAULT_MANIFEST)
    assert "claude/lib/credentials.json" in got and "claude/hooks/__pycache__/x.pyc" in got


# ── 8 / 16. the wrappers ─────────────────────────────────────────────────────

def _run_ps_wrapper(tmp_path, python_lines, python_exit=1, devsync_body=None):
    ps = shutil.which("powershell.exe")
    if os.name != "nt" or not ps:
        pytest.skip("Windows PowerShell wrapper")
    shim = tmp_path / "shim"
    shim.mkdir()
    body = "@echo off\r\n" + "".join(f"echo {l} 1>&2\r\n" for l in python_lines) + f"exit /b {python_exit}\r\n"
    (shim / "python.cmd").write_bytes(body.encode("ascii"))
    (shim / "devsync.cmd").write_bytes((devsync_body or body).encode("ascii"))
    log = tmp_path / "sync.log"
    env = {k: v for k, v in os.environ.items() if k != "DEVSYNC_CMD"}
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(REPO / "scripts" / "devsync-sync-run.ps1"),
                        "-CascadeRoot", str(tmp_path), "-LogPath", str(log)],
                       capture_output=True, text=True, env=env)
    if not log.is_file():
        pytest.skip(f"wrapper could not run here: {r.stderr[:200]}")
    return r.returncode, log.read_text(encoding="utf-8-sig")


def test_a_pull_failure_that_is_not_a_network_problem_is_an_error_not_offline(tmp_path):
    code, log = _run_ps_wrapper(tmp_path, [
        "devsync: git pull failed:",          # the exact prefix the engine raises for any pull failure
        "fatal: refusing to merge unrelated histories",
    ])
    assert "ERROR" in log and "OFFLINE" not in log
    assert code == 1


def test_the_wrapper_uses_the_devsync_command_when_python_cannot_import_it(tmp_path):
    """What a `pipx install` leaves you with: `devsync` on PATH, and a system Python
    that has never heard of the package."""
    ok = "@echo off\r\necho devsync: merged=0  replaced=0  conflicts=0  tombstoned=0  pushed=no\r\nexit /b 0\r\n"
    code, log = _run_ps_wrapper(tmp_path, ["ModuleNotFoundError: No module named devsync"],
                                python_exit=1, devsync_body=ok)
    assert "OK" in log and "merged=0" in log
    assert code == 0


def test_the_linux_wrapper_runs_whatever_devsync_cmd_names(tmp_path):
    bash = shutil.which("bash")
    if os.name == "nt" or not bash:
        pytest.skip("POSIX wrapper")
    fake = tmp_path / "fake-devsync"
    fake.write_text("#!/bin/sh\necho 'devsync: merged=0  replaced=0  conflicts=0  tombstoned=0  pushed=no'\n")
    fake.chmod(0o755)
    log = tmp_path / "sync.log"
    env = dict(os.environ, DEVSYNC_CMD=str(fake), DEVSYNC_LOG=str(log), CASCADE_ROOT=str(tmp_path),
               HOME=str(tmp_path))
    r = subprocess.run([bash, str(REPO / "scripts" / "devsync-sync.sh"), "manual"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in log.read_text() and "[manual]" in log.read_text()


def test_the_linux_wrapper_says_how_to_fix_a_missing_install(tmp_path):
    bash = shutil.which("bash")
    if os.name == "nt" or not bash:
        pytest.skip("POSIX wrapper")
    log = tmp_path / "sync.log"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "DEVSYNC_LOG": str(log),
           "CASCADE_ROOT": str(tmp_path), "DEVSYNC_PY": str(tmp_path / "nope")}
    r = subprocess.run([bash, str(REPO / "scripts" / "devsync-sync.sh")], capture_output=True, text=True, env=env)
    if "devsync not found" not in log.read_text():
        pytest.skip("a devsync is installed system-wide here")
    assert r.returncode == 1 and "DEVSYNC_CMD" in log.read_text()


# ── the tool repository itself ───────────────────────────────────────────────

def test_the_tool_repo_pins_line_endings_and_ships_no_stale_spec():
    assert "eol=lf" in (REPO / ".gitattributes").read_text()
    assert not (REPO / "devsync-spec.md").exists()
    assert "devsync-spec" not in (REPO / "README.md").read_text(encoding="utf-8")
