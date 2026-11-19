<#
.SYNOPSIS
  Registers (or removes) the scheduled task that runs devsync on this machine.

.DESCRIPTION
  Installs a per-user scheduled task calling scripts/devsync-sync-run.ps1 at logon and every
  N hours. Idempotent: re-running replaces the existing task rather than stacking a second.

  ⚠️ WHY 4 HOURS AND NOT 15 MINUTES. The value here is convergence within a working day,
  not within a minute. A tight loop mostly produces lock contention and, since the heartbeat
  stamp landed, at most one extra commit per machine per day anyway (the 24h stamp gate).
  Syncing more often would add noise without changing the outcome.

  ⚠️ RUNS ONLY WHEN LOGGED ON, deliberately. `git push` needs the user's credential store;
  an S4U task ("run whether user is logged on or not") does not reliably have it, and would
  fail in a way that looks like a devsync fault. On a personal PC "logged on" is nearly
  always true, and the dead-man's-switch covers the case where it is not.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install-devsync-task.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\install-devsync-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
  [string]$TaskName = 'devsync-sync',
  [int]$IntervalHours = 4,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# The wrapper is devsync-sync-run.ps1; devsync-sync.ps1 is only a forwarder to it (see
# docs/AUTOMATION.md).
$wrapper = Join-Path $PSScriptRoot 'devsync-sync-run.ps1'
if (-not $Uninstall -and -not (Test-Path $wrapper)) {
  throw "wrapper not found: $wrapper"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Uninstall) {
  if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "removed scheduled task '$TaskName'"
  } else {
    Write-Output "no scheduled task '$TaskName' to remove"
  }
  return
}

if ($existing) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Output "replaced existing task '$TaskName'"
}

# Two launch paths, chosen by whether run-hidden.vbs is on this machine's disk.
#
# WITH the .vbs: launched through run-hidden.vbs, never a bare powershell.exe.
# An Interactive task runs on the user's desktop, so powershell.exe shows a console for the
# ~45s a sync takes, every 4 hours (-WindowStyle Hidden still flashes it). The .vbs waits and
# passes the exit code through, so Last Run Result still reads 0 / 1 / 2.
#
# WITHOUT it: powershell.exe -WindowStyle Hidden, accepting the brief flash. Some antivirus
# products treat a hidden-window .vbs launcher as a threat and delete it together with the
# tasks and scripts it touches. A machine with such a product keeps the .vbs off disk with
# sparse-checkout (`!/scripts/run-hidden.vbs`), and this branch is what runs there.
$hider = Join-Path $PSScriptRoot 'run-hidden.vbs'
$psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$wrapper`""
if (Test-Path $hider) {
  $action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\wscript.exe" `
    -Argument "`"$hider`" powershell.exe $psArgs" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
  $launcher = 'run-hidden.vbs'
} else {
  $action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-WindowStyle Hidden $psArgs" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
  $launcher = 'powershell -WindowStyle Hidden (no run-hidden.vbs on this machine)'
}

# Two triggers: catch the machine coming back after being off, then keep it converging
# through the day.
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
  -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# StartWhenAvailable: a laptop asleep at the scheduled minute runs on wake instead of
# skipping. MultipleInstances IgnoreNew: belt and braces on top of devsync's own lock.

$principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger @($atLogon, $repeating) `
  -Settings $settings `
  -Principal $principal `
  -Description "devsync: converge gitignored files + Claude config across machines. Wrapper: $wrapper" | Out-Null

Write-Output "registered '$TaskName' - at logon, and every $IntervalHours hours - launcher: $launcher"
Get-ScheduledTask -TaskName $TaskName |
  Select-Object TaskName, State |
  Format-Table -AutoSize
