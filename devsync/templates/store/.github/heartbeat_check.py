#!/usr/bin/env python3
"""Has every machine synced this devsync store recently?

devsync's success signal is a side effect: a commit whose subject is
`sync <MACHINE> <timestamp>` landing in the store. When syncing stops, nothing on
the machines says so -- each one keeps working from its own files and they drift
apart quietly. This script measures the ABSENCE of that commit, from the store's
own history, so it can run somewhere that is not one of the machines.

Two measures:
  * global      -- the newest sync commit from ANY machine (nothing is syncing)
  * per machine -- the newest sync commit from EACH machine (one box stopped while
                   the others carry on, which the global measure cannot see)

Machines are discovered from history, so a new PC is watched from its first sync.
An optional `.github/heartbeat-machines.json` retires a machine or gives it its own
threshold:

    { "retired": ["OLD-PC"], "thresholds": { "LAPTOP": 168 } }

A machine that is not listed is watched at the default. Absent never means ignored.

Standalone and stdlib-only on purpose: it is copied into the store repository and
runs there with whatever Python the runner has.

    python .github/heartbeat_check.py                 # print the table
    python .github/heartbeat_check.py --exit-code     # exit 2 if anything is stale

To rehearse the alert path use a threshold of -1, not 0: a sync that just landed is
0 hours old, and `0 > 0` is false, so a drill at 0 passes while looking healthy.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def sync_commits(repo: Path) -> list[tuple[int, str]]:
    """(commit epoch, machine) for every `sync <MACHINE> ...` commit, newest first."""
    r = subprocess.run(["git", "-C", str(repo), "log", "--format=%ct%x09%s", "--grep=^sync "],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        epoch, _, subject = line.partition("\t")
        parts = subject.split()
        if epoch.isdigit() and len(parts) >= 2 and parts[0] == "sync":
            out.append((int(epoch), parts[1]))
    return out


def iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def measure(repo: Path, now: int, threshold: int, machine_threshold: int,
            config: dict | None = None) -> dict:
    config = config or {}
    retired = set(config.get("retired") or [])
    overrides = config.get("thresholds") or {}
    commits = sync_commits(repo)
    if not commits:
        return {"error": "no sync commits found in this store"}

    newest: dict[str, int] = {}
    for epoch, machine in commits:
        newest[machine] = max(epoch, newest.get(machine, 0))
    last = max(newest.values())
    age = (now - last) // 3600

    machines = []
    for name in sorted(newest):
        m_age = (now - newest[name]) // 3600
        if name in retired:
            state, limit = "retired", None
        else:
            limit = int(overrides.get(name, machine_threshold))
            state = "stale" if m_age > limit else "fresh"
        machines.append({"machine": name, "age_hours": m_age, "last": iso(newest[name]),
                         "threshold": limit, "state": state})
    return {"age_hours": age, "last": iso(last), "threshold": threshold,
            "stale": age > threshold, "machines": machines}


def render_table(result: dict) -> str:
    rows = ["| Machine | Last sync | Timestamp | Threshold |", "|---|---|---|---|"]
    for m in result["machines"]:
        limit = "retired" if m["state"] == "retired" else f"{m['threshold']}h"
        rows.append(f"| `{m['machine']}` | {m['age_hours']}h | `{m['last']}` | {limit} |")
    return "\n".join(rows)


def write_github_output(result: dict, path: str):
    stale = " ".join(f"{m['machine']}:{m['age_hours']}:{m['threshold']}:{m['last']}"
                     for m in result["machines"] if m["state"] == "stale")
    fresh = " ".join(f"{m['machine']}:{m['age_hours']}:{m['last']}"
                     for m in result["machines"] if m["state"] == "fresh")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"stale={'true' if result['stale'] else 'false'}\n")
        f.write(f"age_hours={result['age_hours']}\n")
        f.write(f"last_iso={result['last']}\n")
        f.write(f"threshold={result['threshold']}\n")
        f.write(f"stale_machines={stale}\n")
        f.write(f"fresh_machines={fresh}\n")
        f.write("per_machine<<DEVSYNC_TABLE\n" + render_table(result) + "\nDEVSYNC_TABLE\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--threshold", type=int, default=48,
                    help="hours with no sync from any machine (default 48)")
    ap.add_argument("--machine-threshold", type=int, default=72,
                    help="hours with no sync from one machine (default 72)")
    ap.add_argument("--config", default=None,
                    help="default: <repo>/.github/heartbeat-machines.json if present")
    ap.add_argument("--now", type=int, default=None, help="epoch seconds; for testing")
    ap.add_argument("--exit-code", action="store_true", help="exit 2 when anything is stale")
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    cfg_path = Path(args.config) if args.config else repo / ".github" / "heartbeat-machines.json"
    config = {}
    if cfg_path.is_file():
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except ValueError:
            print(f"warning: {cfg_path} is not valid JSON; watching every machine at the default",
                  file=sys.stderr)

    result = measure(repo, args.now if args.now is not None else int(time.time()),
                     args.threshold, args.machine_threshold, config)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    print(f"last sync from any machine: {result['last']} ({result['age_hours']}h ago, "
          f"threshold {result['threshold']}h){'  >>> STALE' if result['stale'] else ''}")
    for m in result["machines"]:
        if m["state"] == "retired":
            print(f"  {m['machine']}: {m['age_hours']}h ago (retired, not watched)")
        else:
            flag = "  >>> STALE" if m["state"] == "stale" else ""
            print(f"  {m['machine']}: {m['age_hours']}h ago (threshold {m['threshold']}h){flag}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        write_github_output(result, gh_out)
    any_stale = result["stale"] or any(m["state"] == "stale" for m in result["machines"])
    return 2 if (args.exit_code and any_stale) else 0


if __name__ == "__main__":
    sys.exit(main())
