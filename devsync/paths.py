"""Canonical ↔ local path mapping.

Canonical layout inside devsync-store:
    repos/<project>/<relpath>                      ← <cascadeRoot>/<project>/<relpath>
    claude/global/<file>                           ← <claudeHome>/<file>
    claude/skills|commands|hooks|lib/<relpath>     ← <claudeHome>/<same>
    claude/memory/<canonicalKey>/<file>            ← <claudeHome>/projects/<localKey>/memory/<file>
    agents/<name>/<relpath>                        ← <that agent's home on this PC>/<relpath>

<localKey> = <projectKeyPrefix> or <projectKeyPrefix>-<suffix>;
<canonicalKey> = "<keyRoot>" or "<keyRoot>-<suffix>" — username-free. <keyRoot> is the
store's `memoryKeyRoot` (manifest.json). A store without that key keeps the original
"CascadeProjects" label, so every store written before the setting existed is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CLAUDE_TREES = ("skills", "commands", "hooks", "lib")
CANONICAL_KEY_ROOT = "CascadeProjects"


class HomeNotPresent(ValueError):
    """A store path belongs to an agent whose folder does not exist on this PC.

    Not an error: a machine that does not use that agent simply does not take
    part in syncing it. Callers skip the path and leave the store copy alone.
    """


@dataclass(frozen=True)
class Machine:
    hostname: str
    cascade_root: Path
    claude_home: Path
    project_key_prefix: str
    # Label of the dev root inside the store. A per-STORE setting (manifest
    # `memoryKeyRoot`), carried here so path mapping needs no manifest argument.
    memory_key_root: str = CANONICAL_KEY_ROOT
    # Other agents' config folders PRESENT on this PC: {name: Path}. Resolved by the
    # engine from the store's `homes` list; empty for a store that has none.
    homes: dict = field(default_factory=dict, compare=False, hash=False)

    @classmethod
    def load(cls, path: Path) -> "Machine":
        import json
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return cls(
            hostname=data["hostname"],
            cascade_root=Path(data["cascadeRoot"]),
            claude_home=Path(data["claudeHome"]),
            project_key_prefix=data["projectKeyPrefix"],
        )


def _rel(path: Path, root: Path) -> str | None:
    # Deliberately does NOT resolve symlinks: ~/.claude/projects/<key> may be a
    # symlink to another folder, and
    # the project-key mapping must operate on the name as addressed, not the
    # link target. Case-insensitive compare for Windows.
    import os
    p = os.path.normcase(os.path.abspath(str(path)))
    r = os.path.normcase(os.path.abspath(str(root)))
    if not p.startswith(r.rstrip("\\/") + os.sep):
        return None
    rel = os.path.abspath(str(path))[len(r.rstrip("\\/")) + 1:]
    return rel.replace("\\", "/")


def local_to_canonical(local: Path, machine: Machine) -> str | None:
    """Map a local absolute path to its canonical repo path, or None if unmanaged."""
    rel = _rel(local, machine.claude_home)
    if rel is not None:
        parts = rel.split("/")
        if parts[0] == "projects":
            # projects/<localKey>/memory/<file...>
            if len(parts) >= 4 and parts[2] == "memory":
                local_key = parts[1]
                if local_key == machine.project_key_prefix:
                    canon_key = machine.memory_key_root
                elif local_key.startswith(machine.project_key_prefix + "-"):
                    suffix = local_key[len(machine.project_key_prefix) + 1:]
                    canon_key = f"{machine.memory_key_root}-{suffix}"
                else:
                    return None
                return "claude/memory/" + canon_key + "/" + "/".join(parts[3:])
            return None
        if len(parts) > 1:
            # Any folder under the agent home. Which folders are COLLECTED is the
            # store's `claudePaths.trees`; the mapping itself must not also keep a
            # list, or a tree added there would upload and never come back down.
            return "claude/" + rel
        if len(parts) == 1:
            return "claude/global/" + parts[0]
        return None

    for name, home in machine.homes.items():
        rel = _rel(local, Path(home))
        if rel is not None:
            return f"agents/{name}/{rel}"

    rel = _rel(local, machine.cascade_root)
    if rel is not None and "/" in rel:
        return "repos/" + rel
    return None


def canonical_to_local(canonical: str, machine: Machine) -> Path:
    """Expand a canonical repo path to this machine's local absolute path."""
    parts = canonical.split("/")
    if len(parts) < 3 or not all(parts):
        # every store path is <area>/<group>/<file...>; anything shorter is a
        # folder or a typo, never a file
        raise ValueError(f"unrecognized canonical path: {canonical}")
    if parts[0] == "repos":
        return machine.cascade_root.joinpath(*parts[1:])
    if parts[0] == "claude":
        if parts[1] == "global":
            return machine.claude_home.joinpath(*parts[2:])
        if parts[1] == "memory":
            canon_key = parts[2]
            if canon_key == machine.memory_key_root:
                local_key = machine.project_key_prefix
            elif canon_key.startswith(machine.memory_key_root + "-"):
                suffix = canon_key[len(machine.memory_key_root) + 1:]
                local_key = f"{machine.project_key_prefix}-{suffix}"
            else:
                raise ValueError(f"unexpected memory key: {canon_key}")
            return machine.claude_home.joinpath("projects", local_key, "memory", *parts[3:])
        if len(parts) >= 3 and parts[1] not in ("global", "memory"):
            return machine.claude_home.joinpath(*parts[1:])
    if parts[0] == "agents" and len(parts) >= 3:
        home = machine.homes.get(parts[1])
        if home is None:
            raise HomeNotPresent(f"agent home {parts[1]!r} is not present on this machine")
        return Path(home).joinpath(*parts[2:])
    raise ValueError(f"unrecognized canonical path: {canonical}")
