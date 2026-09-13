param([switch]$Boot, [string]$ScheduleTime)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = if ($Boot) { "CampusNetAutoLogin_Boot" } else { "CampusNetAutoLogin" }
if (-not $Boot) {
    if (-not $ScheduleTime) {
        $configPath = Join-Path $scriptDir "auto_login_config.json"
        if (Test-Path -LiteralPath $configPath) {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $ScheduleTime = $config.schedule_time
        }
        if (-not $ScheduleTime) { $ScheduleTime = "20:30" }
    }
    if ($ScheduleTime -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
        throw "schedule_time must use HH:mm format"
    }
}

# Check for auto_login.exe first (no Python needed, runs hidden via Start-Process)
$exePath = Join-Path $scriptDir "auto_login.exe"

if (Test-Path $exePath) {
    $exeArgs = if ($Boot) { "--boot" } else { "--background" }
    $escapedExePath = $exePath.Replace("'", "''")
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -WorkingDirectory $scriptDir `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"`$process = Start-Process -FilePath '$escapedExePath' -WindowStyle Hidden -ArgumentList '$exeArgs' -Wait -PassThru; exit `$process.ExitCode`""

    Write-Host "Using: auto_login.exe (via PowerShell Start-Process, fully hidden)" -ForegroundColor Green
}
else {
    # Find pythonw.exe (no console window) or python.exe as fallback
    $pythonPath = $null
    foreach ($name in @("pythonw.exe", "python.exe")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) {
            $pythonPath = $found.Source
            break
        }
    }

    if (-not $pythonPath) {
        Write-Host "Python not found in PATH. Checking common install locations..." -ForegroundColor Yellow
        $commonPaths = @(
            "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
            "$env:APPDATA\Python\Python313\pythonw.exe",
            "$env:APPDATA\Python\Python312\pythonw.exe",
            "C:\Python313\pythonw.exe",
            "C:\Python312\pythonw.exe"
        )
        foreach ($p in $commonPaths) {
            if (Test-Path $p) {
                $pythonPath = $p
                break
            }
        }
        if (-not $pythonPath) {
            Write-Host "ERROR: Neither auto_login.exe nor Python found." -ForegroundColor Red
            Write-Host "Put auto_login.exe in this folder, or install Python from https://python.org" -ForegroundColor Yellow
            exit 1
        }
    }

    Write-Host "Using Python: $pythonPath (--background)" -ForegroundColor Green

    $pyArgs = if ($Boot) { " --boot" } else { "" }
    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -WorkingDirectory $scriptDir `
        -Argument "`"$scriptDir\auto_login.py`" --background$pyArgs"
}

# Trigger
if ($Boot) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
}
else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $ScheduleTime
}

# Principal
if ($Boot) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Limited
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
}

# Settings
if ($Boot) {
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit 0 `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
}
else {
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RestartCount 0
}

# Hidden: don't show any window
$settings.Hidden = $true

# Register
# NOTE: Register-ScheduledTask raises a CIM non-terminating error on access
# denial, which $ErrorActionPreference = "Stop" does NOT trap — the script would
# print a fake success banner. Catch it explicitly and verify the result.
$registerError = $null
try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force `
        -ErrorAction Stop | Out-Null
}
catch {
    $registerError = $_.Exception.Message
}

if ($registerError) {
    Write-Host ""
    Write-Host "ERROR: failed to register task '$taskName'." -ForegroundColor Red
    Write-Host "  Reason: $registerError" -ForegroundColor Red
    Write-Host "  Fix:    run this script from an ELEVATED PowerShell (Run as Administrator)." -ForegroundColor Yellow
    exit 1
}

# Verify the task actually landed with the expected trigger
$registered = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $registered) {
    Write-Host "ERROR: task '$taskName' not found after registration." -ForegroundColor Red
    exit 1
}

$actual = ($registered.Triggers | ForEach-Object { $_.StartBoundary }) -join ','
if ($Boot) {
    # AtStartup triggers carry no StartBoundary — assert the trigger kind instead
    if (-not $registered.Triggers[0].CimClass.CimClassName.Contains('Startup')) {
        Write-Host "WARNING: boot task trigger is not AtStartup ($actual)" -ForegroundColor Yellow
    }
}
elseif ($actual -notmatch ('T' + [regex]::Escape($ScheduleTime) + ':00')) {
    Write-Host "ERROR: task registered but trigger is '$actual', expected $ScheduleTime." -ForegroundColor Red
    exit 1
}

Write-Host "Task '$taskName' registered successfully!" -ForegroundColor Green
if ($Boot) {
    Write-Host "  Schedule:   At system startup (continuous monitoring)"
    Write-Host "  Principal:  SYSTEM (Session 0, no user logon required)"
    Write-Host "  Auth modes: portal_post / http only (browser NOT compatible with Session 0)"
}
else {
    Write-Host "  Schedule: Daily at $ScheduleTime"
    Write-Host "  Window:   Fully hidden (no popup)"
}
Write-Host "  Log file:  $scriptDir\logs\"
