param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$taskName = "AI-News-Digest"
$project = $PSScriptRoot
$python = Join-Path $project ".venv\Scripts\python.exe"
$script = Join-Path $project "news_digest.py"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $taskName."
    exit
}

if (-not (Test-Path $python)) {
    throw "Python not found at $python"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $project
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 08:15)
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Description "Send the AI and stock news digest daily." -Force | Out-Null
Write-Host "Scheduled $taskName daily at 08:15 local time."
