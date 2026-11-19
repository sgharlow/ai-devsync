<#
.SYNOPSIS
  Runs one devsync sync, classifies the outcome, and appends a single line to a log.

.DESCRIPTION
  The wrapper the scheduled task calls. It exists because `python -m devsync.cli sync`
  returns exit 1 for three completely different situations, and an automated caller that
  treats them alike is either useless or a nuisance:

    * another sync already holds the lock  -> benign, skip, say nothing loud
    * the machine is offline               -> benign and EXPECTED on a laptop
    * something actually broke             -> worth surfacing

  Classification is by the message, because the exit code cannot tell them apart.

  WHAT THIS IS NOT. It is not the monitor. The monitor is the dead-man's-switch in
  the store repository (.github/workflows/sync-heartbeat.yml, added by `devsync monitor
  install`), which runs off the machines it watches and
  alerts on the ABSENCE of sync commits. This wrapper's job is to run reliably, never spam,
  and leave a readable trail. If this script silently stops working, the heartbeat is what
  notices — which is the correct division, because a watchdog that lives on the machine it
  watches dies with it.

.NOTES
  Exit codes (what Task Scheduler shows as Last Run Result):
    0  clean, or a benign skip/offline
    2  conflicts written as .conflict sidecars -- NOT a failure; a human has work to do
    1  a real error
#>

[CmdletBinding()]
param(
  # Where the repos live. devsync resolves its own paths from ~/.devsync/machine.json;
  # this is only the working directory the CLI is invoked from. Empty = read it from
  # machine.json too (see below).
  [string]$CascadeRoot = '',

  # Beside the rest of the per-PC devsync state, not in a repo: it is machine-local and
  # must never sync.
  [string]$LogPath = (Join-Path $env:USERPROFILE '.devsync\sync.log'),

  # Rotation by line count. A year of 4-hourly runs is ~2200 lines, so this keeps well
  # over a year and stays greppable.
  [int]$KeepLines = 4000
)

# The dev root comes from ~/.devsync/machine.json (cascadeRoot), the per-machine record devsync
# itself reads. ~\CascadeProjects is only the fallback when that file is missing or unreadable.
# Per-PC devsync state. DEVSYNC_HOME moves it, exactly as it does for the tool.
$devsyncHome = Join-Path $env:USERPROFILE '.devsync'
if ($env:DEVSYNC_HOME) { $devsyncHome = $env:DEVSYNC_HOME }
if (-not $PSBoundParameters.ContainsKey('LogPath')) { $LogPath = Join-Path $devsyncHome 'sync.log' }

if (-not $CascadeRoot) {
  $CascadeRoot = Join-Path $env:USERPROFILE 'CascadeProjects'
  try {
    $machineJson = Join-Path $devsyncHome 'machine.json'
    $fromMachine = (Get-Content -Raw -LiteralPath $machineJson -ErrorAction Stop | ConvertFrom-Json).cascadeRoot
    if ($fromMachine) { $CascadeRoot = $fromMachine }
  } catch { }
}

$ErrorActionPreference = 'Stop'

function Write-LogLine {
  param([string]$Outcome, [string]$Detail)

  $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  $line  = '{0}  {1,-8}  {2}' -f $stamp, $Outcome, $Detail

  $dir = Split-Path -Parent $LogPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  Add-Content -Path $LogPath -Value $line -Encoding utf8

  # Trim in place, occasionally. Reading the whole file every run would be wasteful; this
  # only rewrites once it has actually grown past the cap.
  try {
    $lines = @(Get-Content -Path $LogPath -ErrorAction Stop)
    if ($lines.Count -gt ($KeepLines + 500)) {
      $lines | Select-Object -Last $KeepLines | Set-Content -Path $LogPath -Encoding utf8
    }
  } catch { }

  Write-Output $line
}

if (-not (Test-Path $CascadeRoot)) {
  Write-LogLine -Outcome 'ERROR' -Detail "CascadeRoot not found: $CascadeRoot"
  exit 1
}

# Which command starts devsync.
#   pip install      -> the Python on PATH can import it: python -m devsync.cli
#   pipx install     -> it lives in its own environment: the `devsync` command on PATH
#   DEVSYNC_CMD      -> an explicit executable, used as given
$devsyncExe = 'python'
$devsyncArgs = @('-m', 'devsync.cli', 'sync')
if ($env:DEVSYNC_CMD) {
  $devsyncExe = $env:DEVSYNC_CMD
  $devsyncArgs = @('sync')
} else {
  $canImport = $false
  # Checked from the folder the sync will run in. Checked from anywhere else -- a clone of
  # this repository, say -- the import can succeed there and then fail for the real run.
  Push-Location $CascadeRoot
  try {
    $ErrorActionPreference = 'Continue'
    & python -c 'import devsync.cli' 2>$null | Out-Null
    $canImport = ($LASTEXITCODE -eq 0)
  } catch { $canImport = $false }
  Pop-Location
  $ErrorActionPreference = 'Stop'
  if (-not $canImport -and (Get-Command devsync -ErrorAction SilentlyContinue)) {
    $devsyncExe = 'devsync'
    $devsyncArgs = @('sync')
  }
}

# How devsync is started (decided above): `python -m devsync.cli` when the Python on PATH can
# import it, else the `devsync` command. A bare `devsync` is not assumed: a pip --user install
# puts it in a per-user Scripts directory that is often not on PATH, and that fails silently.
Push-Location $CascadeRoot
$errFile = [System.IO.Path]::GetTempFileName()
try {
  # ⚠️ Stderr goes to a FILE, not `2>&1`. With $ErrorActionPreference = 'Stop' (set
  # above), the first stderr line of a NATIVE command becomes a TERMINATING error:
  # the catch below fires and the text this script classifies on is thrown away, so
  # `already running (pid` never matches and a benign lock collision is logged as
  # ERROR with a red Last Run Result -- crying wolf every time the task overlaps a
  # manual sync, which is the exact opposite of what the lock branch is for.
  #
  # With this construct the lock path logged
  # "Traceback (most recent call last):" and exited 1. Note a one-off
  # Start-ScheduledTask CANNOT catch this -- an overlap only happens when a manual
  # sync coincides with the task, so the registration looks green either way.
  $ErrorActionPreference = 'Continue'
  $out  = & $devsyncExe @devsyncArgs 2>$errFile
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
} catch {
  $ErrorActionPreference = 'Stop'
  Write-LogLine -Outcome 'ERROR' -Detail "could not invoke python: $($_.Exception.Message)"
  Pop-Location
  Remove-Item $errFile -Force -ErrorAction SilentlyContinue
  exit 1
}
Pop-Location

$stderrText = ''
if (Test-Path $errFile) {
  $stderrText = [System.IO.File]::ReadAllText($errFile)
  Remove-Item $errFile -Force -ErrorAction SilentlyContinue
}
# Stderr appended LAST on purpose: PowerShell may still wrap a native failure in an
# error record whose "+ CategoryInfo / + FullyQualifiedErrorId" decoration lands in
# $out. Keeping the real diagnostics last means the ERROR branch's
# "last non-empty line" picks the actual exception, not the decoration.
$text = (($out | Out-String) + "`n" + $stderrText).Trim()

# The tool's own one-line summary, when it got far enough to print one.
$summary = ($text -split "`n" | Where-Object { $_ -match '^devsync:' } | Select-Object -Last 1)
if (-not $summary) { $summary = ($text -split "`n" | Select-Object -Last 1) }
$summary = ($summary -replace '\s+', ' ').Trim()

switch ($code) {

  0 {
    Write-LogLine -Outcome 'OK' -Detail $summary
    exit 0
  }

  2 {
    # Conflicts were written. The sync SUCCEEDED and produced work for a human; treating
    # this as a failure would report a break that did not happen.
    Write-LogLine -Outcome 'CONFLICT' -Detail "$summary -- run: python -m devsync.cli conflicts"
    exit 2
  }

  default {
    # Exit 1 covers three unrelated situations. Classify by message.
    if ($text -match 'already running \(pid') {
      # A manual sync, or an overlapping scheduled run. The lock did its job.
      Write-LogLine -Outcome 'SKIP' -Detail 'another sync holds the lock'
      exit 0
    }

    # AUTH before OFFLINE. Both surface as "git pull failed", so without this branch a
    # rejected or missing credential is logged OFFLINE with exit 0 and the task stays
    # green while nothing syncs. Same patterns as scripts/devsync-sync.sh.
    if ($text -match 'could not read (Username|Password)' -or
        $text -match 'terminal prompts disabled' -or
        $text -match 'Authentication failed' -or
        $text -match 'Invalid username or password' -or
        $text -match 'Permission denied \(publickey\)' -or
        $text -match 'The requested URL returned error: 40[13]' -or
        $text -match 'Repository not found') {
      Write-LogLine -Outcome 'ERROR' -Detail 'AUTH: git credentials missing or rejected -- run: gh auth login; gh auth setup-git'
      exit 1
    }

    # Only messages that mean "no network". `git pull failed` on its own is NOT here: it
    # is the prefix of every pull failure, and matching it logged real faults as OFFLINE.
    if ($text -match 'Could not resolve host' -or
        $text -match 'unable to access' -or
        $text -match 'Connection (timed out|refused)' -or
        $text -match 'network is unreachable' -or
        $text -match 'Failed to connect') {
      # Normal for a laptop. Deliberately exit 0: a red Task Scheduler result every time
      # the machine is off Wi-Fi trains everyone to ignore the one that matters.
      # Sustained absence is the heartbeat's job, not this line's.
      Write-LogLine -Outcome 'OFFLINE' -Detail 'no network / remote unreachable -- will retry next run'
      exit 0
    }

    # ⚠️ PowerShell wraps a native failure in an error record whose decoration lines
    # ("+ CategoryInfo", "+ FullyQualifiedErrorId", "At <file>:<n> char:", "~~~~~") come
    # LAST, so a plain "last non-empty line" logs "+ FullyQualifiedErrorId :
    # NativeCommandError" and hides the actual cause. Observed against the
    # not-initialized path. This is the one outcome that needs
    # a human, and the log line is the signal -- so skip the decoration.
    $decoration = '^\s*\+|^At\s.+:\d+\s+char:|CategoryInfo|FullyQualifiedErrorId|^\s*~+\s*$'
    $real = @($text -split "`n" | Where-Object { $_ -match '\S' -and $_ -notmatch $decoration })

    <#
      ⚠️ JOIN the surviving lines, do not pick one of them.

      Windows PowerShell 5.1 wraps a native command's error record AT CONSOLE WIDTH, so a
      single-line diagnostic arrives split across several lines:

        [python.exe : devsync: not initialized - run `devsync init --seed` (first machine) or ]
        [machine)]

      Picking the LAST non-decoration line logged "machine)"; picking the FIRST would truncate
      mid-sentence. Both are wrong, and — worse — WHICH one is wrong depends on how wide the
      console happened to be, so the same build produced a useful line on one machine and a
      fragment on another (two machines, two console widths). A width-dependent log line is not a log
      line you can trust.

      Joining the fragments reconstructs the message regardless of width. The `python.exe : `
      prefix PowerShell prepends is stripped because it names the host, not the fault.
    #>
    <#
      ...EXCEPT when the text is a Python traceback, where joining is the wrong move.

      A traceback puts the one line that matters LAST, behind frame lines that are pure
      boilerplate. Joining then truncating at 300 chars logged this:

        python.exe : Traceback (most recent call last): File "...cli.py", line 198, in main
        summary = engine.sync() File "...engine.py", line 340, in sync self._reconcile_...

      -- 300 characters of frames with the actual `ValueError: ...` cut off the end. Correct
      classification, useless line, on the one outcome that needs a human.

      So the two shapes need opposite handling, and the discriminator is indentation:
      traceback FRAME lines are indented, the exception line is not. Wrapped-fragment lines
      are not indented either, which is why "last unindented line" alone is not enough and
      the `Traceback` marker has to gate it.

        wrapped single line -> JOIN the fragments      (the case above)
        traceback           -> take the last UNINDENTED line = the exception
    #>
    $isTraceback = $text -match 'Traceback \(most recent call last\)'
    $detail = ''

    if ($isTraceback) {
      $exc = @($real | Where-Object { $_ -notmatch '^\s' -and $_ -notmatch 'Traceback \(most recent call last\)' })
      if ($exc.Count -gt 0) { $detail = $exc[-1] }
    }
    if (-not $detail) { $detail = ($real -join ' ') }

    $detail = ($detail -replace '\s+', ' ').Trim()
    # PowerShell prepends the host name ("python.exe : "); it names the host, not the fault.
    $detail = $detail -replace '^[\w.-]+\.(exe|cmd|bat)\s*:\s*', ''

    if (-not $detail) { $detail = 'unclassified failure; see Task Scheduler history' }
    # One log line stays one log line; the full text is in Task Scheduler history if needed.
    if ($detail.Length -gt 300) { $detail = $detail.Substring(0, 297) + '...' }

    Write-LogLine -Outcome 'ERROR' -Detail $detail
    exit 1
  }
}
