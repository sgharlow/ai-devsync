#!/usr/bin/env bash
# Runs one devsync sync, classifies the outcome, and appends a single line to a log.
#
# Linux port of scripts/devsync-sync.ps1, called by the systemd units that
# scripts/install-devsync-systemd.sh writes to /etc/systemd/system.
# It exists for the same reason: `devsync sync` returns exit 1 for unrelated situations
# (lock held, offline, auth broken, a real fault), and an automated caller that treats
# them alike is either useless or a nuisance. Classification is by message.
#
# It is not the monitor. The heartbeat check in the store repository
# (.github/workflows/sync-heartbeat.yml, added by `devsync monitor install`) runs off every
# machine and alerts on the ABSENCE of sync commits; this script's job is to run reliably
# and leave a readable trail.
#
# Usage: devsync-sync.sh [trigger]    trigger is a free-form label for the log (boot,
#                                     shutdown, timer, manual)
# Exit codes:
#   0  clean, or a benign skip/offline
#   2  conflicts written as .conflict sidecars -- NOT a failure; a human has work to do
#   1  a real error (including AUTH: credentials missing or rejected)

set -u

TRIGGER="${1:-manual}"
# How devsync is started, first match wins:
#   DEVSYNC_CMD                  an explicit command, used as given
#   DEVSYNC_PY                   a Python that can import it (default ~/.devsync-venv/bin/python)
#   devsync on PATH              what `pipx install` gives you (also ~/.local/bin/devsync)
#   python3 -m devsync.cli       a plain `pip install`
DEVSYNC_PY="${DEVSYNC_PY:-$HOME/.devsync-venv/bin/python}"
if [ -n "${DEVSYNC_CMD:-}" ]; then
  RUN=("$DEVSYNC_CMD")
elif [ -x "$DEVSYNC_PY" ]; then
  RUN=("$DEVSYNC_PY" -m devsync.cli)
elif command -v devsync >/dev/null 2>&1; then
  RUN=("$(command -v devsync)")
elif [ -x "$HOME/.local/bin/devsync" ]; then
  RUN=("$HOME/.local/bin/devsync")
elif python3 -c "import devsync.cli" >/dev/null 2>&1; then
  RUN=(python3 -m devsync.cli)
else
  RUN=()
fi
# Per-PC devsync state. DEVSYNC_HOME moves it, exactly as it does for the tool.
DS_HOME="${DEVSYNC_HOME:-$HOME/.devsync}"
LOG_PATH="${DEVSYNC_LOG:-$DS_HOME/sync.log}"
KEEP_LINES="${DEVSYNC_LOG_KEEP:-4000}"
# The dev root comes from ~/.devsync/machine.json (cascadeRoot), the per-machine record devsync
# itself reads; ~/CascadeProjects is only the fallback when that file is missing or unreadable.
# An explicit CASCADE_ROOT in the environment still wins.
machine_root() {
  local mj="$DS_HOME/machine.json"
  [ -r "$mj" ] || return 1
  local r py
  py="$DEVSYNC_PY"; [ -x "$py" ] || py="$(command -v python3)" || return 1
  r="$("$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["cascadeRoot"])' "$mj" 2>/dev/null)" || return 1
  [ -n "$r" ] || return 1
  printf '%s\n' "$r"
}
CASCADE_ROOT="${CASCADE_ROOT:-$(machine_root || echo "$HOME/CascadeProjects")}"

log_line() {
  local outcome="$1" detail="$2" line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ)  $(printf '%-8s' "$outcome")  [$TRIGGER] $detail"
  mkdir -p "$(dirname "$LOG_PATH")"
  printf '%s\n' "$line" >> "$LOG_PATH"
  # Trim only once it has grown past the cap, so most runs never rewrite the file.
  if [ "$(wc -l < "$LOG_PATH")" -gt $((KEEP_LINES + 500)) ]; then
    tail -n "$KEEP_LINES" "$LOG_PATH" > "$LOG_PATH.tmp" && mv "$LOG_PATH.tmp" "$LOG_PATH"
  fi
  printf '%s\n' "$line"   # also lands in the journal when run under systemd
}

if [ "${#RUN[@]}" -eq 0 ]; then
  log_line ERROR "devsync not found: install it (pipx install ...), or set DEVSYNC_CMD or DEVSYNC_PY"
  exit 1
fi
cd "$CASCADE_ROOT" 2>/dev/null || { log_line ERROR "CascadeRoot not found: $CASCADE_ROOT"; exit 1; }

# Never let git block on a prompt: an unattended run with no credential must fail, not hang
# until systemd kills it (at shutdown that would stall the whole poweroff).
export GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS

text="$("${RUN[@]}" sync 2>&1)"
code=$?

summary="$(printf '%s\n' "$text" | grep '^devsync:' | tail -n 1)"
[ -n "$summary" ] || summary="$(printf '%s\n' "$text" | grep -v '^\s*$' | tail -n 1)"
summary="$(printf '%s' "$summary" | tr -s '[:space:]' ' ')"

case "$code" in
  0)
    log_line OK "$summary"
    exit 0 ;;
  2)
    # The sync SUCCEEDED and produced work for a human.
    log_line CONFLICT "$summary -- run: devsync conflicts"
    exit 2 ;;
esac

if printf '%s' "$text" | grep -q 'already running (pid'; then
  log_line SKIP "another sync holds the lock"
  exit 0
fi

# AUTH before OFFLINE. Both surface as "git pull failed"; a missing or revoked credential
# logged as OFFLINE would stay green forever. It is a real break that needs a person.
if printf '%s' "$text" | grep -qiE 'could not read (Username|Password)|terminal prompts disabled|Authentication failed|Invalid username or password|Permission denied \(publickey\)|The requested URL returned error: 40[13]|Repository not found'; then
  log_line ERROR "AUTH: git credentials missing or rejected -- run: gh auth login && gh auth setup-git"
  exit 1
fi

if printf '%s' "$text" | grep -qiE 'Could not resolve host|unable to access|Connection (timed out|refused)|Network is unreachable|Failed to connect'; then
  log_line OFFLINE "no network / remote unreachable -- will retry next run"
  exit 0
fi

# A traceback puts the line that matters LAST (the unindented exception line); anything
# else is joined so a multi-line message is not cut to a fragment.
if printf '%s' "$text" | grep -q 'Traceback (most recent call last)'; then
  detail="$(printf '%s\n' "$text" | grep -v '^\s' | grep -v 'Traceback (most recent call last)' | grep -v '^\s*$' | tail -n 1)"
else
  detail="$(printf '%s\n' "$text" | grep -v '^\s*$' | tr '\n' ' ')"
fi
detail="$(printf '%s' "$detail" | tr -s '[:space:]' ' ')"
[ -n "$detail" ] || detail="unclassified failure (exit $code); see: journalctl -u 'devsync-*'"
[ "${#detail}" -le 300 ] || detail="${detail:0:297}..."
log_line ERROR "$detail"
exit 1
