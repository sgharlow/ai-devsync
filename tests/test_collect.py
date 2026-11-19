"""Collector tests. Uses real temp git repos and a fake ~/.claude."""
import subprocess
from pathlib import Path

import pytest

from devsync.collect import collect_claude_files, collect_repo_files
from devsync.paths import Machine

MANIFEST = {
    "generatedDirs": ["node_modules", ".venv", "dist", "build", "__pycache__",
                      ".next", "coverage", ".turbo"],
    "blocklistGlobs": ["*.log", "*.tmp"],
    "allowlistGlobs": [".env*", "*.pem", "*.key", "certs/**", "secrets/**",
                       "*.sqlite", "config/local.*"],
    "captureAllNonBlocked": False,
    "claudePaths": {
        "global": ["CLAUDE.md", "settings.json", "settings.local.json", "keybindings.json"],
        "trees": ["skills", "commands", "hooks", "lib"],
        "memoryGlob": "projects/*/memory/*.md",
    },
}


def _git(cwd: Path, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path):
    """cascade_root with one git project containing ignored files of all kinds."""
    root = tmp_path / "CascadeProjects"
    proj = root / "myproj"
    proj.mkdir(parents=True)
    _git(proj, "init")
    (proj / ".gitignore").write_text(
        ".env*\nnode_modules/\n*.log\ncerts/\n*.sqlite\nnotes.txt\n"
    )
    (proj / "app.py").write_text("print('hi')\n")
    _git(proj, "add", ".")
    _git(proj, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    # precious ignored files
    (proj / ".env").write_text("KEY=1\n")
    (proj / ".env.local").write_text("KEY=2\n")
    (proj / "certs").mkdir()
    (proj / "certs" / "dev.pem").write_text("PEM\n")
    (proj / "data.sqlite").write_bytes(b"sqlite")
    # noise that must be excluded
    (proj / "node_modules" / "pkg").mkdir(parents=True)
    (proj / "node_modules" / "pkg" / "index.js").write_text("x")
    (proj / "debug.log").write_text("log")
    (proj / "notes.txt").write_text("not allowlisted")
    # a non-git folder must be skipped entirely
    (root / "not-a-repo").mkdir()
    (root / "not-a-repo" / ".env").write_text("SKIP=1\n")
    return root


def machine_for(root: Path, claude_home: Path) -> Machine:
    return Machine(
        hostname="TEST-PC",
        cascade_root=root,
        claude_home=claude_home,
        project_key_prefix="C--Users-Test-CascadeProjects",
    )


def test_collect_repo_files_allowlist(workspace, tmp_path):
    m = machine_for(workspace, tmp_path / ".claude")
    got = collect_repo_files(m, MANIFEST)
    canon = sorted(got)
    assert "repos/myproj/.env" in canon
    assert "repos/myproj/.env.local" in canon
    assert "repos/myproj/certs/dev.pem" in canon
    assert "repos/myproj/data.sqlite" in canon
    # excluded
    assert not any("node_modules" in c for c in canon)
    assert not any(c.endswith(".log") for c in canon)
    assert "repos/myproj/notes.txt" not in canon
    assert not any("not-a-repo" in c for c in canon)


def test_collect_repo_files_syncignore_override(workspace, tmp_path):
    (workspace / "myproj" / ".syncignore").write_text(".env.local\n")
    m = machine_for(workspace, tmp_path / ".claude")
    got = collect_repo_files(m, MANIFEST)
    assert "repos/myproj/.env" in got
    assert "repos/myproj/.env.local" not in got


def test_collect_repo_files_syncinclude_override(workspace, tmp_path):
    (workspace / "myproj" / ".syncinclude").write_text("notes.txt\n")
    m = machine_for(workspace, tmp_path / ".claude")
    got = collect_repo_files(m, MANIFEST)
    assert "repos/myproj/notes.txt" in got


@pytest.fixture
def claude_home(tmp_path):
    ch = tmp_path / ".claude"
    (ch / "skills" / "verify").mkdir(parents=True)
    (ch / "commands").mkdir()
    (ch / "projects" / "C--Users-Test-CascadeProjects" / "memory").mkdir(parents=True)
    (ch / "projects" / "C--Users-Test-CascadeProjects-foo" / "memory").mkdir(parents=True)
    (ch / "CLAUDE.md").write_text("global\n")
    (ch / "settings.json").write_text("{}\n")
    (ch / "skills" / "verify" / "SKILL.md").write_text("skill\n")
    (ch / "commands" / "go.md").write_text("cmd\n")
    (ch / "projects" / "C--Users-Test-CascadeProjects" / "memory" / "MEMORY.md").write_text("m\n")
    (ch / "projects" / "C--Users-Test-CascadeProjects-foo" / "memory" / "topic.md").write_text("t\n")
    # noise: non-memory project files must not be collected
    (ch / "projects" / "C--Users-Test-CascadeProjects" / "session.jsonl").write_text("x\n")
    (ch / "history.jsonl").write_text("x\n")
    return ch


def test_collect_claude_files(claude_home, tmp_path):
    m = machine_for(tmp_path / "CascadeProjects", claude_home)
    got = collect_claude_files(m, MANIFEST)
    canon = sorted(got)
    assert "claude/global/CLAUDE.md" in canon
    assert "claude/global/settings.json" in canon
    assert "claude/skills/verify/SKILL.md" in canon
    assert "claude/commands/go.md" in canon
    assert "claude/memory/CascadeProjects/MEMORY.md" in canon
    assert "claude/memory/CascadeProjects-foo/topic.md" in canon
    assert not any("jsonl" in c for c in canon)


def test_collect_claude_skips_other_machine_keys(claude_home, tmp_path):
    other = claude_home / "projects" / "C--Users-Other-CascadeProjects-bar" / "memory"
    other.mkdir(parents=True)
    (other / "MEMORY.md").write_text("other machine's key\n")
    m = machine_for(tmp_path / "CascadeProjects", claude_home)
    got = collect_claude_files(m, MANIFEST)
    assert not any("bar" in c for c in got)


def test_collect_never_descends_nested_generated_dirs(workspace, tmp_path):
    # generated dirs below the project root (web/node_modules) must be
    # excluded by segment, not only by first path component — and never
    # expanded (the hang: rglob over a huge nested node_modules)
    proj = workspace / "myproj"
    nested = proj / "web" / "node_modules" / "dep"
    nested.mkdir(parents=True)
    (nested / "package.env").write_text("X=1\n")  # would match .env* by name? no — allowlist .env*
    (nested / ".env").write_text("TRAP=1\n")      # allowlisted name inside generated dir
    (proj / ".gitignore").write_text(
        ".env*\nnode_modules/\n*.log\ncerts/\n*.sqlite\nnotes.txt\nweb/\n"
    )
    m = machine_for(workspace, tmp_path / ".claude")
    import time
    t0 = time.time()
    got = collect_repo_files(m, MANIFEST)
    assert time.time() - t0 < 10
    assert not any("node_modules" in c for c in got)


def test_collect_claude_dedupes_symlinked_project_keys(claude_home, tmp_path):
    # the primary key dir may be a symlink to an alias dir (for example
    # C--…-CascadeProjects → CascadeProjects-canonical); the same physical
    # memory files must be collected under ONE canonical key — the primary
    import os
    alias = claude_home / "projects" / "C--Users-Test-CascadeProjects-alias"
    primary = claude_home / "projects" / "C--Users-Test-CascadeProjects"
    target = primary.resolve()
    alias_created = False
    try:
        os.symlink(target, alias, target_is_directory=True)
        alias_created = True
    except OSError:
        # No symlink privilege (the usual case on Windows). A directory junction
        # needs none and is the same hazard: two names, one physical directory.
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                           capture_output=True) if os.name == "nt" else None
        if r is None or r.returncode != 0:
            pytest.skip("no symlink privilege and no junction support")
    m = machine_for(tmp_path / "CascadeProjects", claude_home)
    got = collect_claude_files(m, MANIFEST)
    assert "claude/memory/CascadeProjects/MEMORY.md" in got
    assert "claude/memory/CascadeProjects-alias/MEMORY.md" not in got
    if alias_created:
        alias.unlink()
    elif alias.exists():
        os.rmdir(alias)   # removes the junction, not its target


def test_collect_repo_files_returns_local_paths(workspace, tmp_path):
    m = machine_for(workspace, tmp_path / ".claude")
    got = collect_repo_files(m, MANIFEST)
    for canon, local in got.items():
        assert local.is_file(), f"{canon} → {local} missing"


def test_collect_claude_files_honours_blocklist(claude_home, tmp_path):
    # Claude-managed per-machine state under skills/synced, plus a sidecar.
    bucket = claude_home / "skills" / "synced" / "abc_def"
    (bucket / "docx").mkdir(parents=True)
    (bucket / "manifest.json").write_text("{}\n")
    (bucket / ".last-complete-round").write_text("x cli\n")
    (bucket / "docx" / "SKILL.md").write_text("skill\n")
    (claude_home / "skills" / "verify" / "SKILL.md.conflict").write_text("sidecar\n")
    manifest = dict(MANIFEST, blocklistGlobs=[
        "*.conflict",
        "claude/skills/synced/*/manifest.json",
        "claude/skills/synced/*/.last-complete-round",
    ])
    m = machine_for(tmp_path / "CascadeProjects", claude_home)
    got = collect_claude_files(m, manifest)
    assert "claude/skills/synced/abc_def/docx/SKILL.md" in got
    assert "claude/skills/verify/SKILL.md" in got
    assert "claude/skills/synced/abc_def/manifest.json" not in got
    assert "claude/skills/synced/abc_def/.last-complete-round" not in got
    assert "claude/skills/verify/SKILL.md.conflict" not in got
