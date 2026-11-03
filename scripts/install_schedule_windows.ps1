<#
.SYNOPSIS
  Creates (or removes) the Windows Task Scheduler jobs for the daily VISA & REGULATORY run (Module 3).

.DESCRIPTION
  Same design as Modules 1, 2 and 4 (run it once on the laptop that runs the pipeline):

    MedibankVisaDaily      Every day at 08:30 MELBOURNE time (after Module 1 at 07:00, Module 2
                           at 07:30 and Module 4 at 08:00, so they never fight over the database
                           tunnel), converted to this computer's clock. Allowed to WAKE the computer.
    MedibankVisaCatchUp    At log-on and every 30 minutes. Never wakes the computer.

  Both run ".venv\Scripts\python.exe scripts\daily.py", which decides by itself whether a run
  is due (src/visa_monitor/daily.py), so firing many times is safe.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_schedule_windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_schedule_windows.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wakeTask = "MedibankVisaDaily"
$catchUpTask = "MedibankVisaCatchUp"

foreach ($name in @($wakeTask, $catchUpTask)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed existing task '$name'."
    }
}
if ($Remove) { exit 0 }

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Not found: $python. Create the virtual environment first (see README)." }

# 08:30 in Melbourne, expressed in this computer's local clock time.
$melbourne = [TimeZoneInfo]::FindSystemTimeZoneById("AUS Eastern Standard Time")
$melNow = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $melbourne)
$melRun = [DateTime]::new($melNow.Year, $melNow.Month, $melNow.Day, 8, 30, 0, [DateTimeKind]::Unspecified)
$utcRun = [TimeZoneInfo]::ConvertTimeToUtc($melRun, $melbourne)
$at = [TimeZoneInfo]::ConvertTimeFromUtc($utcRun, [TimeZoneInfo]::Local).ToString("HH:mm")

$action = New-ScheduledTaskAction -Execute $python -Argument "scripts\daily.py" -WorkingDirectory $root
$common = @{
    StartWhenAvailable         = $true
    MultipleInstances          = "IgnoreNew"
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    RunOnlyIfNetworkAvailable  = $true
    ExecutionTimeLimit         = (New-TimeSpan -Hours 2)
}

Register-ScheduledTask -TaskName $wakeTask -Action $action -Settings (New-ScheduledTaskSettingsSet @common -WakeToRun) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $at) `
    -Description "Medibank visa & regulatory monitor: daily run at 08:30 Melbourne time (may wake the computer)." | Out-Null

$me = "$env:USERDOMAIN\$env:USERNAME"
$catchUpTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $me),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650))
)
Register-ScheduledTask -TaskName $catchUpTask -Action $action -Settings (New-ScheduledTaskSettingsSet @common) `
    -Trigger $catchUpTriggers -User $me `
    -Description "Medibank visa & regulatory monitor: catch-up if the computer missed the daily run." | Out-Null

Write-Host ""
Write-Host "Created '$wakeTask': every day at $at on this computer's clock (= 08:30 Melbourne time today)."
Write-Host "Created '$catchUpTask': at log-on and every 30 minutes (runs only when a run is due)."
Write-Host "Check what it would do now:  .venv\Scripts\python.exe scripts\daily.py --dry-run"
Write-Host "Decision log:                $root\logs\visa_daily.log"
