# Forwarder for a scheduled task that still points at this older path. The wrapper itself is
# devsync-sync-run.ps1 (see docs/AUTOMATION.md for why there are two names). Passes the
# arguments through and returns the wrapper's exit code (0 / 1 / 2), which Task Scheduler
# reports as Last Run Result.
& (Join-Path $PSScriptRoot 'devsync-sync-run.ps1') @args
exit $LASTEXITCODE
