"""Collectors: enumerate the managed set on this PC.

Both collectors return {canonical_path: local_Path}.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from devsync.paths import Machine, local_to_canonical

# Non-project dirs under the dev root that must never be scanned. This is the
# LEGACY default: it applies to a store whose manifest has no `skipProjectDirs`
# key, i.e. every store created before the key existed. New stores list their own.
SKIP_PROJECT_DIRS = {"CascadeArchive", "claude-config", "devsync-store"}


def _same_dir(a: Path, b: Path) -> bool:
    import os
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _match_any(rel: str, globs: list[str]) -> bool:
    """fnmatch against the full relpath and its basename; `dir/**` matches contents."""
    name = rel.rsplit("/", 1)[-1]
    for g in globs:
        if g.endswith("/**"):
            if rel.startswith(g[:-3] + "/"):
                return True
        elif fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g):
            return True
    return False


def is_blocked(canon: str, globs: list[str]) -> bool:
    """Blocklist test for a canonical path.

    repos/ paths match project-relative, as the repo collector always has;
    claude/ paths match the full canonical path (`claude/skills/synced/*/manifest.json`)
    or the basename (`*.conflict`).
    """
    if canon.startswith("repos/"):
        parts = canon.split("/", 2)
        if len(parts) == 3:
            return _match_any(parts[2], globs)
    return _match_any(canon, globs)


def _read_override(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line.replace("\\", "/"))
    return lines


def _ignored_candidates(project: Path) -> list[str]:
    """git ls-files --others --ignored --directory: gitignored paths, with
    ignored directories collapsed to one entry (never descended — the speed fix)."""
    r = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--others", "--ignored",
         "--exclude-standard", "--directory", "-z"],
        capture_output=True,
    )
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk_pruned(top: Path, project: Path, generated: set) -> list[str]:
    """Expand an ignored directory without ever descending into generated
    dirs (node_modules nested under an ignored parent was the perf hang)."""
    import os
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in generated]
        for name in filenames:
            f = Path(dirpath) / name
            out.append(f.relative_to(project).as_posix())
    return out


def collect_repo_files(machine: Machine, manifest: dict,
                       exclude: tuple = ()) -> dict[str, Path]:
    """Collector A: precious gitignored files across every git project under the dev root.

    `exclude` is a list of directories never treated as a project whatever they are
    called -- the engine passes its own store checkout, which lives under the dev root
    by default and must not be scanned even when the user renamed it.
    """
    out: dict[str, Path] = {}
    root = machine.cascade_root
    if not root.is_dir():
        return out
    generated = set(manifest.get("generatedDirs", []))
    blocklist = manifest.get("blocklistGlobs", [])
    allowlist = manifest.get("allowlistGlobs", [])
    capture_all = manifest.get("captureAllNonBlocked", False)
    skip = set(manifest.get("skipProjectDirs", SKIP_PROJECT_DIRS))

    for project in sorted(root.iterdir()):
        if not project.is_dir() or project.name in skip:
            continue
        if any(_same_dir(project, e) for e in exclude):
            continue
        if not (project / ".git").exists():
            continue
        sync_ignore = _read_override(project / ".syncignore")
        sync_include = _read_override(project / ".syncinclude")

        files: list[str] = []
        for cand in _ignored_candidates(project):
            cand = cand.replace("\\", "/")
            first = cand.split("/", 1)[0]
            if first in generated:
                continue
            if cand.endswith("/"):
                d = project / cand.rstrip("/")
                files.extend(_walk_pruned(d, project, generated))
            else:
                files.append(cand)

        for rel in files:
            if any(seg in generated for seg in rel.split("/")[:-1]):
                continue
            if _match_any(rel, blocklist):
                continue
            included = _match_any(rel, sync_include)
            if not included:
                if _match_any(rel, sync_ignore):
                    continue
                if not capture_all and not _match_any(rel, allowlist):
                    continue
            local = project / rel
            if not local.is_file():
                continue
            canon = f"repos/{project.name}/{rel}"
            out[canon] = local
    return out


def collect_claude_files(machine: Machine, manifest: dict) -> dict[str, Path]:
    """Collector B: ~/.claude global files, config trees, and per-project memory."""
    out: dict[str, Path] = {}
    ch = machine.claude_home
    cp = manifest.get("claudePaths", {})

    for name in cp.get("global", []):
        f = ch / name
        if f.is_file():
            out[f"claude/global/{name}"] = f

    for tree in cp.get("trees", []):
        d = ch / tree
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file():
                rel = f.relative_to(ch).as_posix()
                out[f"claude/{rel}"] = f

    projects = ch / "projects"
    if projects.is_dir():
        prefix = machine.project_key_prefix
        # Two key dirs can be the same physical directory (a symlink or junction
        # alias). The files must be collected under ONE canonical key, or the
        # store grows a duplicate tree that every machine then materializes.
        # sorted() puts the bare prefix before every "<prefix>-<suffix>", so the
        # primary key always claims the directory first.
        seen_real: set = set()
        for key_dir in sorted(projects.iterdir()):
            name = key_dir.name
            if name != prefix and not name.startswith(prefix + "-"):
                continue
            mem = key_dir / "memory"
            if not mem.is_dir():
                continue
            import os
            try:
                real = os.path.normcase(os.path.realpath(str(mem)))
            except OSError:
                real = os.path.normcase(os.path.abspath(str(mem)))
            if real in seen_real:
                continue
            seen_real.add(real)
            for f in mem.glob("*.md"):
                if f.is_file():
                    canon = local_to_canonical(f, machine)
                    if canon:
                        out[canon] = f
    blocklist = manifest.get("blocklistGlobs", [])
    out = {c: p for c, p in out.items() if not is_blocked(c, blocklist)}
    if manifest.get("configFilters"):
        # New stores only (the key is absent from older manifests): login files and
        # caches inside a synced folder are not collected.
        out = {c: p for c, p in out.items() if not _filtered(c, p)}
    return out


# Files that hold credentials or session history in one agent or another. Never
# collected from an agent home, whatever the store's lists say: a user adding a
# whole folder as a tree must not publish a login by accident.
NEVER_SYNC = {"auth.json", "oauth_creds.json", "credentials.json", ".credentials.json",
              "history.jsonl", ".env"}


# Cache and tooling folders that turn up inside config folders. Deliberately much
# shorter than the repo `generatedDirs` list: names like `lib`, `bin` and `dist` are
# ordinary folder names inside a skill or a hook.
CONFIG_PRUNE_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".git",
                     ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}


def _filtered(canon: str, path: Path) -> bool:
    return path.name in NEVER_SYNC or bool(set(canon.split("/")[:-1]) & CONFIG_PRUNE_DIRS)


def collect_home_files(machine: Machine, manifest: dict) -> dict[str, Path]:
    """Collector C: the listed files and folders of every other agent home that is
    present on this PC. Canonical path: agents/<name>/<path inside that home>."""
    out: dict[str, Path] = {}
    blocklist = manifest.get("blocklistGlobs", [])
    for spec in manifest.get("homes", []) or []:
        name = spec.get("name")
        home = machine.homes.get(name)
        if not name or home is None:
            continue
        home = Path(home)
        found: list[Path] = []
        for g in spec.get("global", []) or []:
            f = home / g
            if f.is_file():
                found.append(f)
        for tree in spec.get("trees", []) or []:
            d = home / tree
            if d.is_dir():
                found.extend(f for f in d.rglob("*") if f.is_file())
        for f in found:
            canon = f"agents/{name}/{f.relative_to(home).as_posix()}"
            if _filtered(canon, f):
                continue
            if not is_blocked(canon, blocklist):
                out[canon] = f
    return out
