<#
Registers the Data Center Watch daily job with Windows Task Scheduler.

    powershell -ExecutionPolicy Bypass -File setup_task.ps1
    powershell -ExecutionPolicy Bypass -File setup_task.ps1 -Times 08:00,20:00
    powershell -ExecutionPolicy Bypass -File setup_task.ps1 -Remove

Default is two runs a day, and they are complementary rather than redundant:

  08:00 (GMT+8) = 00:00 UTC. OpenRouter's daily dataset rolls at UTC midnight,
                  so this run picks up the freshly completed UTC day. It is also
                  after the US equity close (20:00 ET the previous day), so the
                  equity layer gets a settled session.
  20:00 (GMT+8) = after TrendForce publishes the DRAM spot close at 18:10 GMT+8.
                  A morning-only schedule would always report yesterday's memory
                  price. Also gives a second intraday GPU availability snapshot.

Same-day rows are deduplicated at read time (last wins) so the charts stay one
point per day, while the raw CSVs keep every snapshot for intraday analysis.

The task runs only while you are logged on, so no password is stored. The
machine being off means that day's OpenRouter snapshot is lost permanently for
price/spend — volume can still be recovered with backfill.py.
#>
param(
    [string[]]$Times = @("08:00", "20:00"),
    [string]$TaskName = "DataCenterWatch-Daily",
    [switch]$Publish,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "task '$TaskName' is not registered - nothing to remove"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed scheduled task '$TaskName'"
    exit 0
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ([string]::IsNullOrEmpty($py)) {
    Write-Error "python not found on PATH. Install Python or edit this script with a full path."
    exit 1
}

# Invoked via `powershell -File`, every argument arrives as a string, so
# `-Times 08:00,20:00` lands as one element "08:00,20:00" rather than two.
# Split on commas so both the -File and the dot-sourced array form work.
$Times = @($Times | ForEach-Object { $_ -split "," } | Where-Object { $_.Trim() -ne "" } | ForEach-Object { $_.Trim() })
if ($Times.Count -eq 0) {
    Write-Error "no valid times given"
    exit 1
}
foreach ($t in $Times) {
    try { [datetime]::Parse($t) | Out-Null }
    catch { Write-Error "cannot parse time '$t' - use HH:mm, e.g. 08:00"; exit 1 }
}

$script = Join-Path $here "run_daily.py"
if (-not (Test-Path $script)) {
    Write-Error "run_daily.py not found next to this script ($here)"
    exit 1
}

$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) {
    Write-Warning ".env not found. The job will run but commentary will fail until you create it (copy .env.example)."
}

Write-Host "python : $py"
Write-Host "script : $script"
Write-Host "times  : $($Times -join ', ') daily"

# --publish is opt-in: without it the dashboard is rebuilt locally only. Turn it
# on once GitHub Pages is wired up, so the daily run also pushes.
$argline = "`"$script`""
if ($Publish) { $argline = "$argline --publish" }
Write-Host "参数   : $argline"
$action = New-ScheduledTaskAction -Execute $py `
    -Argument $argline -WorkingDirectory $here
# one trigger per requested time; Register-ScheduledTask accepts an array
$trigger = @()
foreach ($t in $Times) {
    $trigger += New-ScheduledTaskTrigger -Daily -At $t
}
# StartWhenAvailable catches up if the machine was asleep at the trigger time.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "replaced existing task"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Data Center Watch: collect OpenRouter + Shadeform + TrendForce + equities, analyze, Opus 5 commentary, rebuild the local dashboard." | Out-Null

Write-Host ""
Write-Host "registered '$TaskName'  ($($Times.Count) triggers/day: $($Times -join ', '))"
Write-Host ""
Write-Host "  run it now      : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  check last run  : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  read the log    : Get-Content '$here\state\run.log' -Tail 40"
Write-Host "  open dashboard  : $here\看板.bat"
Write-Host "  remove          : powershell -ExecutionPolicy Bypass -File setup_task.ps1 -Remove"
if (-not $Publish) {
    Write-Host ""
    Write-Host "  注意：未开启自动发布。配好 GitHub Pages 后重跑并加 -Publish 即可。"
}
