' run-hidden.vbs - run a command with no visible window, wait for it, and return its exit code.
' Used by the devsync-sync scheduled task: the task runs "only when logged on" (git push needs
' the user's credential store), so a bare powershell.exe action pops a console on the desktop
' every 4 hours. wscript.exe has no window, window style 0 hides the child's console, and
' waiting keeps Task Scheduler's "Last Run Result" = the wrapper's own exit code (0/1/2).
Set oShell = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  cmd = cmd & """" & WScript.Arguments(i) & """ "
Next
WScript.Quit oShell.Run(cmd, 0, True)
