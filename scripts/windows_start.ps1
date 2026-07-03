# Run from PowerShell as Administrator after editing the paths.

$ProjectPath = "C:\path\to\genrdp-proxy-test-bot"
$PythonExe   = "$ProjectPath\venv\Scripts\python.exe"
$BotFile     = "$ProjectPath\testbot.py"

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $BotFile `
    -WorkingDirectory $ProjectPath

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "GenRDP Proxy Test Bot" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -User "SYSTEM" `
    -Force
