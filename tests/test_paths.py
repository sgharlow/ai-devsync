"""Canonical path mapping tests."""
from pathlib import Path

from devsync.paths import Machine, canonical_to_local, local_to_canonical

MACHINE = Machine(
    hostname="PC-A",
    cascade_root=Path("C:/Users/Alex.Smith/CascadeProjects"),
    claude_home=Path("C:/Users/Alex.Smith/.claude"),
    project_key_prefix="C--Users-Alex-Smith-CascadeProjects",
)


def test_repo_file_to_canonical():
    local = Path("C:/Users/Alex.Smith/CascadeProjects/billing-app/.env")
    assert local_to_canonical(local, MACHINE) == "repos/billing-app/.env"


def test_repo_nested_file_to_canonical():
    local = Path("C:/Users/Alex.Smith/CascadeProjects/foo/certs/dev.pem")
    assert local_to_canonical(local, MACHINE) == "repos/foo/certs/dev.pem"


def test_claude_global_to_canonical():
    local = Path("C:/Users/Alex.Smith/.claude/CLAUDE.md")
    assert local_to_canonical(local, MACHINE) == "claude/global/CLAUDE.md"


def test_claude_tree_to_canonical():
    local = Path("C:/Users/Alex.Smith/.claude/skills/verify/SKILL.md")
    assert local_to_canonical(local, MACHINE) == "claude/skills/verify/SKILL.md"


def test_memory_to_canonical_strips_username():
    local = Path(
        "C:/Users/Alex.Smith/.claude/projects/"
        "C--Users-Alex-Smith-CascadeProjects-billing-app/memory/MEMORY.md"
    )
    assert local_to_canonical(local, MACHINE) == "claude/memory/CascadeProjects-billing-app/MEMORY.md"


def test_memory_root_project_key():
    local = Path(
        "C:/Users/Alex.Smith/.claude/projects/"
        "C--Users-Alex-Smith-CascadeProjects/memory/MEMORY.md"
    )
    assert local_to_canonical(local, MACHINE) == "claude/memory/CascadeProjects/MEMORY.md"


def test_roundtrip_all_kinds():
    for canon in [
        "repos/billing-app/.env",
        "claude/global/settings.json",
        "claude/skills/verify/SKILL.md",
        "claude/memory/CascadeProjects-billing-app/MEMORY.md",
        "claude/memory/CascadeProjects/MEMORY.md",
    ]:
        local = canonical_to_local(canon, MACHINE)
        assert local_to_canonical(local, MACHINE) == canon, canon


def test_canonical_to_local_memory():
    local = canonical_to_local("claude/memory/CascadeProjects-foo/MEMORY.md", MACHINE)
    assert local == Path(
        "C:/Users/Alex.Smith/.claude/projects/"
        "C--Users-Alex-Smith-CascadeProjects-foo/memory/MEMORY.md"
    )


def test_other_machine_same_canonical():
    other = Machine(
        hostname="PC-B",
        cascade_root=Path("C:/Users/asmith/CascadeProjects"),
        claude_home=Path("C:/Users/asmith/.claude"),
        project_key_prefix="C--Users-asmith-CascadeProjects",
    )
    canon = "claude/memory/CascadeProjects-billing-app/MEMORY.md"
    assert canonical_to_local(canon, other) == Path(
        "C:/Users/asmith/.claude/projects/"
        "C--Users-asmith-CascadeProjects-billing-app/memory/MEMORY.md"
    )


def test_unmanaged_path_returns_none():
    assert local_to_canonical(Path("C:/Windows/system32/foo.txt"), MACHINE) is None
