"""Other coding agents' config folders ("homes"), alongside Claude Code's.

What is pinned:
  * a new store knows Codex, Gemini CLI and Kiro, and syncs only the listed
    instruction material from each -- never logins, history or settings;
  * an agent is synced on a PC only if its folder exists there; a PC without it is
    left alone and says nothing;
  * a store created before `homes` existed syncs no other agent at all;
  * any other agent can be added by name with `devsync home add`.
"""
import json
import subprocess
from pathlib import Path

import pytest

from devsync.cli import main
from devsync.collect import collect_home_files
from devsync.engine import DEFAULT_MANIFEST, Engine, new_store_manifest, resolve_homes
from devsync.paths import HomeNotPresent, Machine, canonical_to_local, local_to_canonical


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote_file(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _remote_paths(remote):
    r = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                       capture_output=True, text=True)
    return r.stdout.split("\n")


def _use(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _blank_pc(tmp_path, name):
    home = tmp_path / name
    (home / "code").mkdir(parents=True)
    (home / ".claude").mkdir()
    return home


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR",
                "DEVSYNC_PASSPHRASE", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    a = _blank_pc(tmp_path, "a")
    (a / ".claude" / "CLAUDE.md").write_text("claude notes\n")
    _use(monkeypatch, a)
    return a, remote, tmp_path, monkeypatch


def _seed(home, remote):
    return main(["init", "--seed", "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-A"])


def _adopt(home, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "PC-B"])


def _codex(home, text="codex rules\n"):
    d = home / ".codex"
    d.mkdir(exist_ok=True)
    (d / "AGENTS.md").write_text(text)
    (d / "auth.json").write_text('{"token": "LOGIN-SECRET"}\n')
    (d / "config.toml").write_text('[mcp]\nkey = "TOKEN-IN-CONFIG"\n')
    (d / "history.jsonl").write_text("{}\n")
    return d


# ── which stores know which agents ───────────────────────────────────────────

def test_new_stores_know_three_agents_and_old_stores_know_none():
    names = [h["name"] for h in new_store_manifest()["homes"]]
    assert names == ["codex", "gemini", "kiro"]
    assert "homes" not in DEFAULT_MANIFEST


def test_presets_list_no_settings_or_credential_files():
    listed = [f for h in new_store_manifest()["homes"] for f in h["global"] + h["trees"]]
    for risky in ("config.toml", "settings.json", "mcp.json", "auth.json", "settings"):
        assert risky not in listed


def test_a_legacy_store_ignores_other_agents_even_when_their_folders_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _codex(tmp_path)
    assert resolve_homes(DEFAULT_MANIFEST) == {}
    m = Machine(hostname="OLD", cascade_root=tmp_path / "code", claude_home=tmp_path / ".claude",
                project_key_prefix="X")
    assert collect_home_files(m, DEFAULT_MANIFEST) == {}


# ── end to end ───────────────────────────────────────────────────────────────

def test_instruction_files_sync_and_logins_settings_and_history_do_not(world, capsys):
    a, remote, tmp, mp = world
    _codex(a)
    (a / ".gemini" / "commands" / "git").mkdir(parents=True)
    (a / ".gemini" / "GEMINI.md").write_text("gemini rules\n")
    (a / ".gemini" / "commands" / "git" / "commit.toml").write_text('prompt = "x"\n')
    (a / ".gemini" / "oauth_creds.json").write_text("{}\n")
    (a / ".kiro" / "steering").mkdir(parents=True)
    (a / ".kiro" / "steering" / "style.md").write_text("kiro steering\n")
    (a / ".kiro" / "settings").mkdir()
    (a / ".kiro" / "settings" / "mcp.json").write_text('{"k": "TOKEN"}\n')

    assert _seed(a, remote) == 0

    assert "codex" in capsys.readouterr().out
    assert _remote_file(remote, "agents/codex/AGENTS.md") == "codex rules\n"
    assert _remote_file(remote, "agents/gemini/GEMINI.md") == "gemini rules\n"
    assert _remote_file(remote, "agents/gemini/commands/git/commit.toml") == 'prompt = "x"\n'
    assert _remote_file(remote, "agents/kiro/steering/style.md") == "kiro steering\n"
    assert _remote_file(remote, "claude/global/CLAUDE.md") == "claude notes\n"
    everything = "\n".join(_remote_paths(remote))
    for leaked in ("auth.json", "config.toml", "history.jsonl", "oauth_creds.json", "mcp.json"):
        assert leaked not in everything
    grep = subprocess.run(["git", "--git-dir", str(remote), "grep", "-l", "-e", "LOGIN-SECRET",
                           "-e", "TOKEN", "main"], capture_output=True, text=True)
    assert grep.stdout.strip() == ""


def test_a_second_pc_with_the_agent_gets_the_files_and_one_without_is_left_alone(world, capsys):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0

    b = _blank_pc(tmp, "b")
    (b / ".codex").mkdir()                       # uses Codex, has no instructions yet
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert (b / ".codex" / "AGENTS.md").read_text() == "codex rules\n"

    c = _blank_pc(tmp, "c")                      # does not use Codex at all
    _use(mp, c)
    capsys.readouterr()
    assert _adopt(c, remote) == 0
    out = capsys.readouterr().out
    assert not (c / ".codex").exists()           # no folder invented for an unused agent
    assert "unmappable" not in out and "agents/" not in out
    assert main(["sync"]) == 0
    assert _remote_file(remote, "agents/codex/AGENTS.md") == "codex rules\n"   # still in the store


def test_two_pcs_merge_edits_to_another_agents_instructions(world):
    a, remote, tmp, mp = world
    _codex(a, "line one\nline two\n")
    assert _seed(a, remote) == 0
    b = _blank_pc(tmp, "b")
    (b / ".codex").mkdir()
    _use(mp, b)
    assert _adopt(b, remote) == 0
    (b / ".codex" / "AGENTS.md").write_text("line one\nline two\nadded on B\n")
    assert main(["sync"]) == 0
    _use(mp, a)
    (a / ".codex" / "AGENTS.md").write_text("added on A\nline one\nline two\n")
    assert main(["sync"]) == 0
    assert (a / ".codex" / "AGENTS.md").read_text() == "added on A\nline one\nline two\nadded on B\n"


def test_installing_the_agent_later_brings_its_files_down(world):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0
    c = _blank_pc(tmp, "c")
    _use(mp, c)
    assert _adopt(c, remote) == 0
    assert not (c / ".codex").exists()
    (c / ".codex").mkdir()                       # the user installs Codex here
    assert main(["sync"]) == 0
    assert (c / ".codex" / "AGENTS.md").read_text() == "codex rules\n"


def test_the_join_guard_counts_another_agents_files_too(world, capsys):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0
    b = _blank_pc(tmp, "b")
    _codex(b, "B's different codex rules\n")
    _use(mp, b)
    capsys.readouterr()
    assert _adopt(b, remote) == 3
    assert "agents/codex/AGENTS.md" in capsys.readouterr().err
    assert _adopt(b, remote, "--follower") == 0
    assert (b / ".codex" / "AGENTS.md").read_text() == "codex rules\n"


# ── where a home is looked for ───────────────────────────────────────────────

def test_the_agents_own_environment_variable_is_honoured(world):
    a, remote, tmp, mp = world
    elsewhere = tmp / "codex-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "AGENTS.md").write_text("from CODEX_HOME\n")
    mp.setenv("CODEX_HOME", str(elsewhere))
    assert _seed(a, remote) == 0
    assert _remote_file(remote, "agents/codex/AGENTS.md") == "from CODEX_HOME\n"


def test_this_pcs_own_path_wins_over_the_default(world):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    odd = tmp / "portable" / "gemini"
    odd.mkdir(parents=True)
    (odd / "GEMINI.md").write_text("portable gemini\n")
    assert main(["home", "path", "gemini", str(odd)]) == 0
    assert main(["sync"]) == 0
    assert _remote_file(remote, "agents/gemini/GEMINI.md") == "portable gemini\n"
    saved = json.loads((a / ".devsync" / "machine.json").read_text())
    assert Path(saved["homes"]["gemini"]) == odd


# ── `devsync home` ───────────────────────────────────────────────────────────

def test_home_list_shows_claude_and_each_agent_with_whether_it_is_here(world, capsys):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0
    capsys.readouterr()
    assert main(["home", "list"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["claude"]["onThisPC"] is True
    assert rows["codex"]["onThisPC"] is True and rows["codex"]["files"] == ["AGENTS.md"]
    assert rows["gemini"]["onThisPC"] is False and rows["gemini"]["path"] == "~/.gemini"


def test_home_add_syncs_any_other_agent_by_name(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    tool = a / ".myagent"
    (tool / "rules").mkdir(parents=True)
    (tool / "RULES.md").write_text("my agent\n")
    (tool / "rules" / "py.md").write_text("python rule\n")
    (tool / "rules" / "auth.json").write_text("never\n")
    (tool / "session.db").write_text("not listed\n")

    assert main(["home", "add", "myagent", str(tool), "--files", "RULES.md", "--folders", "rules"]) == 0
    assert main(["sync"]) == 0

    assert _remote_file(remote, "agents/myagent/RULES.md") == "my agent\n"
    assert _remote_file(remote, "agents/myagent/rules/py.md") == "python rule\n"
    assert _remote_file(remote, "agents/myagent/rules/auth.json") is None     # never, even in a listed folder
    assert _remote_file(remote, "agents/myagent/session.db") is None          # not listed
    spec = [h for h in json.loads(_remote_file(remote, "manifest.json"))["homes"] if h["name"] == "myagent"][0]
    assert spec["default"] == "~/.myagent"       # so the next PC finds it without being told

    b = _blank_pc(tmp, "b")
    (b / ".myagent").mkdir()
    _use(mp, b)
    assert _adopt(b, remote) == 0
    assert (b / ".myagent" / "rules" / "py.md").read_text() == "python rule\n"


def test_home_add_refuses_a_bare_folder_and_bad_or_reserved_names(world, capsys):
    a, remote, tmp, mp = world
    assert _seed(a, remote) == 0
    d = a / ".x"
    d.mkdir()
    assert main(["home", "add", "x", str(d)]) == 2                     # nothing listed
    assert "--files" in capsys.readouterr().err
    assert main(["home", "add", "claude", str(d), "--files", "A.md"]) == 2
    assert main(["home", "add", "Bad Name", str(d), "--files", "A.md"]) == 2
    assert main(["home", "path", "nosuch", str(d)]) == 2
    assert main(["home", "remove", "nosuch"]) == 2


def test_home_remove_stops_syncing_and_deletes_nothing(world, capsys):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0
    assert main(["home", "remove", "codex"]) == 0
    (a / ".codex" / "AGENTS.md").write_text("changed after removal\n")
    assert main(["sync"]) == 0
    assert (a / ".codex" / "AGENTS.md").read_text() == "changed after removal\n"   # live file untouched
    assert _remote_file(remote, "agents/codex/AGENTS.md") == "codex rules\n"       # store copy kept, not updated


# ── path mapping ─────────────────────────────────────────────────────────────

def test_agent_paths_round_trip_and_an_absent_home_is_its_own_error(tmp_path):
    home = tmp_path / ".codex"
    m = Machine(hostname="T", cascade_root=tmp_path / "code", claude_home=tmp_path / ".claude",
                project_key_prefix="X", homes={"codex": home})
    local = canonical_to_local("agents/codex/prompts/a.md", m)
    assert local == home / "prompts" / "a.md"
    assert local_to_canonical(local, m) == "agents/codex/prompts/a.md"
    with pytest.raises(HomeNotPresent):
        canonical_to_local("agents/gemini/GEMINI.md", m)
    assert issubclass(HomeNotPresent, ValueError)


def test_rm_and_restore_work_on_another_agents_file(world):
    a, remote, tmp, mp = world
    _codex(a)
    assert _seed(a, remote) == 0
    f = a / ".codex" / "AGENTS.md"
    assert main(["rm", str(f)]) == 0
    assert not f.exists() and _remote_file(remote, "agents/codex/AGENTS.md") is None
    assert main(["restore", "agents/codex/AGENTS.md"]) == 0
    assert f.read_text() == "codex rules\n"
