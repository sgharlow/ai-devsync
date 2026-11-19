"""What a stranger meets on first install (0.2.0), and the proof that none of it
moves a store that already exists.

Two halves, deliberately in one file:

  * NEW-STORE defaults -- safe join, config-only sync, a root that is found rather
    than assumed, every setting reachable from `devsync config`.
  * LEGACY behaviour -- a manifest written before these keys existed must behave
    exactly as it did when the values were constants in the code.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devsync.cli import main
from devsync.collect import collect_repo_files
from devsync.engine import DEFAULT_MANIFEST, Engine, new_store_manifest
from devsync.paths import Machine, canonical_to_local, local_to_canonical

REPO = Path(__file__).resolve().parent.parent
PREFIX_A = "C--Users-A-code"
PREFIX_B = "C--Users-B-code"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote_file(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _remote_head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def _use(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _make_pc(home, prefix, claude_md, memory):
    (home / "code").mkdir(parents=True)
    mem = home / ".claude" / "projects" / prefix / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text(memory)
    (home / ".claude" / "CLAUDE.md").write_text(claude_md)
    return mem


def _make_project(root, name="app", env="KEY=1\n"):
    proj = root / name
    proj.mkdir(parents=True)
    _git(proj, "init", "--initial-branch=main")
    (proj / ".gitignore").write_text(".env\n")
    (proj / ".env").write_text(env)
    return proj


@pytest.fixture
def world(tmp_path, monkeypatch):
    """PC A (not yet initialised) and an empty bare remote. No legacy env vars."""
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "devsync tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tests@devsync.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "devsync tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tests@devsync.invalid")
    a = tmp_path / "a"
    _make_pc(a, PREFIX_A, "A's global\n", "a index\n")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _use(monkeypatch, a)
    return a, remote, tmp_path


def _seed_a(a, remote, *extra):
    return main(["init", "--seed", *extra, "--remote", str(remote),
                 "--root", str(a / "code"), "--claude-home", str(a / ".claude"),
                 "--hostname", "PC-A", "--project-key-prefix", PREFIX_A])


def _adopt_b(b, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote),
                 "--root", str(b / "code"), "--claude-home", str(b / ".claude"),
                 "--hostname", "PC-B", "--project-key-prefix", PREFIX_B])


# ── safe join ────────────────────────────────────────────────────────────────

def test_plain_adopt_refuses_when_this_pc_would_overwrite_the_fleet(world, monkeypatch, capsys):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    head = _remote_head(remote)
    b = tmp / "b"
    mem = _make_pc(b, PREFIX_B, "B's global\n", "b index\n")
    _use(monkeypatch, b)

    assert _adopt_b(b, remote) == 3

    err = capsys.readouterr().err
    assert "--follower" in err and "--merge" in err
    assert "claude/global/CLAUDE.md" in err
    # nothing left this PC, nothing on it changed, and it is not half-joined
    assert _remote_head(remote) == head
    assert (b / ".claude" / "CLAUDE.md").read_text() == "B's global\n"
    assert (mem / "MEMORY.md").read_text() == "b index\n"
    assert not (b / ".devsync" / "machine.json").exists()
    assert not (b / ".devsync" / "base").exists()


def test_the_refused_adopt_can_be_rerun_either_way(world, monkeypatch):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    b = tmp / "b"
    _make_pc(b, PREFIX_B, "B's global\n", "b index\n")
    _use(monkeypatch, b)
    assert _adopt_b(b, remote) == 3
    assert _adopt_b(b, remote, "--follower") == 0
    assert (b / ".claude" / "CLAUDE.md").read_text() == "A's global\n"
    assert _remote_file(remote, "claude/global/CLAUDE.md") == "A's global\n"


def test_plain_adopt_proceeds_when_nothing_here_differs(world, monkeypatch):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    b = tmp / "b"
    (b / "code").mkdir(parents=True)
    (b / ".claude").mkdir()
    _use(monkeypatch, b)
    assert _adopt_b(b, remote) == 0
    assert (b / ".claude" / "CLAUDE.md").read_text() == "A's global\n"


def test_a_crlf_copy_of_the_same_text_is_not_a_difference(world, monkeypatch):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    b = tmp / "b"
    (b / "code").mkdir(parents=True)
    (b / ".claude").mkdir()
    (b / ".claude" / "CLAUDE.md").write_bytes(b"A's global\r\n")
    _use(monkeypatch, b)
    assert _adopt_b(b, remote) == 0


def test_adopt_rejects_follower_and_merge_together(world, monkeypatch):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    b = tmp / "b"
    _make_pc(b, PREFIX_B, "B's global\n", "b index\n")
    _use(monkeypatch, b)
    assert _adopt_b(b, remote, "--follower", "--merge") == 1


# ── new-store defaults ───────────────────────────────────────────────────────

def test_new_store_does_not_sync_secrets_unless_asked(world, capsys):
    a, remote, _ = world
    _make_project(a / "code")
    assert _seed_a(a, remote) == 0
    assert _remote_file(remote, "repos/app/.env") is None
    manifest = json.loads(_remote_file(remote, "manifest.json"))
    assert manifest["syncRepoFiles"] is False
    assert "config and memory only" in capsys.readouterr().out


def test_plain_text_secret_sync_has_to_be_asked_for_and_says_so(world, capsys):
    a, remote, _ = world
    _make_project(a / "code")
    assert _seed_a(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert _remote_file(remote, "repos/app/.env") == "KEY=1\n"
    assert "UNENCRYPTED" in capsys.readouterr().out


def test_new_store_labels_memory_generically(world):
    a, remote, _ = world
    assert _seed_a(a, remote) == 0
    assert _remote_file(remote, "claude/memory/projects/MEMORY.md") == "a index\n"
    assert _remote_file(remote, "claude/memory/CascadeProjects/MEMORY.md") is None


def test_memory_key_root_can_be_chosen_at_seed(world):
    a, remote, _ = world
    assert _seed_a(a, remote, "--memory-key-root", "work") == 0
    assert _remote_file(remote, "claude/memory/work/MEMORY.md") == "a index\n"


def test_two_new_machines_with_different_roots_share_one_memory_tree(world, monkeypatch):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0
    b = tmp / "b"
    (b / "code").mkdir(parents=True)
    (b / ".claude").mkdir()
    _use(monkeypatch, b)
    assert _adopt_b(b, remote) == 0
    assert (b / ".claude" / "projects" / PREFIX_B / "memory" / "MEMORY.md").read_text() == "a index\n"


def test_new_store_carries_agents_and_rules_and_blocks_claude_synced_skills(world):
    a, remote, _ = world
    ch = a / ".claude"
    (ch / "agents").mkdir()
    (ch / "agents" / "reviewer.md").write_text("agent\n")
    (ch / "rules").mkdir()
    (ch / "rules" / "style.md").write_text("rule\n")
    (ch / "skills" / "synced" / "abc" / "docx").mkdir(parents=True)
    (ch / "skills" / "synced" / "abc" / "docx" / "SKILL.md").write_text("refreshed per machine\n")
    (ch / "skills" / "mine").mkdir(parents=True)
    (ch / "skills" / "mine" / "SKILL.md").write_text("my skill\n")
    assert _seed_a(a, remote) == 0
    assert _remote_file(remote, "claude/agents/reviewer.md") == "agent\n"
    assert _remote_file(remote, "claude/rules/style.md") == "rule\n"
    assert _remote_file(remote, "claude/skills/mine/SKILL.md") == "my skill\n"
    assert _remote_file(remote, "claude/skills/synced/abc/docx/SKILL.md") is None


# ── finding the root and the Claude home ─────────────────────────────────────

def _seed_default_root(home, remote):
    return main(["init", "--seed", "--remote", str(remote),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-A"])


def test_root_is_found_among_the_common_folders(world):
    a, remote, _ = world          # the fixture made ~/code
    assert _seed_default_root(a, remote) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert Path(data["cascadeRoot"]) == a / "code"


def test_legacy_default_root_still_wins_where_it_exists(world):
    a, remote, _ = world
    (a / "CascadeProjects").mkdir()
    assert _seed_default_root(a, remote) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert Path(data["cascadeRoot"]) == a / "CascadeProjects"


def test_devsync_root_env_overrides_the_search(world, monkeypatch):
    a, remote, tmp = world
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("DEVSYNC_ROOT", str(elsewhere))
    assert _seed_default_root(a, remote) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert Path(data["cascadeRoot"]) == elsewhere


def test_no_root_found_is_an_error_that_names_the_flag(world, capsys):
    a, remote, _ = world
    shutil.rmtree(a / "code")
    assert _seed_default_root(a, remote) == 1
    assert "--root" in capsys.readouterr().err
    assert not (a / ".devsync" / "machine.json").exists()


def test_claude_config_dir_is_the_default_claude_home(world, monkeypatch):
    a, remote, tmp = world
    alt = tmp / "alt-claude"
    alt.mkdir()
    (alt / "CLAUDE.md").write_text("alt global\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    assert main(["init", "--seed", "--remote", str(remote), "--root", str(a / "code"),
                 "--hostname", "PC-A"]) == 0
    assert _remote_file(remote, "claude/global/CLAUDE.md") == "alt global\n"


def test_old_cascade_root_flag_is_still_accepted(world):
    a, remote, _ = world
    assert main(["init", "--seed", "--remote", str(remote),
                 "--cascade-root", str(a / "code"), "--claude-home", str(a / ".claude"),
                 "--hostname", "PC-A", "--project-key-prefix", PREFIX_A]) == 0


# ── `devsync config` ─────────────────────────────────────────────────────────

def test_config_shows_machine_and_store_settings(world, capsys):
    a, remote, _ = world
    assert _seed_a(a, remote) == 0
    capsys.readouterr()
    assert main(["config"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert Path(shown["thisMachine"]["root"]) == a / "code"
    assert shown["thisMachine"]["hostname"] == "PC-A"
    assert shown["store"] == {"sync-repo-files": False, "skip-project-dirs": [],
                              "memory-key-root": "projects",
                              "conflict-policy": "newer",
                              "agent-homes": ["codex", "gemini", "kiro"],
                              "encryption": "off"}


def test_config_set_root_moves_the_derived_prefix_with_it(world, capsys):
    a, remote, tmp = world
    assert main(["init", "--seed", "--remote", str(remote), "--root", str(a / "code"),
                 "--claude-home", str(a / ".claude"), "--hostname", "PC-A"]) == 0
    new_root = tmp / "moved"
    new_root.mkdir()
    assert main(["config", "set", "root", str(new_root)]) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert Path(data["cascadeRoot"]) == new_root
    import re
    assert data["projectKeyPrefix"] == re.sub(r"[^A-Za-z0-9]", "-", new_root.as_posix())
    assert "BEFORE the next sync" in capsys.readouterr().out


def test_config_set_root_leaves_a_hand_set_prefix_alone(world, capsys):
    a, remote, tmp = world
    assert _seed_a(a, remote) == 0          # PREFIX_A is not the derived value
    assert main(["config", "set", "root", str(tmp)]) == 0
    data = json.loads((a / ".devsync" / "machine.json").read_text())
    assert data["projectKeyPrefix"] == PREFIX_A
    assert "set by hand" in capsys.readouterr().out


def test_config_can_turn_secret_sync_on_later(world):
    a, remote, _ = world
    _make_project(a / "code")
    assert _seed_a(a, remote) == 0
    assert _remote_file(remote, "repos/app/.env") is None
    assert main(["config", "set", "sync-repo-files", "plain"]) == 0
    assert main(["sync"]) == 0
    assert _remote_file(remote, "repos/app/.env") == "KEY=1\n"


def test_config_refuses_to_relabel_memory_on_a_live_store(world, capsys):
    a, remote, _ = world
    assert _seed_a(a, remote) == 0
    assert main(["config", "set", "memory-key-root", "other"]) == 2
    assert json.loads((a / "code" / "devsync-store" / "manifest.json").read_text())["memoryKeyRoot"] == "projects"


def test_config_rejects_unknown_keys_and_bad_values(world):
    a, remote, _ = world
    assert _seed_a(a, remote) == 0
    assert main(["config", "set", "nonsense", "1"]) == 2
    assert main(["config", "set", "sync-repo-files", "maybe"]) == 2
    assert main(["config", "set", "root"]) == 2


# ── skip list and the store's own folder ─────────────────────────────────────

def _machine(root, tmp):
    return Machine(hostname="T", cascade_root=root, claude_home=tmp / ".claude",
                   project_key_prefix="X")


def test_skip_project_dirs_comes_from_the_manifest(tmp_path):
    root = tmp_path / "code"
    _make_project(root, "keep")
    _make_project(root, "vendor-mirror")
    manifest = dict(DEFAULT_MANIFEST, skipProjectDirs=["vendor-mirror"])
    got = collect_repo_files(_machine(root, tmp_path), manifest)
    assert "repos/keep/.env" in got
    assert "repos/vendor-mirror/.env" not in got


def test_the_store_checkout_is_never_scanned_whatever_it_is_called(tmp_path):
    root = tmp_path / "code"
    _make_project(root, "keep")
    store = _make_project(root, "my-sync-store")
    manifest = dict(DEFAULT_MANIFEST, skipProjectDirs=[])
    got = collect_repo_files(_machine(root, tmp_path), manifest, exclude=(store,))
    assert set(got) == {"repos/keep/.env"}


# ── legacy stores: nothing moves ─────────────────────────────────────────────

def test_legacy_manifest_has_none_of_the_new_keys():
    """DEFAULT_MANIFEST is what a corrupt or missing manifest falls back to, so it
    must keep describing the ORIGINAL behaviour."""
    for key in ("syncRepoFiles", "memoryKeyRoot", "skipProjectDirs"):
        assert key not in DEFAULT_MANIFEST
    assert "claude/skills/synced/**" not in DEFAULT_MANIFEST["blocklistGlobs"]
    assert DEFAULT_MANIFEST["claudePaths"]["trees"] == ["skills", "commands", "hooks", "lib"]


def test_new_store_manifest_does_not_mutate_the_legacy_default():
    before = json.dumps(DEFAULT_MANIFEST, sort_keys=True)
    new_store_manifest(sync_repo_files=True, skip_project_dirs=["x"])
    assert json.dumps(DEFAULT_MANIFEST, sort_keys=True) == before


def test_legacy_skip_list_applies_when_the_manifest_is_silent(tmp_path):
    root = tmp_path / "code"
    _make_project(root, "keep")
    for legacy in ("CascadeArchive", "claude-config", "devsync-store"):
        _make_project(root, legacy)
    got = collect_repo_files(_machine(root, tmp_path), DEFAULT_MANIFEST)
    assert set(got) == {"repos/keep/.env"}


def test_legacy_store_keeps_its_memory_label_and_its_secret_sync(tmp_path, monkeypatch):
    """A store whose manifest predates every new key: memory stays under
    `CascadeProjects`, gitignored files still sync, and a 0.2.0 engine reading it
    writes nothing that an older engine on another PC would not expect."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@devsync.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@devsync.invalid")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    root = tmp_path / "source"
    _make_project(root, "app")
    ch = tmp_path / ".claude"
    mem = ch / "projects" / "D--source" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("index\n")
    store = root / "devsync-store"
    _git(tmp_path, "clone", str(remote), str(store))
    legacy = json.dumps(DEFAULT_MANIFEST, indent=2) + "\n"
    (store / "manifest.json").write_text(legacy)
    (store / "tombstones.json").write_text("[]\n")

    m = Machine(hostname="OLD-PC", cascade_root=root, claude_home=ch,
                project_key_prefix="D--source")
    eng = Engine(machine=m, store_dir=store, state_dir=tmp_path / ".devsync")
    summary = eng.sync()

    assert summary.conflicts == 0 and summary.pushed
    assert _remote_file(remote, "claude/memory/CascadeProjects/MEMORY.md") == "index\n"
    assert _remote_file(remote, "repos/app/.env") == "KEY=1\n"
    assert _remote_file(remote, "manifest.json") == legacy       # not rewritten
    assert eng.machine.memory_key_root == "CascadeProjects"
    canon = "claude/memory/CascadeProjects-app/x.md"
    assert local_to_canonical(canonical_to_local(canon, eng.machine), eng.machine) == canon


# ── install surface ──────────────────────────────────────────────────────────

def test_python_dash_m_devsync_runs_without_the_console_script():
    r = subprocess.run([sys.executable, "-m", "devsync", "--version"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip().startswith("devsync ")


# ── Windows wrapper: a rejected credential must not read as "offline" ────────

def _run_wrapper(tmp_path, stderr_lines):
    ps = shutil.which("powershell.exe")
    if os.name != "nt" or not ps:
        pytest.skip("Windows PowerShell wrapper")
    shim = tmp_path / "shim"
    shim.mkdir()
    body = "@echo off\r\n" + "".join(f"echo {line} 1>&2\r\n" for line in stderr_lines) + "exit /b 1\r\n"
    (shim / "python.cmd").write_bytes(body.encode("ascii"))
    (shim / "devsync.cmd").write_bytes(body.encode("ascii"))
    log = tmp_path / "sync.log"
    env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"])
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(REPO / "scripts" / "devsync-sync-run.ps1"),
                        "-CascadeRoot", str(tmp_path), "-LogPath", str(log)],
                       capture_output=True, text=True, env=env)
    if not log.is_file():
        pytest.skip(f"wrapper could not run here: {r.stderr[:200]}")
    return r.returncode, log.read_text(encoding="utf-8-sig")


def test_wrapper_reports_rejected_credentials_as_an_error(tmp_path):
    code, log = _run_wrapper(tmp_path, [
        "RuntimeError: git pull failed:",
        "remote: Invalid username or password.",
        "fatal: Authentication failed for https://example.invalid/store.git",
    ])
    assert "AUTH" in log and "ERROR" in log and "OFFLINE" not in log
    assert code == 1


def test_wrapper_still_treats_no_network_as_offline(tmp_path):
    code, log = _run_wrapper(tmp_path, [
        "RuntimeError: git pull failed:",
        "fatal: unable to access https://example.invalid/store.git: Could not resolve host: example.invalid",
    ])
    assert "OFFLINE" in log and "AUTH" not in log
    assert code == 0


def test_package_version_matches_pyproject():
    import re
    from devsync._version import __version__
    declared = re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M).group(1)
    assert __version__ == declared


# ── POSIX shapes (Linux, macOS) ──────────────────────────────────────────────

def test_derived_prefix_matches_claude_codes_naming_on_posix_and_windows():
    from pathlib import PurePosixPath, PureWindowsPath
    from devsync.cli import _default_project_key_prefix
    assert _default_project_key_prefix(PurePosixPath("/Users/me/code")) == "-Users-me-code"
    assert _default_project_key_prefix(PurePosixPath("/home/dev.user/src")) == "-home-dev-user-src"
    assert _default_project_key_prefix(PureWindowsPath("C:/Users/Alex.Smith/code")) == "C--Users-Alex-Smith-code"


def test_a_prefix_starting_with_a_dash_is_accepted_with_equals(world):
    a, remote, _ = world
    mem = a / ".claude" / "projects" / "-Users-a-code" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("posix index\n")
    assert main(["init", "--seed", "--remote", str(remote), "--root", str(a / "code"),
                 "--claude-home", str(a / ".claude"), "--hostname", "MAC",
                 "--project-key-prefix=-Users-a-code"]) == 0
    assert _remote_file(remote, "claude/memory/projects/MEMORY.md") == "posix index\n"


def test_posix_memory_paths_round_trip():
    from pathlib import PurePosixPath
    m = Machine(hostname="MAC", cascade_root=Path("/Users/me/code"),
                claude_home=Path("/Users/me/.claude"),
                project_key_prefix="-Users-me-code", memory_key_root="projects")
    local = canonical_to_local("claude/memory/projects-app/MEMORY.md", m)
    assert PurePosixPath(local.as_posix()).parts[-4:] == ("projects", "-Users-me-code-app", "memory", "MEMORY.md")


# ── the way the scheduled task actually starts it ────────────────────────────

def test_runs_when_the_top_level_name_is_only_a_namespace_package(tmp_path):
    """The sync wrappers run `python -m devsync.cli` from the dev root, which holds
    the repo folder `devsync/`. With an editable install Python resolves the name
    `devsync` to that folder as a namespace package: __init__.py never runs, yet
    submodules import fine. Anything imported FROM the package itself breaks there
    and nowhere else -- found only by the first real sync of 0.2.0.

    Reproduced here by a copy of the package with no __init__.py."""
    ns = tmp_path / "devroot" / "devsync"
    ns.mkdir(parents=True)
    for f in (REPO / "devsync").glob("*.py"):
        if f.name != "__init__.py":
            shutil.copyfile(f, ns / f.name)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    for args in (["-m", "devsync.cli", "--version"],
                 ["-c", "import devsync.cli, devsync.engine, devsync.crypto, devsync.collect, "
                        "devsync.merge, devsync.paths; print(devsync.cli.__file__)"]):
        r = subprocess.run([sys.executable, *args], cwd=ns.parent, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-600:]
    assert str(ns) in r.stdout or ns.name in r.stdout


# ── a user who has never set a git name or email ─────────────────────────────

def _no_git_identity(monkeypatch, tmp_path):
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_first_sync_works_for_a_user_with_no_git_identity(world, monkeypatch):
    a, remote, tmp = world
    _no_git_identity(monkeypatch, tmp)
    assert _seed_a(a, remote) == 0
    author = subprocess.run(["git", "--git-dir", str(remote), "log", "-1", "--format=%an <%ae>", "main"],
                            capture_output=True, text=True).stdout.strip()
    assert author == "devsync <devsync@PC-A>"


def test_a_configured_git_identity_is_always_used(world):
    a, remote, _ = world          # the fixture sets GIT_AUTHOR_* / GIT_COMMITTER_*
    assert _seed_a(a, remote) == 0
    author = subprocess.run(["git", "--git-dir", str(remote), "log", "-1", "--format=%an <%ae>", "main"],
                            capture_output=True, text=True).stdout.strip()
    assert author == "devsync tests <tests@devsync.invalid>"
