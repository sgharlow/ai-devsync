# Running devsync unattended

`devsync sync` is safe to run on a schedule: it takes a lock, never deletes by
inference, and withholds its push when something needs a person. This page covers
the scheduled task on Windows, the systemd units on Linux, how to read the log,
and why the task's own result is not a monitor.

The installers and wrappers below live in the `scripts/` folder of this repository.
A `pipx install` does not include them, so clone the repository to use them; the
wrappers work with a pipx-installed `devsync`.

## Windows: a per-user scheduled task

The installer registers a task named **`devsync-sync`** that runs
`scripts/devsync-sync-run.ps1` at logon and every 4 hours.

```powershell
cd <your-clone-of-devsync>
powershell -ExecutionPolicy Bypass -File .\scripts\install-devsync-task.ps1
```

It is idempotent: running it again replaces the task rather than stacking a second
one, and it prints which launcher it chose. To change the cadence pass
`-IntervalHours <n>`. To remove the task:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-devsync-task.ps1 -Uninstall
```

### Two launch paths

The installer picks the launcher by whether `scripts/run-hidden.vbs` is present:

| `run-hidden.vbs` on disk | Task action | Visible effect |
|---|---|---|
| yes | `wscript.exe run-hidden.vbs powershell.exe … -File devsync-sync-run.ps1` | no window |
| no | `powershell.exe -WindowStyle Hidden … -File devsync-sync-run.ps1` | a brief flash |

**Antivirus and the `.vbs` launcher.** Some antivirus products treat a
hidden-window `.vbs` launcher as suspicious, and at least one has responded by
deleting the launcher, the scripts it called and the scheduled tasks that
referenced them. If yours does, keep the `.vbs` off disk and let the installer use
the second path:

```bash
MSYS_NO_PATHCONV=1 git sparse-checkout set --no-cone '/*' '!/scripts/run-hidden.vbs' '!/scripts/devsync-sync.ps1'
```

`MSYS_NO_PATHCONV=1` is required in Git Bash; without it the patterns are silently
rewritten to paths under the Git install and exclude nothing. Check with
`cat .git/info/sparse-checkout`. `scripts/devsync-sync.ps1` is only a forwarder to
`devsync-sync-run.ps1`, kept for tasks registered under the older name.

### Why these choices

**Every 4 hours, not every few minutes.** The goal is convergence within a working
day. Tighter mostly produces lock contention, and a machine with nothing to
contribute adds at most one commit per day anyway (see the heartbeat stamp below).

**Finds devsync however you installed it.** The wrapper uses `python -m devsync.cli`
when the Python on PATH can import it (a `pip install`), and otherwise the
`devsync` command (a `pipx install`). The check is made from the folder the sync
runs in, so a clone of this repository sitting elsewhere does not confuse it.
`DEVSYNC_CMD` overrides both.

**Runs only when you are logged on.** `git push` needs your credential store. A
task set to run "whether the user is logged on or not" does not reliably have it
and fails in a way that looks like a devsync fault.

**`StartWhenAvailable`, and allowed on battery.** A laptop asleep at the scheduled
minute runs on wake instead of skipping the slot. The Task Scheduler defaults stop
a task on battery power and log nothing; the installer overrides them.

## Linux: systemd units

```bash
./scripts/install-devsync-systemd.sh        # asks for sudo
```

This writes system units to `/etc/systemd/system` that run as your user: a timer
that runs `scripts/devsync-sync.sh` every 4 hours, and a service that syncs at boot
(after the network is up) and at shutdown. System units are used because user
units only run while you are logged in. The wrapper logs the same outcomes as the
Windows one, with the trigger (`boot`, `timer`, `shutdown`) added to each line.

The wrapper finds devsync by itself: the `devsync` command that `pipx install`
puts on your PATH, or a Python that can import it. Set `DEVSYNC_CMD` to point at a
specific executable. Git needs a non-interactive
credential: `gh auth login && gh auth setup-git`, or an SSH remote with an agent.

macOS has no installer yet. `scripts/devsync-sync.sh` runs there, so a launchd
agent or a cron entry calling it every few hours does the same job.

## Reading the log

`~/.devsync/sync.log` holds one line per run, in UTC, rotated at about 4000 lines.

```
2026-01-15T03:03:32Z  OK        devsync: merged=0 replaced=0 conflicts=0 tombstoned=0 pushed=no
2026-01-15T07:01:43Z  SKIP      another sync holds the lock
2026-01-15T11:01:54Z  OFFLINE   no network / remote unreachable -- will retry next run
```

| Outcome | Exit | Meaning |
|---|---|---|
| `OK` | 0 | clean |
| `SKIP` | 0 | a manual or overlapping run held the lock; the lock did its job |
| `OFFLINE` | 0 | no network. Normal on a laptop |
| `CONFLICT` | 2 | `.conflict` sidecars written, or still unresolved from an earlier run. The sync succeeded but the push is withheld until you resolve them |
| `ERROR` | 1 | something broke, including `AUTH`: git credentials missing or rejected |

`devsync sync` itself returns exit 1 for three unrelated things: the lock is held,
the machine is offline, or there was a real failure. The exit code cannot tell
them apart, so the wrapper classifies by message. That is the reason it exists: a
caller that treats them alike either ignores real errors or goes red every time
the laptop is off Wi-Fi.

On `CONFLICT`:

```powershell
python -m devsync conflicts                          # lists the sidecars
python -m devsync resolve --all --keep remote        # or local / both; then it syncs
```

A store with `conflict-policy newer` (the default for new stores) settles
conflicts itself and never logs `CONFLICT`; see the README.

## Checking it is alive

```powershell
Get-ScheduledTaskInfo -TaskName devsync-sync | Select LastRunTime, LastTaskResult, NextRunTime
Get-Content ~\.devsync\sync.log -Tail 10
```

**`LastTaskResult: 0` is a weak signal.** The wrapper exits 0 for `OK`, `SKIP` and
`OFFLINE` alike, so a machine that has been offline for a week shows a green task
and a log full of `OFFLINE`. Read the log, not the task result.

## The task is not a monitor

A watchdog that lives on the machine it watches dies with it. The useful question
is "has every machine synced recently?", and only something off the machines can
answer it.

devsync makes that answerable from the store's Git history:

- every sync that changes anything commits as `sync <HOSTNAME> <timestamp>`;
- a sync with nothing to contribute would commit nothing and look exactly like a
  dead machine, so once a day such a machine writes `.heartbeat/<HOSTNAME>.json`
  and commits that instead.

So the newest `sync <HOSTNAME>` commit is each machine's last-seen time, and
devsync ships a check that reads it:

```
devsync monitor install
```

This copies two files into your store repository and syncs: `.github/heartbeat_check.py`
(the measurement, plain Python with no dependencies) and
`.github/workflows/sync-heartbeat.yml` (a daily GitHub Actions job that runs it and
opens or closes issues labelled `devsync-heartbeat`). The defaults are 48 hours for
"nothing anywhere is syncing" and 72 hours per machine, which clears a weekend.
Each machine gets its own issue, so one going quiet never hides behind another
that is still syncing.

GitHub rejects a push that adds a workflow file from a credential without the
`workflow` scope. If `devsync monitor install` reports a failed push, sign in again
with that scope (`gh auth refresh -s workflow`) and run `devsync sync`.

Retire a machine, or give one a longer leash, with
`.github/heartbeat-machines.json` in the store:

```json
{ "retired": ["OLD-PC"], "thresholds": { "LAPTOP": 168 } }
```

A machine that is not listed is watched at the default: absent must never mean
ignored, or a new PC goes unmonitored until someone remembers to add it.

**Rehearse it once before relying on it.** A monitor nobody has seen fire is not a
monitor:

```
gh workflow run sync-heartbeat.yml -f machine_threshold_hours=-1
```

Use `-1`, not `0`: a sync that just landed is 0 hours old, and `0 > 0` is false, so
a drill at 0 passes while looking healthy. The next ordinary run closes the issues
the drill opened.

On a Git host other than GitHub, schedule `python .github/heartbeat_check.py
--exit-code` there; it exits 2 when anything is stale.

## Testing a change to the wrapper

Prove a scheduled script in the host the task registers, not in your own shell.
The task runs `powershell.exe` (Windows PowerShell 5.1). PowerShell 7 (`pwsh`)
handles a native command's stderr differently: under `$ErrorActionPreference =
'Stop'`, 5.1 turns the first stderr line into a terminating error, which once made
both quiet outcomes (`SKIP` and `OFFLINE`) log as `ERROR` while every check run
under `pwsh` passed. The wrapper now sends stderr to a file for that reason.

`Start-ScheduledTask` cannot show this class of fault either: an overlap needs a
concurrent manual sync and `OFFLINE` needs no network, and neither occurs during a
forced run. `tests/test_adoption.py` runs the wrapper under `powershell.exe` with
a stand-in `python` to cover the `AUTH` and `OFFLINE` branches.
