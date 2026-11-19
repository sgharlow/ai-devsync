"""The off-machine heartbeat check that `devsync monitor install` puts in a store.

The measurement is exercised against real git history with controlled commit
times. The GitHub Actions workflow around it is not run here; what is pinned is
that it parses, calls the script, and consumes exactly the outputs the script emits.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devsync.cli import main

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "devsync" / "templates" / "store" / ".github"
SCRIPT = TEMPLATES / "heartbeat_check.py"
WORKFLOW = TEMPLATES / "workflows" / "sync-heartbeat.yml"


def _lf(path: Path) -> bytes:
    """The template as installed: LF, whatever line endings this checkout has."""
    return path.read_bytes().replace(b"\r\n", b"\n")
HOUR = 3600
NOW = 1_800_000_000


def _load():
    spec = importlib.util.spec_from_file_location("heartbeat_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hb = _load()


@pytest.fixture
def store(tmp_path):
    repo = tmp_path / "store"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True)

    def commit(subject, hours_ago):
        stamp = f"{NOW - int(hours_ago * HOUR)} +0000"
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@devsync.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@devsync.invalid")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                       cwd=repo, check=True, env=env)
    return repo, commit


def _states(result):
    return {m["machine"]: m["state"] for m in result["machines"]}


def test_everything_fresh(store):
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 30)
    commit("sync LAPTOP 20260101-010000", 2)
    r = hb.measure(repo, NOW, 48, 72)
    assert r["stale"] is False and r["age_hours"] == 2
    assert _states(r) == {"DESKTOP": "fresh", "LAPTOP": "fresh"}


def test_one_machine_dead_while_the_other_keeps_the_global_measure_green(store):
    """The failure the per-machine measure exists for."""
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 100)
    commit("sync LAPTOP 20260101-010000", 1)
    r = hb.measure(repo, NOW, 48, 72)
    assert r["stale"] is False                       # global: looks healthy
    assert _states(r) == {"DESKTOP": "stale", "LAPTOP": "fresh"}


def test_nothing_syncing_anywhere(store):
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 60)
    r = hb.measure(repo, NOW, 48, 72)
    assert r["stale"] is True and r["age_hours"] == 60
    assert _states(r) == {"DESKTOP": "fresh"}        # 60h is inside its own 72h


def test_the_newest_commit_per_machine_is_what_counts(store):
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 500)
    commit("sync DESKTOP 20260102-000000", 3)
    assert _states(hb.measure(repo, NOW, 48, 72)) == {"DESKTOP": "fresh"}


def test_other_commits_are_not_heartbeats(store):
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 200)
    commit("devsync auto-resolve pull conflicts (DESKTOP)", 1)
    commit("edit manifest by hand", 1)
    r = hb.measure(repo, NOW, 48, 72)
    assert r["stale"] is True and _states(r) == {"DESKTOP": "stale"}


def test_a_retired_machine_is_reported_but_never_stale(store):
    repo, commit = store
    commit("sync OLD-PC 20260101-000000", 5000)
    commit("sync LAPTOP 20260101-010000", 1)
    r = hb.measure(repo, NOW, 48, 72, {"retired": ["OLD-PC"]})
    assert _states(r) == {"LAPTOP": "fresh", "OLD-PC": "retired"}


def test_a_machine_missing_from_the_config_is_still_watched(store):
    repo, commit = store
    commit("sync NEW-PC 20260101-000000", 100)
    commit("sync LAPTOP 20260101-010000", 1)
    r = hb.measure(repo, NOW, 48, 72, {"retired": ["SOMETHING-ELSE"], "thresholds": {"LAPTOP": 10}})
    assert _states(r)["NEW-PC"] == "stale"


def test_a_per_machine_threshold_overrides_the_default(store):
    repo, commit = store
    commit("sync TRAVEL 20260101-000000", 100)
    commit("sync LAPTOP 20260101-010000", 1)
    r = hb.measure(repo, NOW, 48, 72, {"thresholds": {"TRAVEL": 168}})
    assert _states(r)["TRAVEL"] == "fresh"


def test_minus_one_forces_the_alert_and_zero_does_not(store):
    """The drill instruction in the docs: a just-landed sync is 0h old and 0 > 0 is false."""
    repo, commit = store
    commit("sync LAPTOP 20260101-010000", 0)
    assert hb.measure(repo, NOW, 0, 0)["stale"] is False
    assert _states(hb.measure(repo, NOW, 0, 0)) == {"LAPTOP": "fresh"}
    forced = hb.measure(repo, NOW, -1, -1)
    assert forced["stale"] is True and _states(forced) == {"LAPTOP": "stale"}


def test_a_store_with_no_sync_commits_is_an_error_not_a_pass(store, capsys):
    repo, commit = store
    commit("initial", 1)
    assert "error" in hb.measure(repo, NOW, 48, 72)
    assert hb.main(["--repo", str(repo), "--now", str(NOW)]) == 1


def test_the_command_line_writes_the_outputs_the_workflow_reads(store, tmp_path, monkeypatch, capsys):
    repo, commit = store
    commit("sync DESKTOP 20260101-000000", 100)
    commit("sync LAPTOP 20260101-010000", 1)
    (repo / ".github").mkdir()
    (repo / ".github" / "heartbeat-machines.json").write_text('{"thresholds": {"LAPTOP": 5}}')
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert hb.main(["--repo", str(repo), "--now", str(NOW)]) == 0
    assert hb.main(["--repo", str(repo), "--now", str(NOW), "--exit-code"]) == 2
    text = out.read_text()
    assert "stale=false\n" in text and "age_hours=1\n" in text
    assert "stale_machines=DESKTOP:100:72:" in text
    assert "fresh_machines=LAPTOP:1:" in text
    assert "per_machine<<DEVSYNC_TABLE\n" in text and "| `DESKTOP` | 100h |" in text
    assert "STALE" in capsys.readouterr().out


def test_the_workflow_uses_exactly_the_outputs_the_script_writes():
    import re
    wf = WORKFLOW.read_text(encoding="utf-8")
    used = set(re.findall(r"steps\.heartbeat\.outputs\.([a-z_]+)", wf))
    src = SCRIPT.read_text(encoding="utf-8")
    written = set(re.findall(r'f\.write\(f?"([a-z_]+)[=<]', src))
    assert used and used <= written, used - written
    assert "python3 .github/heartbeat_check.py" in wf
    assert "\t" not in wf                              # YAML: no tabs


def test_the_script_runs_standalone_with_no_devsync_installed(store):
    repo, commit = store
    commit("sync LAPTOP 20260101-010000", 1)
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--repo", str(repo), "--now", str(NOW)],
                       capture_output=True, text=True, cwd=repo)
    assert r.returncode == 0 and "LAPTOP: 1h ago" in r.stdout


# ── `devsync monitor install` ────────────────────────────────────────────────

@pytest.fixture
def pc(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR", "DEVSYNC_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    home = tmp_path / "home"
    (home / "code").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("notes\n")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main"], cwd=remote, check=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))
    assert main(["init", "--seed", "--remote", str(remote), "--root", str(home / "code"),
                 "--claude-home", str(home / ".claude"), "--hostname", "LAPTOP"]) == 0
    return home, remote


def _remote_file(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def test_monitor_install_publishes_both_files_to_the_store(pc, capsys):
    home, remote = pc
    assert main(["monitor", "install"]) == 0
    assert _remote_file(remote, ".github/heartbeat_check.py") == _lf(SCRIPT)
    assert _remote_file(remote, ".github/workflows/sync-heartbeat.yml") == _lf(WORKFLOW)
    assert "Rehearse it once" in capsys.readouterr().out


def test_the_installed_check_sees_this_machines_own_syncs(pc):
    home, remote = pc
    assert main(["monitor", "install"]) == 0
    store = home / "code" / "devsync-store"
    r = hb.measure(store, int(__import__("time").time()) + 60, 48, 72)
    assert _states(r) == {"LAPTOP": "fresh"} and r["stale"] is False


def test_monitor_files_are_never_mapped_onto_a_live_path(pc):
    """They live at the store root, outside repos/ and claude/, so a sync on any
    machine must leave them alone and must not copy them anywhere."""
    home, remote = pc
    assert main(["monitor", "install"]) == 0
    assert main(["sync"]) == 0
    assert not (home / ".claude" / ".github").exists()
    assert not (home / "code" / ".github").exists()
    assert _remote_file(remote, ".github/heartbeat_check.py") == _lf(SCRIPT)


def test_monitor_install_keeps_a_customised_workflow_unless_forced(pc, capsys):
    home, remote = pc
    assert main(["monitor", "install"]) == 0
    wf = home / "code" / "devsync-store" / ".github" / "workflows" / "sync-heartbeat.yml"
    wf.write_text("# mine\n")
    capsys.readouterr()
    assert main(["monitor", "install"]) == 0
    assert wf.read_text() == "# mine\n"
    assert "kept your existing" in capsys.readouterr().out
    assert main(["monitor", "install", "--force"]) == 0
    assert wf.read_bytes() == _lf(WORKFLOW)


def test_monitor_install_writes_lf_even_from_a_crlf_checkout(pc, monkeypatch):
    """Found by CI on Windows: git there checks the templates out with CRLF."""
    import devsync.cli as cli
    home, remote = pc
    fake = home / 'pkg'
    for rel in cli.MONITOR_FILES:
        dst = fake / 'templates' / 'store' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_lf(TEMPLATES.parent / rel).replace(b'\n', b'\r\n'))
    monkeypatch.setattr(cli, '__file__', str(fake / 'cli.py'))
    assert main(['monitor', 'install']) == 0
    assert b'\r' not in _remote_file(remote, '.github/heartbeat_check.py')
    assert _remote_file(remote, '.github/workflows/sync-heartbeat.yml') == _lf(WORKFLOW)


def test_the_templates_are_packaged_with_the_wheel():
    """`pipx install` users have no checkout: the templates must ship inside the package."""
    text = (REPO / "pyproject.toml").read_text()
    assert "templates/store/.github/*.py" in text and "templates/store/.github/workflows/*.yml" in text
    data = json.dumps(sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*") if p.is_file()))
    assert "heartbeat_check.py" in data and "workflows/sync-heartbeat.yml" in data
