"""The convergent sync engine + tombstones/trash + bootstrap."""
from __future__ import annotations

import json
import os
import copy
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import devsync.crypto as crypto
from devsync._version import __version__
from devsync.collect import (collect_claude_files, collect_home_files,
                             collect_repo_files, is_blocked)
from devsync.merge import category_for, has_markers, reconcile, resolve_markers
from devsync.paths import (CANONICAL_KEY_ROOT, HomeNotPresent, Machine,
                           canonical_to_local, local_to_canonical)

DEFAULT_MANIFEST = {
    "version": 1,
    "generatedDirs": [
        "node_modules", ".venv", "venv", "env", "dist", "build", "out",
        "bin", "obj", "target", ".next", ".nuxt", ".gradle", ".idea", ".vs",
        "__pycache__", "vendor", "packages", "coverage", ".cache",
        ".parcel-cache", ".turbo", ".angular", ".svelte-kit",
    ],
    "blocklistGlobs": ["*.log", "*.tmp", "*.conflict", "nul",
                       # Claude-managed, refreshed per machine: never mergeable
                       "claude/skills/synced/*/manifest.json",
                       "claude/skills/synced/*/.last-complete-round"],
    "allowlistGlobs": [
        ".env*", "*.local.*", "*.pem", "*.key", "*.crt", "*.pfx", "*.p12",
        "config/local.*", "certs/**", "secrets/**", "*.sqlite", "*.sqlite3",
        "appsettings.*.json",
    ],
    "captureAllNonBlocked": False,
    "claudePaths": {
        "global": ["CLAUDE.md", "settings.json", "settings.local.json",
                   "keybindings.json"],
        "trees": ["skills", "commands", "hooks", "lib"],
        "memoryGlob": "projects/*/memory/*.md",
    },
    "categoryByExt": {
        ".md": "text", ".txt": "text",
        ".env": "env", ".ini": "env", ".properties": "env",
        ".json": "structured", ".yaml": "structured", ".yml": "structured",
        ".toml": "structured",
        ".png": "binary", ".jpg": "binary", ".jpeg": "binary", ".gif": "binary",
        ".ico": "binary", ".pdf": "binary", ".pfx": "binary", ".p12": "binary",
        ".sqlite": "binary", ".sqlite3": "binary", ".pem": "binary",
        ".key": "binary", ".crt": "binary",
    },
    "trashRetentionDays": 90,
}

STORE_META_FILES = {"manifest.json", "tombstones.json", ".gitattributes"}

# Manifest keys added after the first stores were seeded. A store whose manifest
# lacks one behaves exactly as it did before the key existed -- these are the
# values the code used when they were constants. Never change them: every
# existing store depends on them silently.
LEGACY_MEMORY_KEY_ROOT = CANONICAL_KEY_ROOT      # "CascadeProjects"
LEGACY_SYNC_REPO_FILES = True
LEGACY_CONFLICT_POLICY = "ask"
CONFLICT_POLICIES = ("ask", "newer")

# What `devsync init --seed` writes for a NEW store, on top of DEFAULT_MANIFEST.
# Existing stores never see these: their manifest.json is already written.
NEW_STORE_MEMORY_KEY_ROOT = "projects"
NEW_STORE_EXTRA_TREES = ["agents", "rules", "output-styles"]
# Skills that claude.ai syncs down itself. Each machine refreshes them on its own
# schedule, so they diverge by design and can never be merged.
NEW_STORE_EXTRA_BLOCKLIST = ["claude/skills/synced/**"]


# Other coding agents a new store knows about. Each lists only instruction and
# prompt material -- never that agent's settings or MCP config, which commonly hold
# tokens; add those yourself if yours do not. A home is synced on a PC only if its
# folder exists there, so listing an agent you do not use costs nothing.
# Locations taken from each vendor's documentation.
KNOWN_HOMES = [
    {"name": "codex", "default": "~/.codex", "env": "CODEX_HOME",
     "global": ["AGENTS.md"], "trees": []},
    {"name": "gemini", "default": "~/.gemini", "env": "",
     "global": ["GEMINI.md"], "trees": ["commands"]},
    {"name": "kiro", "default": "~/.kiro", "env": "",
     "global": [], "trees": ["steering", "agents", "skills", "hooks"]},
]
HOME_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,31}$"
RESERVED_HOME_NAMES = {"claude", "global", "memory"}


def resolve_homes(manifest: dict, overrides: dict | None = None) -> dict:
    """{name: Path} for every agent home in the store's list that EXISTS on this PC.

    Where it is looked for, first match wins: this PC's own setting
    (`devsync home path`), the agent's environment variable, its default folder.
    """
    import re
    out = {}
    overrides = overrides or {}
    for spec in manifest.get("homes", []) or []:
        name = spec.get("name") or ""
        if not re.match(HOME_NAME_RE, name) or name in RESERVED_HOME_NAMES:
            continue
        raw = (overrides.get(name) or (os.environ.get(spec["env"]) if spec.get("env") else None)
               or spec.get("default"))
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_dir():
            out[name] = path
    return out


def new_store_manifest(sync_repo_files: bool = False,
                       memory_key_root: str = NEW_STORE_MEMORY_KEY_ROOT,
                       skip_project_dirs: list | None = None) -> dict:
    """The manifest for a store being seeded now.

    `sync_repo_files` defaults to False: gitignored files are secrets (.env, keys,
    certs) and the store holds them unencrypted, so a new user opts in knowingly.
    """
    m = copy.deepcopy(DEFAULT_MANIFEST)
    m["syncRepoFiles"] = bool(sync_repo_files)
    m["memoryKeyRoot"] = memory_key_root
    m["skipProjectDirs"] = list(skip_project_dirs or [])
    # Settle a real conflict in favour of the newer side instead of stopping for
    # a person; the other side's file is kept in trash.
    m["conflictPolicy"] = "newer"
    m["homes"] = copy.deepcopy(KNOWN_HOMES)
    # Skip login files and cache folders inside synced config folders.
    m["configFilters"] = True
    for t in NEW_STORE_EXTRA_TREES:
        if t not in m["claudePaths"]["trees"]:
            m["claudePaths"]["trees"].append(t)
    for g in NEW_STORE_EXTRA_BLOCKLIST:
        if g not in m["blocklistGlobs"]:
            m["blocklistGlobs"].append(g)
    return m

# A sync that only PULLS changes nothing in the store, so it commits nothing —
# and the store's dead-man switch derives each machine's last-seen time from
# "sync <MACHINE>" commit subjects, so a faithfully-syncing consumer looks
# exactly like a dead machine. A stamp under .heartbeat/ gives such a sync
# something to commit. Written only when this machine's own last sync commit is
# older than this, so an every-4h automated sync adds at most one commit per
# machine per day. Must stay well under the switch's per-machine threshold (72h).
HEARTBEAT_STAMP_AFTER_HOURS = 24


META_FILES = ("tombstones.json", "manifest.json")
_MISSING = object()


def _merge3(base, ours, theirs, key: str = ""):
    """3-way merge of JSON values. Dicts merge key by key and lists union, so two
    machines each adding a setting or an agent both keep theirs. Where both sides
    changed one scalar differently, ours stands -- except `syncRepoFiles`, where
    a tie goes to off: never upload secrets because a merge had to pick."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(ours) + [k for k in theirs if k not in ours]:
            v = _merge3(b.get(k, _MISSING), ours.get(k, _MISSING), theirs.get(k, _MISSING), k)
            if v is not _MISSING:
                out[k] = v
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        return ours + [x for x in theirs if x not in ours]
    if key == "syncRepoFiles":
        return False
    return theirs if ours is _MISSING else ours


def merge_store_meta(path: str, base: bytes, ours: bytes, theirs: bytes) -> bytes:
    """Merge the store's own tombstones.json or manifest.json from three versions."""
    def load(blob, default):
        return json.loads(blob.decode("utf-8-sig")) if blob and blob.strip() else default
    try:
        if path == "tombstones.json":
            key = lambda t: (t.get("path"), t.get("ts"), t.get("machine"))
            o, t_ = load(ours, []), load(theirs, [])
            ours_keys, theirs_keys = {key(x) for x in o}, {key(x) for x in t_}
            # an entry in base that one side no longer has was removed there on
            # purpose (`devsync restore`); a union would bring the delete back
            removed = {key(x) for x in load(base, [])
                       if key(x) not in ours_keys or key(x) not in theirs_keys}
            seen, out = set(), []
            for t in o + t_:
                k = key(t)
                if k not in seen and k not in removed:
                    seen.add(k)
                    out.append(t)
            out.sort(key=lambda t: t.get("ts", 0))
        else:
            out = _merge3(load(base, {}), load(ours, {}), load(theirs, {}))
            if isinstance(out.get("homes"), list):
                # the list union keeps both specs when two machines defined the
                # same agent differently; one name, one entry, ours first
                names, homes = set(), []
                for h in out["homes"]:
                    if h.get("name") not in names:
                        names.add(h.get("name"))
                        homes.append(h)
                out["homes"] = homes
    except ValueError as e:
        raise RuntimeError(f"{path} could not be merged: one side is not valid JSON "
                           f"({e}). Fix it in the store checkout.") from None
    return (json.dumps(out, indent=2) + "\n").encode("utf-8")


@dataclass
class Summary:
    merged: int = 0
    conflicts: int = 0
    tombstoned: int = 0
    replaced: int = 0
    adopted: int = 0
    autoresolved: int = 0
    pushed: bool = False
    notes: list = field(default_factory=list)

    def __str__(self):
        parts = [
            f"merged={self.merged}", f"replaced={self.replaced}",
            f"conflicts={self.conflicts}", f"tombstoned={self.tombstoned}",
            f"pushed={'yes' if self.pushed else 'no'}",
        ]
        if self.autoresolved:
            # Appended only when non-zero, so the line a store with the original
            # policy prints is byte-for-byte what it always was.
            parts.append(f"autoresolved={self.autoresolved}")
        s = "devsync: " + "  ".join(parts)
        for n in self.notes:
            s += f"\n  note: {n}"
        return s


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Engine:
    def __init__(self, machine: Machine, store_dir: Path, state_dir: Path,
                 home_overrides: dict | None = None):
        self.home_overrides = dict(home_overrides or {})
        self.machine = machine
        self.store_dir = Path(store_dir)
        self.state_dir = Path(state_dir)
        self.base_dir = self.state_dir / "base"
        self.trash_dir = self.state_dir / "trash"
        self._run_ts = None
        self._enc = None          # manifest `encryption` block, if any
        self._key_cache = None
        # Path mapping depends on a per-store setting; pick it up before any
        # caller (rm, restore, conflicts) maps a path without running a sync.
        self._load_manifest()

    # ── git helpers ──────────────────────────────────────────────

    def _identity(self) -> list:
        """`-c user.name/-c user.email` for a machine with no git identity.

        Someone who has never committed anywhere has none, and git then refuses
        to commit ('Please tell me who you are'), which reached the user as a
        traceback on their very first sync. The store is their own private
        repository and the commits are machine-made, so a neutral identity is
        the right answer rather than an error. A configured identity always
        wins: nothing is passed when git already has one.
        """
        if getattr(self, "_identity_cache", None) is None:
            if not self.store_dir.is_dir():
                return []
            if os.environ.get("GIT_AUTHOR_EMAIL") and os.environ.get("GIT_COMMITTER_EMAIL"):
                self._identity_cache = []
            else:
                r = subprocess.run(["git", "-C", str(self.store_dir), "config",
                                    "user.email"], capture_output=True, text=True)
                self._identity_cache = [] if r.stdout.strip() else [
                    "-c", "user.name=devsync",
                    "-c", f"user.email=devsync@{self.machine.hostname}"]
        return self._identity_cache

    def _git(self, *args, check=True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(self.store_dir), *self._identity(), *args],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        return r

    def _git_show_stage(self, stage: int, path: str) -> bytes:
        r = subprocess.run(
            ["git", "-C", str(self.store_dir), "show", f":{stage}:{path}"],
            capture_output=True)
        return r.stdout if r.returncode == 0 else b""

    def _pull(self, summary: Summary):
        r = self._git("pull", "--no-rebase", "origin", "main", check=False)
        if r.returncode == 0:
            return
        err = (r.stdout + r.stderr).lower()
        if "couldn't find remote ref" in err or "does not appear" in err:
            return  # empty remote — first seed
        # -z: NUL-separated and unquoted, so a path containing a space is one path.
        unmerged = [u for u in self._git("diff", "--name-only", "--diff-filter=U", "-z",
                                         check=False).stdout.split("\0") if u]
        if not unmerged:
            raise RuntimeError(f"git pull failed:\n{r.stdout}\n{r.stderr}")
        try:
            self._finish_merge(unmerged, summary)
        except BaseException:
            # Leave the store as it was before the pull. Half a merge left behind
            # would be committed, conflict markers and all, by the next sync.
            self._git("merge", "--abort", check=False)
            raise

    def _finish_merge(self, unmerged: list, summary: Summary):
        # The store's own two files first, by meaning rather than by line: both are
        # JSON that two machines routinely append to, and a line merge of that is
        # either a conflict or invalid JSON. They must also be whole before the
        # manifest is read below.
        for path in [u for u in unmerged if u in META_FILES]:
            merged = merge_store_meta(path, self._git_show_stage(1, path),
                                      self._git_show_stage(2, path),
                                      self._git_show_stage(3, path))
            (self.store_dir / path).write_bytes(merged)
            self._git("add", path)
        unmerged = [u for u in unmerged if u not in META_FILES]
        manifest = self._load_manifest()
        for path in unmerged:
            base = self._git_show_stage(1, path)
            ours = self._git_show_stage(2, path)
            theirs = self._git_show_stage(3, path)
            base, ours, theirs = (self._decode(path, b) if b else b
                                  for b in (base, ours, theirs))
            cat = category_for(path, manifest)
            res = reconcile(cat, base, ours or None, theirs or None,
                            ours_newer=True,
                            policy=manifest.get("conflictPolicy", LEGACY_CONFLICT_POLICY))
            if res.auto_resolved:
                summary.autoresolved += 1
                if res.loser is not None:
                    self._trash(path + ".loser", res.loser)
                self._log_autoresolve(path, True, summary)
            if res.conflict:
                # Without a sidecar nothing remembers this conflict: the next sync
                # would copy the marked file to the live side, see conflicts=0 and
                # push the markers to every machine.
                try:
                    self._write_sidecar(canonical_to_local(path, self.machine), path, cat,
                                        res.loser_note or "found while pulling: this machine's "
                                        "unpushed change and another machine's touch the same lines")
                except (ValueError, HomeNotPresent):
                    # A file this machine has no place for, so nobody here could
                    # settle it. Take the other machine's copy; ours goes to trash.
                    if ours:
                        self._trash(path + ".loser", ours)
                    res = replace(res, merged=theirs or None, conflict=False)
                    summary.notes.append(f"pull conflict on {path}: no place for it on this "
                                         "machine, kept the other machine's copy (ours is in trash)")
            target = self.store_dir / path
            if res.merged is None:
                if target.exists():
                    target.unlink()
                self._git("rm", "--cached", "--ignore-unmatch", path)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self._encode(path, res.merged))
                self._git("add", path)
            if res.conflict:
                summary.conflicts += 1
                summary.notes.append(f"pull conflict auto-marked: {path}")
        self._git("commit", "-m",
                  f"devsync auto-resolve pull conflicts ({self.machine.hostname})")

    # ── state helpers ────────────────────────────────────────────

    def _load_manifest(self) -> dict:
        manifest = DEFAULT_MANIFEST
        f = self.store_dir / "manifest.json"
        if f.is_file():
            try:
                manifest = json.loads(f.read_text(encoding="utf-8-sig"))
            except ValueError as e:
                # Never fall back to defaults here. The defaults describe the
                # ORIGINAL behaviour, which syncs gitignored files unencrypted: a
                # typo in the manifest would upload every .env in plain text.
                raise RuntimeError(
                    f"{f} is not valid JSON ({e}). Nothing was synced. Fix the "
                    f"file, or restore it with `git -C {self.store_dir} checkout "
                    f"-- manifest.json`.") from None
        # Every load re-applies the store-level path setting, so a manifest that
        # arrived with this run's pull is honoured by the same run.
        root = manifest.get("memoryKeyRoot") or LEGACY_MEMORY_KEY_ROOT
        if root != self.machine.memory_key_root:
            self.machine = replace(self.machine, memory_key_root=root)
        self._enc = manifest.get("encryption") or None
        homes = resolve_homes(manifest, self.home_overrides)
        if homes != self.machine.homes:
            self.machine = replace(self.machine, homes=homes)
        return manifest

    # ── encryption at the store boundary ─────────────────────────
    #
    # Live files and base stay plaintext. Only bytes written into the store
    # checkout are encoded, and everything read from it is decoded, so the
    # merge logic never sees ciphertext. A store with no `encryption` block
    # takes none of these branches: _encode and _decode return their input.

    def _key(self) -> bytes:
        if self._key_cache is None:
            key = crypto.load_key(self.state_dir)
            if key is None:
                raise crypto.EncryptionError(
                    "this store is encrypted and this PC has no key yet -- "
                    "run `devsync unlock` and enter the store passphrase")
            if not crypto.key_matches(key, self._enc):
                raise crypto.EncryptionError(
                    "this PC's key does not open this store (the passphrase "
                    "was changed, or the key file is from another store) -- "
                    "run `devsync unlock`")
            self._key_cache = key
        return self._key_cache

    def _decode(self, canon: str, blob):
        if blob is None or not crypto.is_encrypted(blob):
            return blob
        if not self._enc:
            raise crypto.EncryptionError(
                f"{canon} is encrypted in the store but the store's manifest "
                "has no encryption settings -- restore manifest.json")
        return crypto.decrypt(self._key(), canon, blob)

    def _encode(self, canon: str, data: bytes) -> bytes:
        if crypto.applies(canon, self._enc):
            return crypto.encrypt(self._key(), canon, data)
        return data

    def _check_store_requirements(self, manifest: dict):
        """Refuse to touch a store this build cannot read correctly, BEFORE any
        live file is written. An older build that ignores a newer store's
        settings is how ciphertext would end up over a live .env."""
        if not (self.store_dir / "manifest.json").is_file():
            # Same reason as an unreadable one: without a manifest there is no
            # record of what this store may hold, and the fallback is the
            # permissive original.
            raise RuntimeError(
                "this store has no manifest.json, so it has not been seeded. Run "
                "`devsync init --seed` on the first machine. Nothing was synced.")
        need = manifest.get("requires")
        if need:
            def parts(v):
                return tuple(int(x) for x in str(v).split(".") if x.isdigit())
            if parts(need) > parts(__version__):
                raise RuntimeError(
                    f"this store requires devsync >= {need} and this PC runs "
                    f"{__version__} -- upgrade devsync before syncing")
        if self._enc:
            if self._enc.get("scheme") != crypto.SCHEME:
                raise crypto.EncryptionError(
                    f"unknown encryption scheme {self._enc.get('scheme')!r} -- "
                    "upgrade devsync")
            self._key()

    def _collect_managed(self, manifest: dict) -> dict:
        managed = {}
        if manifest.get("syncRepoFiles", LEGACY_SYNC_REPO_FILES):
            managed.update(collect_repo_files(self.machine, manifest,
                                              exclude=(self.store_dir,)))
        managed.update(collect_claude_files(self.machine, manifest))
        managed.update(collect_home_files(self.machine, manifest))
        return managed

    def _load_tombstones(self) -> list[dict]:
        f = self.store_dir / "tombstones.json"
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8-sig"))
            except ValueError as e:
                # An empty list here would let every deleted file come back.
                raise RuntimeError(f"{f} is not valid JSON ({e}). Nothing was "
                                   f"synced.") from None
        return []

    def _save_tombstones(self, tombs: list[dict]):
        (self.store_dir / "tombstones.json").write_text(
            json.dumps(tombs, indent=2) + "\n")

    def _load_applied(self) -> set:
        f = self.state_dir / "applied-tombstones.json"
        if f.is_file():
            try:
                return set(json.loads(f.read_text(encoding="utf-8-sig")))
            except ValueError:
                return set()
        return set()

    def _save_applied(self, applied: set):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "applied-tombstones.json").write_text(
            json.dumps(sorted(applied), indent=2) + "\n")

    def _trash(self, canon: str, data: bytes):
        ts = self._run_ts or time.strftime("%Y%m%d-%H%M%S")
        dest = self.trash_dir / ts / canon
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def _store_files(self):
        for sub in ("repos", "claude", "agents"):
            d = self.store_dir / sub
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        yield f.relative_to(self.store_dir).as_posix()

    def _base_files(self):
        if self.base_dir.is_dir():
            for f in self.base_dir.rglob("*"):
                if f.is_file():
                    yield f.relative_to(self.base_dir).as_posix()

    def _repo_commit_ts(self, canon: str) -> int:
        r = self._git("log", "-1", "--format=%ct", "--", canon, check=False)
        out = r.stdout.strip()
        return int(out) if out.isdigit() else 0

    # ── heartbeat stamp ──────────────────────────────────────────

    def _last_own_sync_epoch(self) -> int:
        """Commit time of this machine's newest sync commit, 0 if it has none.

        Deliberately the same query the store's dead-man switch runs, so the
        stamp fires exactly when the switch would otherwise go stale.
        """
        r = self._git("log", "-1", "--format=%ct",
                      f"--grep=^sync {self.machine.hostname} ", check=False)
        out = r.stdout.strip()
        return int(out) if out.isdigit() else 0

    def _write_heartbeat_stamp(self) -> bool:
        """Leave a per-machine signal for the store's dead-man switch.

        Returns True if a stamp was written. A machine with no sync commit at
        all is stamped immediately: absent must never mean unmonitored.

        The stamp lives at the store root, outside repos/ and claude/, so
        _store_files() never enumerates it and it is never mapped to a live
        path on any machine.
        """
        now = int(time.time())
        last = self._last_own_sync_epoch()
        if last and (now - last) < HEARTBEAT_STAMP_AFTER_HOURS * 3600:
            return False
        d = self.store_dir / ".heartbeat"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.machine.hostname}.json").write_text(json.dumps({
            "machine": self.machine.hostname,
            "epoch": now,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "run": self._run_ts,
            "note": "devsync liveness stamp; see HEARTBEAT_STAMP_AFTER_HOURS",
        }, indent=2) + "\n", encoding="utf-8")
        return True

    # ── locking ──────────────────────────────────────────────────

    def _acquire_lock(self):
        lock = self.state_dir / "devsync.lock"
        if lock.exists():
            pid_file = lock / "pid"
            pid = None
            if pid_file.is_file():
                try:
                    pid = int(pid_file.read_text().strip())
                except ValueError:
                    pid = None
            if pid and _pid_alive(pid):
                raise RuntimeError(f"devsync already running (pid {pid})")
            shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(os.getpid()))

    def _release_lock(self):
        shutil.rmtree(self.state_dir / "devsync.lock", ignore_errors=True)

    # ── the one command ──────────────────────────────────────────

    def sync(self, push: bool = True) -> Summary:
        """Converge live, base and store. `push=False` commits locally and stops,
        so a caller can inspect `unpublished_paths()` before anything leaves."""
        self._acquire_lock()
        self._run_ts = time.strftime("%Y%m%d-%H%M%S")
        summary = Summary()
        try:
            # `rm`, `config set`, `home add`, `encrypt`, `resolve` and `monitor
            # install` all edit files in the store checkout. Pulling over an
            # uncommitted edit fails ("your local changes would be overwritten")
            # as soon as another machine touched the same file, and it fails the
            # same way on every later run. Committing first turns that into an
            # ordinary merge, which _pull knows how to finish.
            self._git("add", "-A")
            if self._git("status", "--porcelain").stdout.strip():
                self._git("commit", "-m",
                          f"store edits {self.machine.hostname} {self._run_ts}")
            self._pull(summary)
            manifest = self._load_manifest()
            self._check_store_requirements(manifest)

            # tombstones first: a tombstoned live file must not be re-added
            tombs = self._load_tombstones()
            applied = self._load_applied()
            for t in tombs:
                tid = f"{t['path']}@{t['ts']}"
                if tid in applied:
                    continue
                try:
                    live = canonical_to_local(t["path"], self.machine)
                except ValueError:
                    applied.add(tid)
                    continue
                if live.is_file():
                    self._trash(t["path"], live.read_bytes())
                    live.unlink()
                    summary.tombstoned += 1
                base = self.base_dir / t["path"]
                if base.is_file():
                    base.unlink()
                repo = self.store_dir / t["path"]
                if repo.is_file():
                    repo.unlink()
                applied.add(tid)
            self._save_applied(applied)
            tomb_paths = {t["path"] for t in tombs}

            managed = self._collect_managed(manifest)
            all_canon = (set(managed) | set(self._store_files())
                         | set(self._base_files())) - tomb_paths
            # A blocklisted path already in the store or base (synced before it
            # was blocked) must never be written back over a live file.
            blocklist = manifest.get("blocklistGlobs", [])
            all_canon = {c for c in all_canon if not is_blocked(c, blocklist)}
            if not manifest.get("syncRepoFiles", LEGACY_SYNC_REPO_FILES):
                # Off means off: repo files already in the store from before it was
                # turned off are left where they are, on both sides, untouched.
                all_canon = {c for c in all_canon if not c.startswith("repos/")}

            for canon in sorted(all_canon):
                self._reconcile_one(canon, managed, manifest, summary)

            # commit + push
            self._git("add", "-A")
            dirty = bool(self._git("status", "--porcelain").stdout.strip())
            if not dirty and self._write_heartbeat_stamp():
                # Nothing to contribute this run. Stamp so the dead-man switch
                # can tell "synced, nothing to send" from "this machine is dead".
                self._git("add", "-A")
                dirty = bool(self._git("status", "--porcelain").stdout.strip())
            if dirty:
                self._git("commit", "-m",
                          f"sync {self.machine.hostname} {self._run_ts}")
            # Conflicted content is never published: the markers would reach
            # every other machine before a human sees the sidecar. The commit
            # stays local, so nothing is lost -- resolve the sidecar, then
            # re-run to push.
            #
            # The gate is the sidecar on disk, not just this run's count: a
            # conflicted merge keeps the old base, so a re-run sees live ==
            # store and reports conflicts=0 while still unresolved. And the
            # push runs whenever the store is ahead of origin, not only when
            # this run committed: a resolved re-run has nothing new to commit,
            # and the withheld commit must still go out.
            unresolved = max(summary.conflicts, len(self.conflicts()))
            if dirty or self._ahead_of_origin():
                if unresolved:
                    summary.notes.append(
                        f"push withheld: {unresolved} unresolved "
                        "conflict(s) -- resolve the .conflict sidecar(s), "
                        "then re-run")
                elif push:
                    summary.pushed = self._push_with_retry(summary)
            self._prune_trash(manifest)
            return summary
        finally:
            self._release_lock()

    def _reconcile_one(self, canon: str, managed: dict, manifest: dict,
                       summary: Summary):
        try:
            live_path = managed.get(canon) or canonical_to_local(canon, self.machine)
        except HomeNotPresent:
            # An agent this PC does not use: nothing to merge and nothing to say. The
            # store copy still has to be stored the way the manifest now says.
            self._reencode_store_copy(canon, summary)
            return
        except ValueError:
            summary.notes.append(f"skipped unmappable path: {canon}")
            return
        repo_path = self.store_dir / canon
        base_path = self.base_dir / canon

        live = live_path.read_bytes() if live_path.is_file() else None
        raw = repo_path.read_bytes() if repo_path.is_file() else None
        repo = self._decode(canon, raw)
        base = base_path.read_bytes() if base_path.is_file() else None

        # bootstrap/adopt semantics: first contact between an
        # existing live file and an existing repo file uses the repo (seed)
        # as the merge base — avoids whole-file duplicate unions.
        if base is None and live is not None and repo is not None:
            base = repo
            summary.adopted += 1

        # The executable bit, where the platform has one. Taken from the live file
        # when there is one, else from the store copy, and only ever added.
        exec_bits = 0
        if os.name != "nt":
            src = live_path if live is not None else (repo_path if raw is not None else None)
            if src is not None:
                exec_bits = src.stat().st_mode & 0o111

        cat = category_for(canon, manifest)
        ours_newer = True
        if live is not None and repo is not None and live != repo:
            live_ts = live_path.stat().st_mtime
            ours_newer = live_ts >= self._repo_commit_ts(canon)

        res = reconcile(cat, base, live, repo, ours_newer,
                        policy=manifest.get("conflictPolicy", LEGACY_CONFLICT_POLICY))
        if res.merged is None:
            if base_path.is_file():
                base_path.unlink()
            return

        changed = False
        if live != res.merged:
            if live is not None:
                self._trash(canon, live)   # replaced copy is always recoverable
                summary.replaced += 1
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_bytes(res.merged)
            changed = True
        # Compare what is stored with what should be stored, not the decoded
        # content: turning encryption on must rewrite a file whose plaintext
        # did not change. Without encryption the two comparisons are the same.
        encoded = self._encode(canon, res.merged)
        if raw != encoded:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            repo_path.write_bytes(encoded)
            changed = True
        if exec_bits:
            for target in (live_path, repo_path):
                if target.is_file() and (target.stat().st_mode & 0o111) != exec_bits:
                    os.chmod(target, target.stat().st_mode | exec_bits)
        # A conflicted merge must never become the recorded ancestor: once
        # the marker text is in base, base==live==repo on the next run and
        # every later sync truthfully reports conflicts=0 while the file
        # stays corrupt. Keeping the old base forces a real 3-way re-merge
        # after a human resolves the sidecar.
        if not res.conflict and (base != res.merged
                                 or not base_path.is_file()):
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_bytes(res.merged)
        if changed:
            summary.merged += 1

        if res.conflict:
            summary.conflicts += 1
            self._write_sidecar(live_path, canon, cat, res.loser_note)
        if res.loser is not None:
            self._trash(canon + ".loser", res.loser)
        if res.auto_resolved:
            summary.autoresolved += 1
            self._log_autoresolve(canon, ours_newer, summary)

    def _reencode_store_copy(self, canon: str, summary: Summary):
        """Rewrite one store file so its encryption matches the manifest.

        Turning encryption on for `agents/**` from a PC that has no such agent
        would otherwise report the store as encrypted while those files stayed in
        plain text until some other PC happened to sync them.
        """
        repo_path = self.store_dir / canon
        if not repo_path.is_file():
            return
        raw = repo_path.read_bytes()
        encoded = self._encode(canon, self._decode(canon, raw))
        if encoded != raw:
            repo_path.write_bytes(encoded)
            summary.merged += 1

    def _log_autoresolve(self, canon: str, ours_newer: bool, summary: Summary):
        """Record a conflict the policy settled, where a person will look for it."""
        ts = self._run_ts or time.strftime("%Y%m%d-%H%M%S")
        kept = "this PC's copy" if ours_newer else "the store's copy"
        other = (self.trash_dir / ts / (canon + ".loser")).as_posix()
        summary.notes.append(f"conflict settled on {canon}: kept {kept} (newer); "
                             f"the other is at {other}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "conflicts.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{canon}\tkept={kept}\tother={other}\n")

    # ── follower adopt ───────────────────────────────────────────

    def set_aside_local(self) -> tuple[list[str], Path]:
        """Move every managed live file on this PC into trash, before first contact.

        🔴 Why `adopt --follower` exists. On first contact the engine uses the
        store copy as the merge base (see `_reconcile_one`), so base == repo and
        the 3-way merge reads every local difference as a local EDIT: the live
        copy wins, is written to the store and pushed. A plain `adopt` therefore
        publishes the joining PC's config over the fleet's — its MEMORY.md
        replaces the shared index, its settings.json replaces everyone's.

        With nothing live, `reconcile` returns the repo copy for every path and
        there is nothing local to merge. Everything moved here stays recoverable
        with `devsync restore <canonical>` for the trash retention window, and
        the list is kept in `follower-set-aside.json` for deliberate reintroduction.
        """
        self._acquire_lock()
        try:
            self._run_ts = time.strftime("%Y%m%d-%H%M%S")
            manifest = self._load_manifest()
            managed = self._collect_managed(manifest)
            moved = []
            for canon in sorted(managed):
                live = managed[canon]
                if not live.is_file():
                    continue
                self._trash(canon, live.read_bytes())
                live.unlink()
                moved.append(canon)
            trash = self.trash_dir / self._run_ts
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "follower-set-aside.json").write_text(json.dumps({
                "machine": self.machine.hostname,
                "run": self._run_ts,
                "trash": trash.as_posix(),
                "paths": moved,
            }, indent=2) + "\n", encoding="utf-8")
            return moved, trash
        finally:
            self._release_lock()

    def first_contact_differences(self) -> list[str]:
        """Managed files on this PC that a first plain adopt would publish over
        the store's copy. Read-only: nothing live, in base or in the store is
        written, and no lock is taken.

        These are exactly the paths `_reconcile_one` would treat as local edits
        on first contact (base absent, live and store both present and unequal).
        Line endings are ignored, as the text merge ignores them, so a CRLF
        checkout of the same content is not reported as a difference.
        """
        self._git("pull", "--no-rebase", "origin", "main", check=False)
        manifest = self._load_manifest()
        blocklist = manifest.get("blocklistGlobs", [])
        out = []
        for canon, live_path in sorted(self._collect_managed(manifest).items()):
            if is_blocked(canon, blocklist):
                continue
            repo_path = self.store_dir / canon
            if not live_path.is_file() or not repo_path.is_file():
                continue
            if (self.base_dir / canon).is_file():
                continue
            live = live_path.read_bytes()
            repo = self._decode(canon, repo_path.read_bytes())
            if live != repo and live.replace(b"\r\n", b"\n") != repo.replace(b"\r\n", b"\n"):
                out.append(canon)
        return out

    def _ahead_of_origin(self) -> bool:
        """True if HEAD has commits origin/main lacks (as of the last pull)."""
        r = self._git("rev-list", "--count", "origin/main..HEAD", check=False)
        if r.returncode != 0:
            # No origin/main at all: the very first push never landed (it was
            # rejected, or the machine was offline). Everything local is unpublished.
            return self._git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
        return r.stdout.strip() not in ("", "0")

    def unpublished_paths(self) -> list[str]:
        """Store paths committed locally but not yet on origin/main."""
        r = self._git("diff", "--name-only", "origin/main", "HEAD", check=False)
        return [p for p in r.stdout.splitlines() if p.strip()]

    def publish(self, summary: Summary):
        summary.pushed = self._push_with_retry(summary)

    def _push_with_retry(self, summary: Summary) -> bool:
        """Push; on a lost race pull once and push again. Returns whether it pushed.

        The retry pull can itself hit a conflict. Under the `ask` policy that
        leaves marker text in the store, which must not be published: the caller's
        gate ran before this pull, so the check is repeated here.
        """
        r = self._git("push", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            return True
        before = summary.conflicts
        self._pull(summary)
        if summary.conflicts > before:
            summary.notes.append(
                "push withheld: another machine pushed first and its change "
                "conflicts with this one -- run `devsync sync` again, then "
                "`devsync resolve`")
            return False
        self._git("push", "origin", "HEAD:main")
        return True

    def _prune_trash(self, manifest: dict):
        days = manifest.get("trashRetentionDays", 90)
        cutoff = time.time() - days * 86400
        if not self.trash_dir.is_dir():
            return
        for d in self.trash_dir.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ── explicit deletion + recovery ──────────────────

    @staticmethod
    def _require_canonical(canon: str, caller: str):
        """Refuse an absolute path where a canonical one is required.

        Defence in depth. The CLI converts paths before calling this, but
        `rm`/`restore` are importable and the failure mode was SILENT: with an
        absolute `canon`, `store_dir / canon` and `base_dir / canon` resolve to
        the absolute path itself — pathlib replaces the base — so the deletion
        lands outside the store and every caller is told it succeeded. Raising
        is strictly better than a no-op that reports success.
        """
        if (len(canon) > 1 and canon[1] == ":") or canon.startswith(("/", "\\")):
            raise ValueError(
                f"{caller}() needs a canonical store-relative path such as "
                f"'claude/global/CLAUDE.md', not the absolute path {canon!r}. "
                f"Convert it with paths.local_to_canonical() first."
            )

    def rm(self, canon: str):
        self._require_canonical(canon, "rm")
        self._run_ts = self._run_ts or time.strftime("%Y%m%d-%H%M%S")
        tombs = self._load_tombstones()
        entry = {"path": canon, "ts": int(time.time()),
                 "machine": self.machine.hostname}
        tombs.append(entry)
        self._save_tombstones(tombs)
        repo = self.store_dir / canon
        if repo.is_file():
            repo.unlink()
        base = self.base_dir / canon
        if base.is_file():
            base.unlink()
        try:
            live = canonical_to_local(canon, self.machine)
            if live.is_file():
                self._trash(canon, live.read_bytes())
                live.unlink()
        except ValueError:
            pass
        applied = self._load_applied()
        applied.add(f"{entry['path']}@{entry['ts']}")
        self._save_applied(applied)

    def restore(self, canon: str) -> Path:
        self._require_canonical(canon, "restore")
        candidates = sorted(self.trash_dir.glob(f"*/{canon}"))
        if not candidates:
            raise FileNotFoundError(f"{canon} not found in trash")
        newest = candidates[-1]
        live = canonical_to_local(canon, self.machine)
        if live.is_file() and live.read_bytes() != newest.read_bytes():
            self._run_ts = self._run_ts or time.strftime("%Y%m%d-%H%M%S")
            self._trash(canon + ".before-restore", live.read_bytes())
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(newest.read_bytes())
        tombs = [t for t in self._load_tombstones() if t["path"] != canon]
        self._save_tombstones(tombs)
        return live

    # ── status / conflicts ───────────────────────────────────────

    def resolve(self, sidecar: Path, keep: str | None) -> str:
        """Settle one conflict and clear its sidecar. Returns the canonical path.

        The resolved content is written to all THREE copies -- live, store and
        base. Fixing only the live file is the trap this replaces: the store
        still holds the marker text and base still holds the old ancestor, so
        the next sync 3-way merges a fixed file against a conflicted one and
        conflicts again, forever.

        If the live file still has conflict markers, `keep` (local | remote |
        both) chooses the side for every block; the marked-up version goes to
        trash first. If a person already edited the markers away, the file is
        accepted as it stands and `keep` is not needed.
        """
        sidecar = Path(sidecar)
        if not sidecar.name.endswith(".conflict") or not sidecar.is_file():
            raise FileNotFoundError(f"no conflict sidecar at {sidecar}")
        live = sidecar.with_name(sidecar.name[:-len(".conflict")])
        canon = local_to_canonical(live, self.machine)
        if canon is None or not live.is_file():
            raise FileNotFoundError(f"{live} is not a file devsync manages")
        data = live.read_bytes()
        if has_markers(data):
            if keep is None:
                raise ValueError(
                    f"{live} still has conflict markers -- choose with --keep "
                    "local, --keep remote or --keep both, or edit the file")
            self._acquire_lock()
            try:
                self._run_ts = self._run_ts or time.strftime("%Y%m%d-%H%M%S")
                self._trash(canon + ".conflicted", data)
            finally:
                self._release_lock()
            data = resolve_markers(data, keep)
            live.write_bytes(data)
        self._load_manifest()
        for target, content in ((self.store_dir / canon, self._encode(canon, data)),
                                (self.base_dir / canon, data)):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        sidecar.unlink()
        return canon

    def _write_sidecar(self, live_path: Path, canon: str, cat: str, loser_note: str = ""):
        """Record an unsettled conflict beside the live file.

        The sidecar IS the conflict gate: while it exists the push is withheld,
        `devsync status` lists it and `devsync resolve` settles it.
        """
        sidecar = live_path.with_name(live_path.name + ".conflict")
        note = (f"devsync conflict on {canon} at {self._run_ts}\n"
                f"category: {cat}\n")
        if loser_note:
            note += loser_note + "\n"
        note += ("The merged file keeps BOTH versions (markers or winner);"
                 " settle it with `devsync resolve` (or edit the file, then"
                 " `devsync resolve <file>`), which also clears this sidecar.\n")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(note, encoding="utf-8")

    def conflicts(self) -> list[Path]:
        out = []
        seen = set()
        for canon in set(self._base_files()) | set(self._store_files()):
            try:
                live = canonical_to_local(canon, self.machine)
            except ValueError:
                continue
            sidecar = live.with_name(live.name + ".conflict")
            if sidecar.is_file() and sidecar not in seen:
                seen.add(sidecar)
                out.append(sidecar)
        return sorted(out)

    def status(self) -> dict:
        self._git("fetch", "origin", "main", check=False)
        ahead = behind = 0
        r = self._git("rev-list", "--left-right", "--count",
                      "HEAD...origin/main", check=False)
        if r.returncode == 0 and r.stdout.strip():
            a, b = r.stdout.split()
            ahead, behind = int(a), int(b)
        last = self._git("log", "-1", "--format=%s (%cr)", check=False).stdout.strip()
        return {
            "machine": self.machine.hostname,
            "store": str(self.store_dir),
            "ahead": ahead,
            "behind": behind,
            "lastCommit": last,
            "conflicts": [str(p) for p in self.conflicts()],
        }
