# ai-devsync

Merge-based sync of your AI coding agents' instructions, config and memory across
your machines, with optional sync of the gitignored files in your repos (`.env`,
keys, certs). Built around Claude Code; also carries Codex, Gemini CLI and Kiro
instruction files, and any other agent you name. The command is `devsync`.

One command, `devsync sync`, converges every machine. Edits made on two PCs both
survive (3-way merge against a per-machine base snapshot), and nothing is ever
deleted by inference (explicit tombstones plus a 90-day trash).

The store is a private Git repository that you own. devsync has no server.

## Install

```
pipx install git+https://github.com/sgharlow/ai-devsync.git
```

That puts `devsync` on your PATH. From a clone of the repository instead:
`pip install -e .`

If the `devsync` command is not on your PATH, `python -m devsync` is the same thing.
Requires Python 3.10+ and git. ai-devsync is not on PyPI. An unrelated
project owns the name `devsync` there: **do not `pip install devsync`**, it installs
something else.

## First machine

Create an empty private repository for the store, then:

```
devsync init --seed --remote https://github.com/<you>/devsync-store.git
```

By default this syncs Claude Code config and memory only. Add `--sync-repo-files`
to also carry gitignored repo files; see [Secrets](#secrets) first.

## Every other machine

```
devsync adopt --remote https://github.com/<you>/devsync-store.git
```

If files on the joining PC differ from the store, `adopt` stops before changing
anything, lists them, and asks you to choose:

| Flag | Meaning |
|---|---|
| `--follower` | Take the store's copies. Every managed file on this PC moves to `~/.devsync/trash/<ts>/` first, including any the store does not have, and nothing local is published. They are listed in `~/.devsync/follower-set-aside.json` and kept for 90 days; bring one back with `devsync restore <path>`. |
| `--merge` | This PC's differences win and are pushed to every other machine. |

When nothing differs, a plain `adopt` joins without asking. Joining is still a
sync: files only this PC has are uploaded, and a file the store records as deleted
is removed here too (to trash). Seed the store from one machine
only, and wait for that first `init` to finish before joining from another. An
`adopt` that runs too early is refused and can simply be run again later.

## Settings

Every default can be overridden at `init`/`adopt`, and changed later with
`devsync config`.

| Setting | Flag | Default |
|---|---|---|
| Folder holding your repos | `--root` | `DEVSYNC_ROOT`, else the first of `~/CascadeProjects`, `~/code`, `~/src`, `~/projects`, `~/dev`, `~/repos`, `~/git`, `~/workspace`, `~/Developer`, `~/source/repos`, `~/source` that exists |
| Claude Code folder | `--claude-home` | `CLAUDE_CONFIG_DIR`, else `~/.claude` |
| Store checkout | `--store-dir` | `<root>/devsync-store` |
| This PC's name | `--hostname` | the computer name |
| Project-key prefix | `--project-key-prefix` | derived from the root, the way Claude Code names `~/.claude/projects/*` |
| Sync gitignored repo files | `--sync-repo-files` (init) | off |
| Memory label in the store | `--memory-key-root` (init) | `projects` |
| What to do with a real conflict | `devsync config set conflict-policy` | `newer` |
| Other agents to sync | `devsync home add / remove` | Codex, Gemini CLI, Kiro |

| Command | What it does |
|---|---|
| `devsync config` | show this PC's and the store's settings |
| `devsync config set root D:/work` | this PC only; the derived prefix moves with it |
| `devsync config set conflict-policy ask` | whole store; reaches other PCs on next sync |
| `devsync config set skip-project-dirs archive,scratch` | whole store; folders under the root that are never scanned |

On Linux and macOS a project-key prefix starts with `-`, so pass it as
`--project-key-prefix=-home-me-code` (with `=`). The derived default is
normally right and needs no flag.

`memory-key-root` is fixed when a store is seeded, because every memory path in
the store is named after it.

The store's `manifest.json` holds the rest: which gitignored files count as
precious (`allowlistGlobs`), what is never synced (`blocklistGlobs`,
`generatedDirs`), which `~/.claude` folders are carried (`claudePaths`), and the
merge category per file extension. Edit it in the store and sync. A manifest that
is missing or is not valid JSON stops the sync with a message and syncs nothing;
devsync never guesses at what a store is allowed to hold.

## Daily use

| Command | What it does |
|---|---|
| `devsync sync` | the everyday command (exit 2 if a .conflict was written) |
| `devsync status` | ahead/behind, last commit, unresolved conflicts |
| `devsync conflicts` | list .conflict sidecars |
| `devsync resolve --all --keep local\|remote\|both` | settle them in one go |
| `devsync rm <path>` | explicit delete (tombstone; recoverable) |
| `devsync restore <path>` | pull back from ~/.devsync/trash |

Paths can be a store path (`claude/global/CLAUDE.md`, `repos/<project>/.env`), an
absolute path, or a path relative to where you are. A path devsync does not manage
is refused rather than silently accepted, and so is a folder or a name that
matches no file: `rm` removes one file at a time. To run it unattended, see [docs/AUTOMATION.md](docs/AUTOMATION.md).
`devsync monitor install` adds a daily check to the store repository that opens
an issue when a machine stops syncing.

## What gets synced

- **Claude Code config**: `CLAUDE.md`, `settings*.json`, `keybindings.json`,
  `skills/`, `commands/`, `hooks/`, `lib/`, `agents/`, `rules/`,
  `output-styles/`, and per-project `memory/*.md`. Memory is stored under a
  machine-independent name, so `C:\code` on one PC and `/home/me/code` on another
  merge into one tree. Skills that claude.ai syncs down itself
  (`skills/synced/`) are left alone.
- **Other coding agents** whose folder exists on the PC; see
  [Other coding agents](#other-coding-agents).
- **Precious gitignored files** (opt-in) in every git repo under the root,
  filtered by the allowlist. Generated folders (`node_modules`, `dist`, ...) are
  never descended. Per-repo overrides: `.syncinclude` / `.syncignore`.

Only the listed files and folders are collected, so session transcripts, plugin
runtime and the agent's own login file are not. Inside a synced folder, a new
store also skips files named like a login (`auth.json`, `credentials.json`,
`.env`, …) and cache folders (`__pycache__`, `node_modules`, `.venv`, …). A
pre-0.2 store has no such filter and syncs synced folders whole.

## Other coding agents

A new store also syncs the instruction material of these agents. Each is synced on
a PC only if its folder exists there, so an agent you do not use costs nothing and
no folder is ever created for it.

| Agent | Folder | What is synced |
|---|---|---|
| Codex | `~/.codex` (or `CODEX_HOME`) | `AGENTS.md` |
| Gemini CLI | `~/.gemini` | `GEMINI.md`, `commands/` |
| Kiro | `~/.kiro` | `steering/`, `agents/`, `skills/`, `hooks/` |

Deliberately not synced for these agents: their settings and MCP configuration
(they commonly hold tokens), logins and history. Files named `auth.json`,
`oauth_creds.json`, `credentials.json`, `history.jsonl` or `.env` are never
collected from an agent folder, even inside a folder you list.

Any other agent works the same way. Name it, point at its folder, and list what to
carry:

| Command | What it does |
|---|---|
| `devsync home add myagent ~/.myagent --files RULES.md --folders rules,prompts` | add an agent: its folder, and what to carry |
| `devsync home list` | every agent, its folder, and whether it is on this PC |
| `devsync home path gemini D:/tools/gemini` | this PC keeps it somewhere else |
| `devsync home remove myagent` | stop syncing it; nothing is deleted |

Only what you list is synced. In the store these files live under
`agents/<name>/`; Claude Code's stay under `claude/`. Per-project memory sync is
Claude Code only, because it depends on how Claude Code names its project folders.

Project-level instruction files (`AGENTS.md`, `.cursor/rules/` and the like) are
normally committed to the project's own repository, so git already carries them.

## Secrets

Gitignored repo files are secrets, so three things are true by default:

1. They are **not synced** unless you ask.
2. When you ask at `init`, they are **encrypted** before they reach the store.
3. Turning it on later with `devsync config set sync-repo-files on` is refused on
   an unencrypted store. Run `devsync encrypt --all-machines-upgraded` first, or
   say `plain` to accept plain text.

| Command | What it does |
|---|---|
| `pipx inject ai-devsync cryptography` | if you installed with pipx |
| `pip install cryptography` | if you installed with pip |
| `devsync init --seed --sync-repo-files --remote <url>` | prompts for a passphrase |

On every other PC, `adopt` asks for the same passphrase. Set `DEVSYNC_PASSPHRASE`
to supply it without a prompt. The passphrase is never stored; a key derived from
it is kept in `~/.devsync/key` on each PC so scheduled syncs can run. **A lost
passphrase cannot be recovered**, though every PC that already holds the key keeps
working and your live files are plaintext on your own disk as always.

What encryption covers: the content of `repos/**` in the store. Add more with
`devsync encrypt --paths "claude/**,agents/**"`, which works on a store that is
already encrypted too; `repos/**` always stays in the list. Each value must be a
store path under `repos/`, `claude/` or `agents/`; anything else is refused. Files
already in the store under a newly added path are re-encrypted by that same
command, including an agent's files that the PC running it does not have. File names,
sizes and commit history stay visible. Claude config and memory are left readable
so you can diff them; add `claude/**` to the paths if yours hold secrets.
`settings.json` can contain tokens in `env` blocks and is synced by default.

It uses AES-SIV with a scrypt-derived key. Encryption is deterministic, so an
unchanged file does not look modified on every sync; the trade-off is that someone
watching the store can tell when a file returns to earlier content.

To turn it on for a store that already exists:

| Command | What it does |
|---|---|
| `devsync encrypt --all-machines-upgraded` | on one PC |
| `devsync unlock` | on each other PC |

Every PC must run 0.2.0 or later first: an older build does not know the store is
encrypted. Earlier commits in the store's Git history keep the old plain-text
copies; rewrite or recreate the store if that matters. `--no-encrypt` at `init`, or `sync-repo-files plain`
later, stores repo files in plain text, which is only reasonable in a private
repository you control.

## Merge rules

| Category | Rule |
|---|---|
| text/markdown | 3-way line merge; both-sides-appended becomes a union |
| `.env*` | key-aware union; same key changed both sides: newer wins, loser recorded |
| JSON | normalize, 3-way merge, must re-parse, else keep local and write `.conflict` |
| binary | newer-mtime wins; loser goes to `~/.devsync/trash` |
| anything else (`.sh`, `.py`, `.ps1`, no extension, …) | not merged line by line: if only one side changed it, that side wins; if both did, the newer file wins whole and the other goes to `~/.devsync/trash` |

## Known limitations

- **`.env` comments and layout.** When a `.env` file changed on one PC and the
  store copy differs, the merged file is usually rewritten as plain `KEY=value`
  lines, and comments and blank lines are then dropped. (A file in which fewer
  than half the non-comment lines are `KEY=value` is merged line by line and keeps them.)
- **Multi-line values in `.env`.** A value that spans lines (a PEM block in
  quotes, say) is usually cut at its first line by a merge, under the same
  condition. Keep such values in a separate file, or keep that `.env` out of
  sync.
- **Conflict policy `ask`: a file conflicted twice.** If a file picks up a second
  conflict before the first is resolved, `devsync resolve` can leave conflict
  marker lines in it and still report success. After resolving, open the file and
  check it before the next sync, or use `conflict-policy newer`.
- **Your global gitignore applies to the store.** A file named in your global
  git ignore file (many list `.env`) is not committed to the store, so it does
  not sync, and nothing reports it.

## Conflicts

Most two-sided edits merge on their own. A *real* conflict is two PCs rewriting
the same lines. What happens next is the store's `conflict-policy`:

**`newer`** (the default for a new store). The newer side wins for the contested
lines, everything else from both sides is kept, and the sync carries on. Nothing
is lost: the other side's whole file is saved as
`~/.devsync/trash/<ts>/<path>.loser`, the sync prints where, and
`~/.devsync/conflicts.log` keeps a line per settled conflict. The summary shows
`autoresolved=N`.

**`ask`** (every store created before 0.2). The merged file keeps both versions
between `<<<<<<< LOCAL` / `>>>>>>> REMOTE` markers, a `<file>.conflict` sidecar
appears beside it, `devsync sync` exits 2, and the push is withheld. Then either:

| Command | What it does |
|---|---|
| `devsync resolve --all --keep remote` | or local, or both, for every conflict |
| `devsync resolve path/to/file --keep local` | one file, keeping this PC's side |

or edit the file yourself until the markers are gone and run
`devsync resolve path/to/file`. Use `resolve` rather than deleting the sidecar by
hand: it writes your resolution to the live file, the store and the merge base
together, so the same conflict cannot reappear on the next sync. The marked-up
version is kept in trash as `<path>.conflicted`.

Switch a store with `devsync config set conflict-policy newer` (or `ask`).

## State on each PC (not synced)

```
~/.devsync/machine.json     this PC's settings
~/.devsync/base/            merge ancestors (advanced after each sync)
~/.devsync/trash/<ts>/      every replaced/tombstoned file, 90-day retention
~/.devsync/devsync.lock     single-run guard
```

`DEVSYNC_HOME` moves this folder, for the tool and for both wrapper scripts. The
Linux systemd units do not pass it on; they use `~/.devsync`.

## Upgrading from 0.1

Nothing in an existing store or `machine.json` changes. A store whose manifest
predates 0.2 keeps its original behaviour: repo files sync, memory stays under
`CascadeProjects`, conflicts still wait for you (`ask`), and the original skip
list applies. The one behaviour change
is on a *new* join: a plain `adopt` of a PC that differs from the store now asks
for `--follower` or `--merge` instead of silently behaving like `--merge`.

## Tests

```
pip install -e ".[test]"
python -m pytest tests/
```

Includes two-machine end-to-end scenarios against a local bare remote.

## Platforms

Developed and used day to day on Windows 11. The test suite runs on Windows, Linux
and macOS in CI (`.github/workflows/tests.yml`), which also installs the tool into
an empty environment and walks this README step by step. macOS has no daily user
yet.

## License

MIT. See [LICENSE](LICENSE).
