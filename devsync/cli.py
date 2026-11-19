"""devsync CLI.

    devsync init --seed      first machine: seed the store
    devsync adopt            other machines: join an existing store
    devsync adopt --follower   ...taking the store's state, publishing nothing local
    devsync sync             the everyday command
    devsync status           ahead/behind, conflicts, last sync
    devsync rm <path>        explicit deletion (tombstone)
    devsync conflicts        list unresolved .conflict sidecars
    devsync resolve          settle conflicts: resolve --all --keep local|remote|both
    devsync restore <path>   pull a file back out of trash
    devsync config           show, or `config set <key> <value>`, this PC's settings
    devsync encrypt          turn on encryption for an existing store
    devsync unlock           give this PC the key to an encrypted store
    devsync monitor install  add the off-machine heartbeat check to the store repo
    devsync home             list / add / remove / path: other agents' config folders
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import devsync.crypto as crypto
from devsync._version import __version__
from devsync.collect import SKIP_PROJECT_DIRS
from devsync.engine import (CONFLICT_POLICIES, HOME_NAME_RE, LEGACY_CONFLICT_POLICY,
                            RESERVED_HOME_NAMES, resolve_homes,
                            LEGACY_MEMORY_KEY_ROOT, LEGACY_SYNC_REPO_FILES,
                            NEW_STORE_MEMORY_KEY_ROOT, Engine, new_store_manifest)
from devsync.paths import HomeNotPresent, Machine, canonical_to_local, local_to_canonical


def _is_absolute(p: str) -> bool:
    """A drive-letter path (`C:/...`) or a POSIX root path (`/...`)."""
    return (len(p) > 1 and p[1] == ":") or p.startswith("/") or p.startswith("\\")


def _abs(p) -> Path:
    """An absolute path, without resolving symlinks. Settings are read later by a
    scheduled run from some other working directory, so nothing relative is stored."""
    return Path(os.path.abspath(str(Path(p).expanduser())))


CANONICAL_ROOTS = ("repos", "claude", "agents")


def _to_canonical(engine: Engine, raw: str) -> str | None:
    """Whatever the user typed -> the canonical store-relative path, or None.

    Accepted: a canonical path (`claude/global/CLAUDE.md`), an absolute path, a
    `~` path, or a path relative to the current directory. Everything downstream
    assumes a canonical path, and the failure mode of passing anything else through
    is silent: the tombstone matches nothing, the store copy survives, and the
    command reports success. So anything that is not recognisably canonical is
    treated as a file on this PC and converted, and a path under no managed folder
    returns None so the caller can refuse.
    """
    norm = raw.replace("\\", "/")
    if not _is_absolute(raw) and not raw.startswith("~"):
        if "/" in norm and norm.split("/", 1)[0] in CANONICAL_ROOTS:
            return norm
    return local_to_canonical(_abs(raw), engine.machine)


def _devsync_home() -> Path:
    env = os.environ.get("DEVSYNC_HOME")
    if env:
        return Path(env)
    return Path.home() / ".devsync"


def _load_engine() -> Engine:
    home = _devsync_home()
    mj = home / "machine.json"
    if not mj.is_file():
        sys.exit("devsync: not initialized — run `devsync init --seed` "
                 "(first machine) or `devsync adopt` (additional machine)")
    data = json.loads(mj.read_text(encoding="utf-8-sig"))
    machine = Machine(
        hostname=data["hostname"],
        cascade_root=Path(data["cascadeRoot"]),
        claude_home=Path(data["claudeHome"]),
        project_key_prefix=data["projectKeyPrefix"],
    )
    return Engine(machine=machine, store_dir=Path(data["storeDir"]),
                  state_dir=home, home_overrides=data.get("homes") or {})


def _default_project_key_prefix(cascade_root: Path) -> str:
    # Claude Code's project-key convention: every non-alphanumeric character
    # becomes '-' (C:/Users/Alex.Smith/CascadeProjects →
    # C--Users-Alex-Smith-CascadeProjects — note the dot converts too).
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", str(cascade_root).replace("\\", "/"))


def _has_files(d: Path) -> bool:
    return d.is_dir() and any(f.is_file() for f in d.rglob("*"))


# Where people keep their repositories, most common first. Used only when neither
# --root nor DEVSYNC_ROOT is given. `CascadeProjects` leads because it was the one
# hard-coded default before this list existed: a machine that has that folder gets
# exactly the root it always got.
ROOT_CANDIDATES = ("CascadeProjects", "code", "src", "projects", "dev", "repos",
                   "git", "workspace", "Developer", "source/repos", "source")

MACHINE_KEYS = {
    "root": "cascadeRoot",
    "claude-home": "claudeHome",
    "store-dir": "storeDir",
    "hostname": "hostname",
    "project-key-prefix": "projectKeyPrefix",
}
STORE_KEYS = ("sync-repo-files", "skip-project-dirs", "memory-key-root",
              "conflict-policy")


def _default_root() -> Path | None:
    env = os.environ.get("DEVSYNC_ROOT")
    if env:
        return Path(env).expanduser()
    for name in ROOT_CANDIDATES:
        d = Path.home().joinpath(*name.split("/"))
        if d.is_dir():
            return d
    return None


def _default_claude_home() -> Path:
    # CLAUDE_CONFIG_DIR is Claude Code's own override for ~/.claude.
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _passphrase(confirm: bool) -> str | None:
    """DEVSYNC_PASSPHRASE, else an interactive prompt. None when neither is possible."""
    env = os.environ.get("DEVSYNC_PASSPHRASE")
    if env:
        return env
    if sys.stdin is None or not sys.stdin.isatty():
        return None
    import getpass
    first = getpass.getpass("devsync store passphrase: ")
    if confirm and getpass.getpass("again: ") != first:
        print("devsync: the two entries differ.", file=sys.stderr)
        return None
    return first or None


NO_PASSPHRASE = ("devsync: a store passphrase is needed. Run this in a terminal to be "
                 "prompted, or set DEVSYNC_PASSPHRASE.")


def _store_encryption(store: Path) -> dict | None:
    f = store / "manifest.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8-sig")).get("encryption") or None
    except ValueError:
        return None


def _ensure_key(store: Path, home: Path) -> bool:
    """Make sure this PC holds the key to `store`, asking for the passphrase if not.
    True when the store is unencrypted or the key is in place."""
    cfg = _store_encryption(store)
    if not cfg:
        return True
    if not crypto.available():
        print("devsync: this store is encrypted and the `cryptography` package is not "
              "installed -- run: pip install cryptography   (installed with pipx? pipx inject ai-devsync cryptography)", file=sys.stderr)
        return False
    key = crypto.load_key(home)
    if key is not None and crypto.key_matches(key, cfg):
        return True
    phrase = _passphrase(confirm=False)
    if phrase is None:
        print(NO_PASSPHRASE, file=sys.stderr)
        return False
    key = crypto.derive_key(phrase, cfg)
    if not crypto.key_matches(key, cfg):
        print("devsync: that passphrase does not open this store. Nothing was changed.",
              file=sys.stderr)
        return False
    crypto.save_key(home, key)
    return True


def _setup(args, seed: bool) -> int:
    home = _devsync_home()
    follower = getattr(args, "follower", False)
    merge = getattr(args, "merge", False)
    if follower and merge:
        print("devsync adopt: --follower and --merge are opposites; pick one.",
              file=sys.stderr)
        return 1
    if follower and _has_files(home / "base"):
        # base is only empty before first contact. After it, local edits are
        # real edits and must merge normally; setting them aside would discard
        # work this machine already owns.
        print(f"devsync adopt --follower: {home / 'base'} is not empty — this PC "
              f"has already synced. --follower is only for a first adopt.",
              file=sys.stderr)
        return 1
    if args.cascade_root:
        cascade = _abs(args.cascade_root)
    else:
        found = _default_root()
        if found is None:
            print("devsync: could not find the folder that holds your repositories. "
                  "Looked in ~/" + ", ~/".join(ROOT_CANDIDATES) + ".\n"
                  "Pass it with --root <folder> (or set DEVSYNC_ROOT).",
                  file=sys.stderr)
            return 1
        cascade = found
    # Secrets are encrypted unless the caller says otherwise: asking for repo-file
    # sync is asking to put .env files in a Git remote.
    want_encrypt = seed and (args.encrypt or (args.sync_repo_files and not args.no_encrypt))
    seed_phrase = None
    if want_encrypt:
        if not crypto.available():
            print("devsync: encrypting the store needs the `cryptography` package -- "
                  "run: pip install cryptography   (installed with pipx? pipx inject ai-devsync cryptography)\n"
                  "(or pass --no-encrypt to store gitignored files in plain text)",
                  file=sys.stderr)
            return 1
        seed_phrase = _passphrase(confirm=True)
        if seed_phrase is None:
            print(NO_PASSPHRASE, file=sys.stderr)
            return 1
    home.mkdir(parents=True, exist_ok=True)
    claude = _abs(args.claude_home) if args.claude_home else _abs(_default_claude_home())
    hostname = args.hostname or os.environ.get("COMPUTERNAME") or socket.gethostname()
    prefix = args.project_key_prefix or _default_project_key_prefix(cascade)
    store = _abs(args.store_dir) if args.store_dir else cascade / "devsync-store"

    if not store.exists():
        r = subprocess.run(["git", "clone", args.remote, str(store)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            if not (seed and store.exists()):
                # A remote that cannot be cloned is a mistyped URL or missing access.
                # Carrying on would build a store that can never be pushed.
                print(f"devsync: could not clone {args.remote}:\n{(r.stdout + r.stderr).strip()}\n"
                      "Nothing has been changed. Check the URL and that you can read the "
                      "repository.", file=sys.stderr)
                return 1

    if (store / ".git").exists():
        # The checkout may be older than the remote: an earlier attempt cloned it
        # before the first machine had pushed, or the remote's default branch is not
        # `main` so the clone came down empty. Without this, `adopt` kept answering
        # "not seeded" for ever. Best effort: an offline machine just carries on.
        subprocess.run(["git", "-C", str(store), "pull", "--no-rebase", "origin", "main"],
                       capture_output=True, text=True)

    if seed and not _has_files(home / "base") and any(
            _has_files(store / d) for d in CANONICAL_ROOTS):
        # `init --seed` on a second machine would treat every file that differs here
        # as this PC's edit and push it over everyone else's, with no questions asked.
        print("devsync init --seed: this store already holds files, so it was seeded "
              "from another machine. Join it with `devsync adopt` instead. Nothing has "
              "been changed.", file=sys.stderr)
        return 1

    if follower and not any(_has_files(store / d) for d in CANONICAL_ROOTS):
        # Following an empty store would set every local file aside and bring
        # nothing back.
        print(f"devsync adopt --follower: the store at {store} has no managed "
              f"files to follow — refusing to set this PC's files aside.",
              file=sys.stderr)
        return 1

    if not seed and not (store / "manifest.json").is_file():
        # Without a manifest the engine has no record of what this store may hold.
        # Joining it anyway used to fall back to defaults that upload gitignored
        # files unencrypted.
        print(f"devsync adopt: the store at {store} has no manifest.json, so it has "
              "not been seeded yet. Run `devsync init --seed` on the first machine "
              "(and check that its push succeeded). Nothing has been changed.",
              file=sys.stderr)
        return 1
    if not seed and not _ensure_key(store, home):
        return 1

    if not seed and not follower and not merge and not _has_files(home / "base"):
        # 🔴 A plain first adopt is not a union. The store copy becomes the merge
        # base, so every file that differs here reads as a local edit: this PC's
        # copy wins and is pushed to every other machine. That is sometimes what
        # is wanted and never what should happen by accident, so when it WOULD
        # happen the caller has to say which way round they mean. Nothing has
        # been written yet: no machine.json, no sync, no push.
        probe = Engine(machine=Machine(hostname=hostname, cascade_root=cascade,
                                       claude_home=claude,
                                       project_key_prefix=prefix),
                       store_dir=store, state_dir=home)
        differing = probe.first_contact_differences()
        if differing:
            print(f"devsync adopt: {len(differing)} file(s) on this PC differ from "
                  f"the store. Nothing has been changed. Choose one and re-run:\n"
                  f"  --follower  take the store's copies; this PC's are set aside "
                  f"in trash (recoverable)\n"
                  f"  --merge     this PC's differences win and are pushed to every "
                  f"other machine", file=sys.stderr)
            for c in differing[:20]:
                print(f"  {c}", file=sys.stderr)
            if len(differing) > 20:
                print(f"  ... and {len(differing) - 20} more", file=sys.stderr)
            return 3

    machine_json = {
        "hostname": hostname,
        "cascadeRoot": cascade.as_posix(),
        "claudeHome": claude.as_posix(),
        "projectKeyPrefix": prefix,
        "storeDir": store.as_posix(),
    }
    (home / "machine.json").write_text(json.dumps(machine_json, indent=2) + "\n")

    manifest_file = store / "manifest.json"
    if seed and manifest_file.is_file() and (args.sync_repo_files or args.encrypt
                                             or args.no_encrypt or args.memory_key_root):
        print("devsync: this store already has a manifest, so --sync-repo-files, "
              "--encrypt, --no-encrypt and --memory-key-root were NOT applied. Change "
              "an existing store with `devsync config set` and `devsync encrypt`.")
    if seed and not manifest_file.is_file():
        key_root = (args.memory_key_root or os.environ.get("DEVSYNC_MEMORY_KEY_ROOT")
                    or NEW_STORE_MEMORY_KEY_ROOT)
        manifest = new_store_manifest(sync_repo_files=args.sync_repo_files,
                                      memory_key_root=key_root)
        if want_encrypt:
            manifest["encryption"], key = crypto.new_config(seed_phrase)
            # a build that predates encryption must refuse this store
            manifest["requires"] = "0.2.0"
            crypto.save_key(home, key)
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        if not (store / ".gitattributes").is_file():
            # The store holds bytes exactly as devsync wrote them. Without this, git on
            # a machine with autocrlf rewrites line endings on checkout and a key or a
            # shell script arrives changed.
            (store / ".gitattributes").write_text(
                "# devsync store: no line-ending conversion, on any machine.\n* -text\n")
        if want_encrypt:
            print("devsync: store encryption is ON for "
                  + ", ".join(manifest["encryption"]["paths"])
                  + ". Other PCs will ask for the passphrase when they join; "
                  "it cannot be recovered if lost.")
        if args.sync_repo_files and not want_encrypt:
            print("devsync: gitignored repo files (.env, keys, certs) will be synced. "
                  "The store holds them UNENCRYPTED -- keep its remote private.")
        elif args.sync_repo_files:
            print("devsync: gitignored repo files (.env, keys, certs) will be synced, "
                  "encrypted.")
        else:
            print("devsync: syncing Claude Code config and memory only. To also carry "
                  "gitignored repo files (.env, keys, certs), encrypted: "
                  "`devsync encrypt --all-machines-upgraded`, then "
                  "`devsync config set sync-repo-files on`.")
    if seed:
        try:
            found = resolve_homes(json.loads(manifest_file.read_text(encoding="utf-8-sig")))
        except ValueError:
            found = {}
        if found:
            print("devsync: also syncing instruction files for: "
                  + ", ".join(f"{n} ({p.as_posix()})" for n, p in sorted(found.items()))
                  + ". See `devsync home list`.")
    if seed and not (store / "tombstones.json").is_file():
        (store / "tombstones.json").write_text("[]\n")

    engine = _load_engine()
    if follower:
        return _follow(engine, hostname, args.remote)
    summary = engine.sync()
    print(f"devsync {'init --seed' if seed else 'adopt'} complete "
          f"({hostname} -> {args.remote})")
    print(summary)
    return 0


def _follow(engine: Engine, hostname: str, remote: str) -> int:
    """First adopt that takes the store's state and publishes nothing local.

    Set every managed local file aside, sync WITHOUT pushing, then push only if
    the one thing the local commit carries is this machine's heartbeat stamp.
    Anything else means local content would leave — withheld, never pushed.
    """
    moved, trash = engine.set_aside_local()
    print(f"set aside {len(moved)} local file(s) -> {trash}")
    print("  that includes any file only this PC had; none of them is published")
    print(f"  list: {engine.state_dir / 'follower-set-aside.json'}  "
          f"(recover one with `devsync restore <canonical>`)")
    summary = engine.sync(push=False)
    unpublished = engine.unpublished_paths()
    leaked = [p for p in unpublished if not p.startswith(".heartbeat/")]
    if leaked or summary.conflicts:
        print(summary)
        print(f"devsync adopt --follower: push WITHHELD — the local commit would "
              f"publish {len(leaked)} non-heartbeat path(s):", file=sys.stderr)
        for p in leaked[:50]:
            print(f"  {p}", file=sys.stderr)
        if len(leaked) > 50:
            print(f"  ... and {len(leaked) - 50} more", file=sys.stderr)
        print("Inspect with `git -C <store> diff --stat origin/main HEAD`; "
              "nothing has left this PC.", file=sys.stderr)
        return 1
    if unpublished:
        engine.publish(summary)
    print(f"devsync adopt --follower complete ({hostname} -> {remote})")
    print(summary)
    return 0


def _on_off(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "true", "yes", "1"):
        return True
    if v in ("off", "false", "no", "0"):
        return False
    raise ValueError(f"expected on or off, got {value!r}")


def _config(args) -> int:
    home = _devsync_home()
    mj = home / "machine.json"
    if not mj.is_file():
        sys.exit("devsync: not initialized — run `devsync init --seed` "
                 "(first machine) or `devsync adopt` (additional machine)")
    data = json.loads(mj.read_text(encoding="utf-8-sig"))
    manifest_file = Path(data["storeDir"]) / "manifest.json"
    manifest = {}
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            print(f"devsync config: {manifest_file} is not valid JSON ({e}). Fix or "
                  "restore it first.", file=sys.stderr)
            return 1

    if not args.action:
        print(json.dumps({
            "thisMachine": {k: data.get(v) for k, v in MACHINE_KEYS.items()},
            "store": {
                "sync-repo-files": manifest.get("syncRepoFiles", LEGACY_SYNC_REPO_FILES),
                "skip-project-dirs": manifest.get("skipProjectDirs", sorted(SKIP_PROJECT_DIRS)),
                "memory-key-root": manifest.get("memoryKeyRoot", LEGACY_MEMORY_KEY_ROOT),
                "conflict-policy": manifest.get("conflictPolicy", LEGACY_CONFLICT_POLICY),
                "agent-homes": sorted(h.get("name", "") for h in manifest.get("homes", []) or []),
                "encryption": (manifest["encryption"].get("paths")
                               if manifest.get("encryption") else "off"),
            },
        }, indent=2))
        return 0

    key, value = args.key, args.value
    if key in MACHINE_KEYS:
        if key in ("root", "claude-home", "store-dir"):
            value = _abs(value).as_posix()
        if key == "root":
            # The prefix names this PC's ~/.claude/projects/<key> folders and is
            # derived from the root. Left stale it silently stops collecting
            # memory, so it moves with the root unless it was set by hand.
            old_default = _default_project_key_prefix(Path(data["cascadeRoot"]))
            if data.get("projectKeyPrefix") == old_default:
                data["projectKeyPrefix"] = _default_project_key_prefix(Path(value))
                print(f"devsync: project-key-prefix -> {data['projectKeyPrefix']}")
                print("devsync: Claude Code keys project memory by path. Copy "
                      f"{data['claudeHome']}/projects/{old_default}* to the new prefix "
                      "BEFORE the next sync, or that memory is not collected.")
            else:
                print("devsync: project-key-prefix was set by hand and is unchanged "
                      f"({data.get('projectKeyPrefix')}); update it if the root moved.")
            if data.get("storeDir", "").startswith(data["cascadeRoot"].rstrip("/") + "/"):
                print(f"devsync: store-dir is still {data['storeDir']} -- move the "
                      "folder and `devsync config set store-dir <path>` if it moved too.")
        data[MACHINE_KEYS[key]] = value
        mj.write_text(json.dumps(data, indent=2) + "\n")
        print(f"devsync: {key} = {value} (this PC only)")
        return 0

    if key in STORE_KEYS:
        if key == "memory-key-root":
            print("devsync: memory-key-root is fixed when a store is seeded -- every "
                  "memory path in the store is named after it. Set it with "
                  "`devsync init --seed --memory-key-root <name>` on a new store.",
                  file=sys.stderr)
            return 2
        if not manifest_file.is_file():
            print(f"devsync: no manifest at {manifest_file}", file=sys.stderr)
            return 1
        try:
            if key == "sync-repo-files":
                plain = value.strip().lower() == "plain"
                want = True if plain else _on_off(value)
                already_on = manifest.get("syncRepoFiles", LEGACY_SYNC_REPO_FILES)
                if want and not plain and not already_on and not manifest.get("encryption"):
                    print("devsync: this store is not encrypted, so that would upload .env "
                          "files, keys and certs in plain text. Nothing was changed. "
                          "Either:\n"
                          "  devsync encrypt --all-machines-upgraded     (then run this again)\n"
                          "  devsync config set sync-repo-files plain     (plain text, in a "
                          "private repository you control)", file=sys.stderr)
                    return 2
                manifest["syncRepoFiles"] = want
                if want and not manifest.get("encryption"):
                    print("devsync: the store holds these files UNENCRYPTED -- keep "
                          "its remote private, or run `devsync encrypt`.")
            elif key == "conflict-policy":
                if value not in CONFLICT_POLICIES:
                    raise ValueError("expected " + " or ".join(CONFLICT_POLICIES)
                                     + f", got {value!r}")
                manifest["conflictPolicy"] = value
            else:
                manifest["skipProjectDirs"] = [v.strip() for v in value.split(",")
                                               if v.strip()]
        except ValueError as e:
            print(f"devsync config: {e}", file=sys.stderr)
            return 2
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"devsync: {key} = {value} (whole store; reaches other PCs on the "
              "next `devsync sync`)")
        return 0

    print(f"devsync config: unknown key {key!r}. Keys: "
          + ", ".join(list(MACHINE_KEYS) + list(STORE_KEYS)), file=sys.stderr)
    return 2


def _encrypt(args) -> int:
    """Turn encryption on for a store that already exists."""
    home = _devsync_home()
    engine = _load_engine()
    store = engine.store_dir
    manifest_file = store / "manifest.json"
    extra = []
    for piece in (args.paths or "").split(","):
        glob = piece.strip().strip("'\"").strip()     # cmd.exe passes single quotes through
        if not glob:
            continue
        if "/" not in glob or glob.split("/", 1)[0] not in CANONICAL_ROOTS:
            print(f"devsync encrypt: {piece.strip()!r} is not a store path. Use globs "
                  f"under repos/, claude/ or agents/, for example "
                  f"--paths \"claude/**,agents/**\". Nothing was changed.", file=sys.stderr)
            return 2
        extra.append(glob)
    existing = _store_encryption(store)
    if existing:
        current = list(existing.get("paths") or crypto.DEFAULT_PATHS)
        wanted = list(dict.fromkeys(current + extra))
        if wanted == current:
            print("devsync: this store is already encrypted for " + ", ".join(current) + ".")
            return 0
        if not _ensure_key(store, home):
            return 1
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        manifest["encryption"]["paths"] = wanted
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        summary = _load_engine().sync()
        print(summary)
        print("devsync: now also encrypting " + ", ".join(p for p in wanted if p not in current)
              + ". Earlier commits in the store's Git history still hold the old plain-text copies.")
        return 2 if summary.conflicts else 0
    if not args.all_machines_upgraded:
        print("devsync encrypt: NOT done. Every PC that syncs this store must run "
              "devsync 0.2.0 or later FIRST.\nAn older build does not know the store "
              "is encrypted and would write the encrypted bytes over your live files "
              "(the originals go to its trash, but every .env on that PC breaks).\n"
              "Check `devsync --version` on each PC, then re-run with "
              "--all-machines-upgraded.", file=sys.stderr)
        return 2
    if not crypto.available():
        print("devsync: needs the `cryptography` package -- run: pip install cryptography   (installed with pipx? pipx inject ai-devsync cryptography)",
              file=sys.stderr)
        return 1
    if not manifest_file.is_file():
        print(f"devsync: no manifest at {manifest_file}", file=sys.stderr)
        return 1
    phrase = _passphrase(confirm=True)
    if phrase is None:
        print(NO_PASSPHRASE, file=sys.stderr)
        return 1
    manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    # --paths ADDS to the default: a list that left out repos/** would leave the very
    # files encryption is for in plain text.
    paths = list(dict.fromkeys(crypto.DEFAULT_PATHS + extra))
    manifest["encryption"], key = crypto.new_config(phrase, paths)
    manifest["requires"] = "0.2.0"
    crypto.save_key(home, key)
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
    summary = _load_engine().sync()
    print(summary)
    print("devsync: store encryption is ON for " + ", ".join(manifest["encryption"]["paths"])
          + ". Run `devsync unlock` on each other PC. Earlier commits in the store's "
          "Git history still hold the old plain-text copies.")
    return 2 if summary.conflicts else 0


def _unlock() -> int:
    home = _devsync_home()
    engine = _load_engine()
    engine._git("pull", "--no-rebase", "origin", "main", check=False)
    if not _store_encryption(engine.store_dir):
        print("devsync: this store is not encrypted; nothing to unlock.")
        return 0
    if not _ensure_key(engine.store_dir, home):
        return 1
    print("devsync: this PC can now read the store. Run `devsync sync`.")
    return 0


def _resolve(engine: Engine, args) -> int:
    sidecars = engine.conflicts()
    if args.all:
        chosen = sidecars
    else:
        chosen = []
        for raw in args.paths:
            p = Path(raw).expanduser()
            if not _is_absolute(raw) and not p.exists():
                # a canonical store path such as claude/global/CLAUDE.md
                try:
                    from devsync.paths import canonical_to_local
                    p = canonical_to_local(raw, engine.machine)
                except ValueError:
                    pass
            if not p.name.endswith(".conflict"):
                p = p.with_name(p.name + ".conflict")
            chosen.append(p.resolve() if p.exists() else p)
    if not chosen:
        if not sidecars:
            print("no unresolved conflicts")
            return 0
        print("devsync resolve: name the file(s), or use --all. Unresolved:", file=sys.stderr)
        for s in sidecars:
            print(f"  {s}", file=sys.stderr)
        return 2
    done = 0
    for sc in chosen:
        try:
            canon = engine.resolve(sc, args.keep)
        except (FileNotFoundError, ValueError) as e:
            print(f"devsync resolve: {e}", file=sys.stderr)
            return 2
        print(f"resolved {canon}")
        done += 1
    summary = engine.sync()
    print(summary)
    return 2 if summary.conflicts or engine.conflicts() else 0


MONITOR_FILES = (".github/heartbeat_check.py", ".github/workflows/sync-heartbeat.yml")


def _monitor(engine: Engine, args) -> int:
    """Copy the heartbeat check into the store checkout; the next sync publishes it.

    The check has to live in the STORE repository: it reads that repo's history, and
    it must run somewhere that is not one of the machines it watches.
    """
    src_root = Path(__file__).resolve().parent / "templates" / "store"
    written, kept = [], []
    for rel in MONITOR_FILES:
        src, dst = src_root / rel, engine.store_dir / rel
        if not src.is_file():
            print(f"devsync monitor: template missing from this install: {rel}", file=sys.stderr)
            return 1
        content = src.read_bytes().replace(b"\r\n", b"\n")
        if (dst.is_file() and dst.read_bytes().replace(b"\r\n", b"\n") != content
                and not args.force):
            kept.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)
        written.append(rel)
    for rel in written:
        print(f"installed {rel}")
    for rel in kept:
        print(f"kept your existing {rel} (differs from the template; --force replaces it)")
    summary = engine.sync()
    print(summary)
    print("devsync: the check runs daily on GitHub Actions in the store repository and "
          "opens an issue when a machine stops syncing.\n"
          "Rehearse it once before relying on it:\n"
          "  gh workflow run sync-heartbeat.yml -f machine_threshold_hours=-1\n"
          "On another Git host, schedule `python .github/heartbeat_check.py --exit-code` "
          "there instead.")
    return 2 if summary.conflicts or engine.conflicts() else 0


def _split(value: str | None) -> list:
    return [v.strip().replace("\\", "/").strip("/") for v in (value or "").split(",") if v.strip()]


def _home(args) -> int:
    """Other coding agents' config folders ("homes"). Claude Code's own folder is
    `claude-home` under `devsync config`; everything here is in addition to it."""
    import re
    state = _devsync_home()
    mj = state / "machine.json"
    if not mj.is_file():
        sys.exit("devsync: not initialized — run `devsync init --seed` "
                 "(first machine) or `devsync adopt` (additional machine)")
    data = json.loads(mj.read_text(encoding="utf-8-sig"))
    manifest_file = Path(data["storeDir"]) / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        print(f"devsync home: cannot read {manifest_file}", file=sys.stderr)
        return 1
    homes = manifest.get("homes") or []
    overrides = data.get("homes") or {}
    by_name = {h.get("name"): h for h in homes}

    if args.action == "list":
        present = resolve_homes(manifest, overrides)
        rows = [{"name": "claude", "path": data["claudeHome"], "onThisPC": True,
                 "note": "Claude Code; set with `devsync config set claude-home`"}]
        for h in homes:
            n = h.get("name")
            where = overrides.get(n) or h.get("default") or ""
            rows.append({"name": n, "path": present[n].as_posix() if n in present else where,
                         "onThisPC": n in present, "files": h.get("global", []),
                         "folders": h.get("trees", [])})
        print(json.dumps(rows, indent=2))
        return 0

    name = args.name
    if not name or not re.match(HOME_NAME_RE, name) or name in RESERVED_HOME_NAMES:
        print(f"devsync home: {name!r} is not a usable name. Use lower-case letters, digits, "
              f"'-' or '_' (and not: {', '.join(sorted(RESERVED_HOME_NAMES))}).", file=sys.stderr)
        return 2

    if args.action == "path":
        if name not in by_name:
            print(f"devsync home: the store has no agent named {name!r}. "
                  f"Add it first with `devsync home add`.", file=sys.stderr)
            return 2
        if not args.path:
            print("usage: devsync home path <name> <folder>", file=sys.stderr)
            return 2
        overrides[name] = _abs(args.path).as_posix()
        data["homes"] = overrides
        mj.write_text(json.dumps(data, indent=2) + "\n")
        print(f"devsync: {name} is at {overrides[name]} on this PC")
        return 0

    if args.action == "add":
        if not args.path:
            print("usage: devsync home add <name> <folder> --files a,b --folders x,y",
                  file=sys.stderr)
            return 2
        files, folders = _split(args.files), _split(args.folders)
        if not files and not folders:
            print("devsync home add: name what to sync with --files and/or --folders. "
                  "Nothing in an agent's folder is synced unless it is listed, so its "
                  "logins and history stay put.", file=sys.stderr)
            return 2
        folder = _abs(args.path)
        try:
            default = "~/" + folder.resolve().relative_to(Path.home().resolve()).as_posix()
        except ValueError:
            default = ""            # outside the home directory: each PC sets its own path
        spec = {"name": name, "default": default, "env": "", "global": files, "trees": folders}
        manifest["homes"] = [h for h in homes if h.get("name") != name] + [spec]
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        overrides[name] = folder.as_posix()
        data["homes"] = overrides
        mj.write_text(json.dumps(data, indent=2) + "\n")
        print(f"devsync: added agent {name!r} at {folder.as_posix()} "
              f"(files: {files or 'none'}; folders: {folders or 'none'}).")
        if not default:
            print(f"devsync: that folder is outside your home directory, so run "
                  f"`devsync home path {name} <folder>` on each other PC.")
        print("devsync: reaches other PCs on the next `devsync sync`; a PC without that "
              "folder is left alone.")
        return 0

    if args.action == "remove":
        if name not in by_name:
            print(f"devsync home: the store has no agent named {name!r}.", file=sys.stderr)
            return 2
        manifest["homes"] = [h for h in homes if h.get("name") != name]
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        overrides.pop(name, None)
        data["homes"] = overrides
        mj.write_text(json.dumps(data, indent=2) + "\n")
        print(f"devsync: stopped syncing {name!r}. Nothing was deleted: your files and the "
              f"store's copies under agents/{name}/ are untouched.")
        return 0
    return 2


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="devsync",
                                     description="merge-based sync of AI coding-agent "
                                                 "config, memory and gitignored files")
    parser.add_argument("--version", action="version",
                        version=f"devsync {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("init", "adopt"):
        p = sub.add_parser(name)
        if name == "init":
            p.add_argument("--seed", action="store_true", required=True)
            p.add_argument("--sync-repo-files", action="store_true",
                           help="also sync gitignored repo files (.env, keys, "
                                "certs), encrypted unless --no-encrypt")
            p.add_argument("--encrypt", action="store_true",
                           help="encrypt store files (default when "
                                "--sync-repo-files is given)")
            p.add_argument("--no-encrypt", action="store_true",
                           help="with --sync-repo-files: store them in plain text")
            p.add_argument("--memory-key-root",
                           help="label for the dev root inside the store "
                                "(default: projects)")
        else:
            p.add_argument("--follower", action="store_true",
                           help="take the store's state as-is: set every managed "
                                "local file aside (to trash) first, and publish "
                                "nothing but this PC's heartbeat")
            p.add_argument("--merge", action="store_true",
                           help="where this PC's files differ from the store, "
                                "this PC's copy wins and is pushed everywhere")
        p.add_argument("--remote", required=True,
                       help="git remote URL (or local bare repo path)")
        p.add_argument("--root", "--cascade-root", dest="cascade_root",
                       help="folder that holds your repositories (default: "
                            "DEVSYNC_ROOT, else the first of ~/CascadeProjects, "
                            "~/code, ~/src, ~/projects, ~/dev, ... that exists)")
        p.add_argument("--claude-home",
                       help="default: CLAUDE_CONFIG_DIR, else ~/.claude")
        p.add_argument("--hostname")
        p.add_argument("--project-key-prefix")
        p.add_argument("--store-dir",
                       help="default: <root>/devsync-store")

    sub.add_parser("sync")
    sub.add_parser("status")
    sub.add_parser("conflicts")
    p_rm = sub.add_parser("rm")
    p_rm.add_argument("path", help="canonical path, e.g. repos/foo/.env")
    p_rs = sub.add_parser("restore")
    p_rs.add_argument("path", help="canonical path to pull back from trash")
    p_cf = sub.add_parser("config", help="show settings, or: config set <key> <value>")
    # REMAINDER, not three positionals: a value may begin with '-' (every Linux and
    # macOS project-key prefix does) and argparse would read it as an option.
    p_cf.add_argument("rest", nargs=argparse.REMAINDER)
    p_en = sub.add_parser("encrypt", help="turn on encryption for an existing store")
    p_en.add_argument("--all-machines-upgraded", action="store_true",
                      help="confirm every PC on this store runs devsync >= 0.2.0")
    p_en.add_argument("--paths", help="extra store globs to encrypt as well as repos/**, "
                                       "comma-separated, e.g. claude/**,agents/**")
    sub.add_parser("unlock", help="give this PC the key to an encrypted store")
    p_mo = sub.add_parser("monitor", help="monitor install: add the heartbeat check to the store")
    p_mo.add_argument("action", choices=["install"])
    p_mo.add_argument("--force", action="store_true",
                      help="replace files in the store that differ from the template")
    p_ho = sub.add_parser("home", help="other agents' config folders: list, add, remove, path")
    p_ho.add_argument("action", choices=["list", "add", "remove", "path"])
    p_ho.add_argument("name", nargs="?")
    p_ho.add_argument("path", nargs="?")
    p_ho.add_argument("--files", help="comma-separated files directly in the folder")
    p_ho.add_argument("--folders", help="comma-separated sub-folders, synced whole")
    p_rv = sub.add_parser("resolve", help="settle conflicts and clear their sidecars")
    p_rv.add_argument("paths", nargs="*", help="conflicted file(s), or use --all")
    p_rv.add_argument("--all", action="store_true")
    p_rv.add_argument("--keep", choices=["local", "remote", "both"],
                      help="which side to keep where the file still has conflict "
                           "markers (not needed if you already edited them away)")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        return _setup(args, seed=True)
    if args.cmd == "adopt":
        return _setup(args, seed=False)
    if args.cmd == "config":
        rest = list(args.rest)
        if rest and (rest[0] != "set" or len(rest) != 3):
            print("usage: devsync config            (show settings)\n"
                  "       devsync config set <key> <value>", file=sys.stderr)
            return 2
        args.action = "set" if rest else None
        args.key, args.value = (rest[1], rest[2]) if rest else (None, None)
        return _config(args)
    if args.cmd == "home":
        return _home(args)
    if args.cmd == "encrypt":
        return _encrypt(args)
    if args.cmd == "unlock":
        return _unlock()

    engine = _load_engine()
    if args.cmd == "sync":
        try:
            summary = engine.sync()
        except crypto.EncryptionError as e:
            # Raised before any live file is written. A plain message, not a
            # traceback: this is something the user fixes, not a fault.
            print(f"devsync: {e}", file=sys.stderr)
            return 1
        print(summary)
        # An older unresolved sidecar withholds the push too; exit 2 is what the
        # wrappers log as CONFLICT, so it must cover both.
        return 2 if summary.conflicts or engine.conflicts() else 0
    if args.cmd == "status":
        st = engine.status()
        print(json.dumps(st, indent=2))
        return 0
    if args.cmd == "conflicts":
        sidecars = engine.conflicts()
        if not sidecars:
            print("no unresolved conflicts")
            return 0
        for s in sidecars:
            print(s)
        return 2
    if args.cmd == "monitor":
        return _monitor(engine, args)
    if args.cmd == "resolve":
        try:
            return _resolve(engine, args)
        except crypto.EncryptionError as e:
            print(f"devsync: {e}", file=sys.stderr)
            return 1
    if args.cmd == "rm":
        canon = _to_canonical(engine, args.path)
        if canon is None:
            print(f"devsync rm: {args.path} is not under any root devsync manages — "
                  f"nothing tombstoned", file=sys.stderr)
            return 2
        places = [engine.store_dir / canon, engine.base_dir / canon]
        try:
            places.append(canonical_to_local(canon, engine.machine))
        except ValueError:
            pass
        if any(p.is_dir() for p in places):
            print(f"devsync rm: {args.path} is a folder. Name the file(s) to remove; "
                  f"nothing was tombstoned.", file=sys.stderr)
            return 2
        if not any(p.is_file() for p in places):
            print(f"devsync rm: nothing called {canon} on this PC or in the store; "
                  f"nothing was tombstoned.", file=sys.stderr)
            return 2
        if not any(p.is_file() for p in places[:2]):
            # On this PC but never synced: devsync does not manage it, and removing
            # it here would delete a file nothing else knows about.
            print(f"devsync rm: {canon} is not a file devsync has synced (it is not "
                  f"in the store). Nothing was changed.", file=sys.stderr)
            return 2
        engine.rm(canon)
        engine.sync()
        print(f"tombstoned {canon} (recoverable via `devsync restore`)")
        return 0
    if args.cmd == "restore":
        canon = _to_canonical(engine, args.path)
        if canon is None:
            print(f"devsync restore: {args.path} is not under any root devsync manages",
                  file=sys.stderr)
            return 2
        try:
            live = engine.restore(canon)
        except HomeNotPresent as e:
            print(f"devsync restore: {e}", file=sys.stderr)
            return 2
        print(f"restored -> {live}")
        summary = engine.sync()
        print(summary)
        return 2 if summary.conflicts or engine.conflicts() else 0
    return 1


def main(argv=None) -> int:
    """Run a command. Failures a user can act on -- the lock is held, the remote
    is unreachable, a file is not in trash, the store needs a key -- are printed
    as one message with exit 1, not as a traceback. The sync wrappers classify
    that message, so its wording is part of the contract."""
    try:
        return _main(argv)
    except (crypto.EncryptionError, HomeNotPresent, RuntimeError, OSError) as e:
        print(f"devsync: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
