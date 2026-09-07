$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bot = Join-Path $project ".venv\Scripts\crypto-bot.exe"
if (-not (Test-Path $bot)) {
    throw "Missing $bot. Create the venv and install the project first."
}
$action = New-ScheduledTaskAction -Execute $bot -Argument "paper tick" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At 7:10PM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "CryptoBot Forward Paper" -Action $action -Trigger $trigger -Settings $settings -Description "Persistent forward paper tick for Crypto Bot 7.0" -Force
Write-Host "Installed task: CryptoBot Forward Paper"
Write-Host "Test now with: Start-ScheduledTask -TaskName 'CryptoBot Forward Paper'"
