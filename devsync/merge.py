"""Category detection + 3-way merge rules and the reconcile matrix."""
from __future__ import annotations

import json
import posixpath
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


# ── category detection ──────────────────────────────────────────

def category_for(canonical_path: str, manifest: dict) -> str:
    """Map a canonical repo path to a merge category via manifest categoryByExt.

    Basenames like `.env.local` / `.env.production` count as env even though
    their trailing "extension" isn't `.env`.
    """
    name = posixpath.basename(canonical_path)
    if name == ".env" or name.startswith(".env."):
        return "env"
    dot = name.rfind(".")
    if dot > 0 or (dot == 0 and name.count(".") > 1):
        ext = name[name.rfind("."):].lower()
        cat = manifest.get("categoryByExt", {}).get(ext)
        if cat:
            return cat
    elif dot == 0:
        cat = manifest.get("categoryByExt", {}).get(name.lower())
        if cat:
            return cat
    return "unknown"


# ── text 3-way merge (git merge-file) ───────────────────────────

def merge_text(base: bytes, ours: bytes, theirs: bytes,
               prefer: str | None = None) -> tuple[bytes, bool]:
    """3-way line merge via `git merge-file --diff3 -p`. Returns (merged, conflict).

    Additive edits union — including both sides appending/inserting at the same
    spot, which plain git flags as a conflict but the spec's worked example
    (MEMORY.md fact A + fact B) requires to auto-union. Only a genuine
    same-line divergence (both sides rewrote existing base lines) keeps both
    versions inside conflict markers — nothing is ever dropped.

    `prefer` ("ours" or "theirs") settles each such divergence by taking that
    side's lines for the hunk, so the result has no markers and is never a
    conflict. Everything that merged cleanly from the other side is still kept;
    only the contested lines are chosen. The caller keeps the losing file.
    """
    if ours == theirs:
        return ours, False
    if base == ours:
        return theirs, False
    if base == theirs:
        return ours, False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "base").write_bytes(base)
        (p / "ours").write_bytes(ours)
        (p / "theirs").write_bytes(theirs)
        r = subprocess.run(
            ["git", "merge-file", "--diff3", "-p",
             "-L", "LOCAL", "-L", "BASE", "-L", "REMOTE",
             str(p / "ours"), str(p / "base"), str(p / "theirs")],
            capture_output=True,
        )
    if r.returncode < 0 or r.returncode == 255:
        raise RuntimeError(f"git merge-file failed: {r.stderr.decode(errors='replace')}")
    if r.returncode == 0:
        return r.stdout, False
    return _resolve_diff3_blocks(r.stdout, prefer)


def _resolve_diff3_blocks(merged: bytes, prefer: str | None = None) -> tuple[bytes, bool]:
    """Post-process diff3 conflict blocks: empty-base blocks (both sides
    inserted new content at the same point) auto-union to LOCAL then REMOTE;
    non-empty-base blocks become two-way markers and count as real conflicts."""
    out: list[bytes] = []
    conflict = False
    lines = merged.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"<<<<<<<"):
            ours_l: list[bytes] = []
            base_l: list[bytes] = []
            theirs_l: list[bytes] = []
            section = ours_l
            i += 1
            while i < len(lines) and not lines[i].startswith(b">>>>>>>"):
                if lines[i].startswith(b"|||||||"):
                    section = base_l
                elif lines[i].startswith(b"======="):
                    section = theirs_l
                else:
                    section.append(lines[i])
                i += 1
            i += 1  # skip >>>>>>>
            if not base_l:
                # both sides inserted where base had nothing → union
                out.extend(ours_l)
                out.extend(theirs_l)
            elif prefer == "ours":
                out.extend(ours_l)
            elif prefer == "theirs":
                out.extend(theirs_l)
            else:
                conflict = True
                out.append(b"<<<<<<< LOCAL\n")
                out.extend(ours_l)
                out.append(b"=======\n")
                out.extend(theirs_l)
                out.append(b">>>>>>> REMOTE\n")
        else:
            out.append(line)
            i += 1
    return b"".join(out), conflict


# ── env key-aware merge ─────────────────────────────────────────

_ENV_LINE = re.compile(rb"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=")


def _parse_env(data: bytes):
    """Return ordered {key: full_line} or None if too little parses as k=v."""
    entries: dict[bytes, bytes] = {}
    meaningful = 0
    for line in data.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        meaningful += 1
        m = _ENV_LINE.match(line)
        if m:
            entries[m.group(1)] = line
    if meaningful == 0 or len(entries) < meaningful * 0.5:
        return None
    return entries


def merge_env(base: bytes, ours: bytes, theirs: bytes,
              ours_newer: bool) -> tuple[bytes, bool, str]:
    """Key-aware 3-way merge for .env-style files.

    Union of keys; per-key 3-way on the value line. Same key changed on both
    sides → newer file wins, loser recorded. Keys never disappear (anti-wipe:
    a key present in base+one side is kept). Falls back to line 3-way when the
    content doesn't parse as key=value.

    Returns (merged, conflict, loser_record).
    """
    eb, eo, et = _parse_env(base), _parse_env(ours), _parse_env(theirs)
    if eo is None or et is None or eb is None:
        merged, conflict = merge_text(base, ours, theirs)
        return merged, conflict, ""

    keys: list[bytes] = []
    for src in (eb, eo, et):
        for k in src:
            if k not in keys:
                keys.append(k)

    out: list[bytes] = []
    conflict = False
    losers: list[str] = []
    for k in keys:
        b, o, t = eb.get(k), eo.get(k), et.get(k)
        if o == t:
            line = o if o is not None else (b if (o is None and t is None) else None)
            # both deleted relative to base → anti-wipe: keep base line
            if o is None and t is None:
                line = b
        elif o == b:
            line = t if t is not None else o  # theirs changed (or deleted → keep ours/base)
            if t is None:
                line = o  # deletion never inferred
        elif t == b:
            line = o if o is not None else t
            if o is None:
                line = t
        else:
            # both changed differently
            if o is None:
                line = t
            elif t is None:
                line = o
            else:
                conflict = True
                winner, loser = (o, t) if ours_newer else (t, o)
                line = winner
                losers.append(
                    f"{k.decode(errors='replace')}: lost value from "
                    f"{'REMOTE' if ours_newer else 'LOCAL'}: "
                    f"{loser.decode(errors='replace').strip()}"
                )
        if line is not None:
            if not line.endswith(b"\n"):
                line += b"\n"
            out.append(line)
    return b"".join(out), conflict, "\n".join(losers)


# ── structured (JSON) merge ─────────────────────────────────────

def _normalize_json(data: bytes) -> bytes | None:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def merge_structured_json(base: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
    """Normalize (stable key order + indent) then 3-way merge; validate the
    result parses. Invalid merge → keep local, flag conflict."""
    nb, no, nt = _normalize_json(base), _normalize_json(ours), _normalize_json(theirs)
    if no is None and nt is None:
        # Neither side is JSON: this is YAML or TOML, which share the category.
        # Validating the merge as JSON would fail every time and report a
        # conflict for a change only one side made. Merge it as text.
        return merge_text(base, ours, theirs)
    if nb is None or no is None or nt is None:
        merged, conflict = merge_text(base, ours, theirs)
        if _try_parse(merged):
            return merged, conflict
        return ours, True
    merged, conflict = merge_text(nb, no, nt)
    if conflict or not _try_parse(merged):
        return no, True
    return merged, False


def _try_parse(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8-sig"))
        return True
    except (ValueError, UnicodeDecodeError):
        return False


# ── reconcile matrix ───────────────────────────

@dataclass
class ReconcileResult:
    merged: bytes | None      # content all three locations should hold (None = no file)
    conflict: bool = False    # a .conflict sidecar must be written
    loser: bytes | None = None    # replaced content to preserve in trash
    loser_note: str = ""      # human-readable record for the sidecar
    auto_resolved: bool = False   # a conflict was settled by policy; `loser` holds the other side


def _norm_eol(data: bytes | None) -> bytes | None:
    return None if data is None else data.replace(b"\r\n", b"\n")


MARK_OPEN, MARK_MID, MARK_CLOSE = b"<<<<<<< LOCAL", b"=======", b">>>>>>> REMOTE"


def has_markers(data: bytes) -> bool:
    """True if `data` still holds a conflict block written by merge_text."""
    lines = [l.rstrip(b"\r\n") for l in data.splitlines()]
    return MARK_OPEN in lines and MARK_CLOSE in lines


def resolve_markers(data: bytes, keep: str) -> bytes:
    """Settle every conflict block in `data`: keep = local | remote | both."""
    if keep not in ("local", "remote", "both"):
        raise ValueError(f"keep must be local, remote or both, not {keep!r}")
    out: list[bytes] = []
    side = None            # None = outside a block, else 'local' / 'remote'
    for line in data.splitlines(keepends=True):
        bare = line.rstrip(b"\r\n")
        if side is None and bare == MARK_OPEN:
            side = "local"
        elif side == "local" and bare == MARK_MID:
            side = "remote"
        elif side == "remote" and bare == MARK_CLOSE:
            side = None
        elif side is None or keep in (side, "both"):
            out.append(line)
    return b"".join(out)


def reconcile(category: str, base: bytes | None, live: bytes | None,
              repo: bytes | None, ours_newer: bool,
              policy: str = "ask") -> ReconcileResult:
    """Merge the three copies, then apply the store's conflict policy.

    policy "ask"   -- a real conflict is reported; the engine writes a sidecar and
                     withholds the push until a person resolves it. The original
                     behaviour, and what a store with no `conflictPolicy` gets.
    policy "newer" -- a real conflict is settled at once in favour of the newer
                     side, hunk by hunk. Nothing is lost: the other side's whole
                     file comes back as `loser` and the engine puts it in trash.
    """
    res = _reconcile(category, base, live, repo, ours_newer)
    if policy != "newer" or not res.conflict:
        return res
    if category in ("text", "env", "structured"):
        base, live, repo = _norm_eol(base), _norm_eol(live), _norm_eol(repo)
    b = base if base is not None else b""
    prefer = "ours" if ours_newer else "theirs"
    winner, loser = (live, repo) if ours_newer else (repo, live)
    merged = res.merged                      # env and unknown already hold the newer value
    if category == "text":
        merged, _ = merge_text(b, live, repo, prefer=prefer)
    elif category == "structured":
        nb, no, nt = _normalize_json(b), _normalize_json(live), _normalize_json(repo)
        if nb is not None and no is not None and nt is not None:
            merged, _ = merge_text(nb, no, nt, prefer=prefer)
        else:
            merged, _ = merge_text(b, live, repo, prefer=prefer)
        if (no is not None or nt is not None) and not _try_parse(merged):
            merged = winner            # a hunk-level pick that is not valid JSON is no merge
    note = res.loser_note or f"{category}: both sides changed the same lines"
    return ReconcileResult(merged=merged, conflict=False, loser=loser,
                           loser_note=note, auto_resolved=True)


def _reconcile(category: str, base: bytes | None, live: bytes | None,
               repo: bytes | None, ours_newer: bool) -> ReconcileResult:
    """Merge base/live/repo (each possibly absent) per the category rule.

    Deletion is NEVER inferred from absence: a file present anywhere
    survives everywhere.

    Text-like categories are CRLF→LF normalized before comparing/merging: the
    store is LF (core.autocrlf=input) while Windows live files are often CRLF,
    and without this every line would spuriously differ. Merged output is LF.
    """
    if category in ("text", "env", "structured"):
        base, live, repo = _norm_eol(base), _norm_eol(live), _norm_eol(repo)
    if live is None and repo is None:
        # exists only in base (or nowhere) — resurrect from base if present
        return ReconcileResult(merged=base)
    if live is None:
        return ReconcileResult(merged=repo)
    if repo is None:
        return ReconcileResult(merged=live)
    if live == repo:
        return ReconcileResult(merged=live)

    b = base if base is not None else b""

    if category == "text":
        merged, conflict = merge_text(b, live, repo)
        return ReconcileResult(merged=merged, conflict=conflict)
    if category == "env":
        merged, conflict, losers = merge_env(b, live, repo, ours_newer)
        return ReconcileResult(merged=merged, conflict=conflict, loser_note=losers)
    if category == "structured":
        merged, conflict = merge_structured_json(b, live, repo)
        return ReconcileResult(merged=merged, conflict=conflict)
    if category == "binary":
        winner, loser = (live, repo) if ours_newer else (repo, live)
        changed_live = live != b
        changed_repo = repo != b
        if changed_live and not changed_repo:
            return ReconcileResult(merged=live)
        if changed_repo and not changed_live:
            return ReconcileResult(merged=repo)
        return ReconcileResult(merged=winner, loser=loser,
                               loser_note="binary: newer-mtime won; loser preserved")
    # unknown: newer-wins + sidecar if both changed since base
    changed_live = live != b
    changed_repo = repo != b
    if changed_live and not changed_repo:
        return ReconcileResult(merged=live)
    if changed_repo and not changed_live:
        return ReconcileResult(merged=repo)
    winner, loser = (live, repo) if ours_newer else (repo, live)
    return ReconcileResult(merged=winner, conflict=True, loser=loser,
                           loser_note="unknown category: both sides changed; newer won")
