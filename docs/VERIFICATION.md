# Verifying an install

Run this after installing devsync on a machine, or any time you want to re-prove
the install. Every step has an expected result; a deviation is something to
investigate, not a warning to skip.

Commands assume a POSIX shell (Git Bash on Windows). If `devsync` is not on PATH,
`python -m devsync` is the same entry point. `<store>` is the `store-dir` shown by
`devsync config`, and `<key-root>` is its `memory-key-root` (`projects` for a
store seeded with 0.2 or later, `CascadeProjects` for an older one).

## 1. Status is clean and converged

```bash
devsync status
```

**Expect:** `"ahead": 0, "behind": 0`, `"conflicts": []`, the right machine name
and a recent `lastCommit`. Non-zero ahead or behind means a sync has not
completed: run `devsync sync` and check again.

## 2. A new file reaches the store

Use a fresh scratch filename every run. Step 4 ends by tombstoning it, and a
tombstoned path is permanently excluded from sync, so a reused name silently does
nothing.

```bash
MEM=~/.claude/projects/<project-key-prefix>/memory     # prefix from `devsync config`
mkdir -p "$MEM"      # the folder exists only if the agent has been run in the root itself
SCRATCH="devsync-verify-$(hostname)-$(date +%Y%m%d%H%M%S).md"
printf -- "- fact 1\n- fact 2\n" > "$MEM/$SCRATCH"
devsync sync
cat <store>/claude/memory/<key-root>/"$SCRATCH"
```

**Expect:** sync prints `merged=1 … pushed=yes`, and the store copy matches the
live file exactly.

## 3. Two-sided edits are both kept

Simulate another PC by committing directly to the store remote, then edit the same
file locally with a different line:

```bash
TMP=$(mktemp -d)
git clone -q <your-store-remote-url> "$TMP/store2"
echo "- fact A (simulated other PC)" >> "$TMP/store2/claude/memory/<key-root>/$SCRATCH"
git -C "$TMP/store2" -c user.email=v@t -c user.name=v commit -aqm "simulated other-PC edit"
git -C "$TMP/store2" push -q origin main

echo "- fact B (this PC)" >> "$MEM/$SCRATCH"
devsync sync
cat "$MEM/$SCRATCH"
rm -rf "$TMP"
```

**Expect:** the live file holds fact 1, fact 2, fact A and fact B, with
`conflicts=0`. `replaced=1` is normal: the pre-merge local copy went to
`~/.devsync/trash` first.

## 4. Delete, recover, delete

```bash
devsync rm claude/memory/<key-root>/"$SCRATCH"
ls "$MEM/$SCRATCH"                      # expect: No such file
find ~/.devsync/trash -name "$SCRATCH"  # expect: at least one copy
devsync restore claude/memory/<key-root>/"$SCRATCH"
cat "$MEM/$SCRATCH"                     # expect: full content back
devsync rm claude/memory/<key-root>/"$SCRATCH"      # clean up
```

**Expect:** `rm` removes the live and store copies and appends to
`tombstones.json`; `restore` brings the file back from trash byte for byte, clears
its tombstone and syncs, so the second `rm` finds it in the store again.

## 5. The store holds what this machine has

```bash
cd <store>
git ls-files claude/global/                                   # CLAUDE.md, settings, keybindings
git ls-files claude/memory/ | cut -d/ -f3 | sort | uniq -c    # one entry per local memory key
git ls-files agents/ | cut -d/ -f2 | sort | uniq -c           # other agents, if you use any
git fetch -q origin main && git rev-parse HEAD origin/main    # two identical hashes
```

If repo-file sync is on, `git ls-files repos/ | head` lists your gitignored files.
On an encrypted store their content begins with `DEVSYNC-ENC1`:

```bash
head -c 12 "$(git ls-files repos/ | head -1)"
```

## 6. A second sync changes nothing

```bash
devsync sync
```

**Expect:** `merged=0 replaced=0 conflicts=0 … pushed=no`. A second consecutive
sync that still reports changes means the merge is not converging; find out why
before scheduling it.

## Exit codes, for scripts

| Command | 0 | 2 | Other |
|---|---|---|---|
| `devsync sync` | clean | a conflict needs you; push withheld | 1: lock held, offline, or a failure |
| `devsync conflicts` | none | some (paths printed) | |
| `devsync adopt` | joined | | 3: this PC differs from the store; choose `--follower` or `--merge` |

## Things that are expected, not faults

- **Folders that are not git repositories are not scanned.** Repo-file sync finds
  gitignored files through git. A folder of secrets with no `.git` is invisible to
  it; `git init` the folder.
- **Per-project files outside the allowlist.** A gitignored file that is not a
  `.env`, key or certificate (a project's `.claude/settings.json`, say) is not
  picked up by default. Add its name to a `.syncinclude` file in that repo.
- **A burst of `replaced=N` on the first sync of a Windows machine.** The store is
  LF. Files with CRLF line endings are normalized once, and every original is kept
  in `~/.devsync/trash/<ts>/`.
- **Two project folders that are the same directory** (one a symlink or junction
  to the other) are collected once, under the primary key.
