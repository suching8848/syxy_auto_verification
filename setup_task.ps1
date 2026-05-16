param([switch]$Boot)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = if ($Boot) { "CampusNetAutoLogin_Boot" } else { "CampusNetAutoLogin" }

# Check for auto_login.exe first (no Python needed, runs hidden via Start-Process)
$exePath = Join-Path $scriptDir "auto_login.exe"

if (Test-Path $exePath) {
    $exeArgs = if ($Boot) { " -ArgumentList '--boot'" } else { "" }
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -WorkingDirectory $scriptDir `
        -Argument "-NoProfile -WindowStyle Hidden -Command Start-Process -FilePath '$exePath' -WindowStyle Hidden$exeArgs"

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
        -Argument "$scriptDir\auto_login.py --background$pyArgs"
}

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed existing task '$taskName'" -ForegroundColor Yellow
}

# Trigger
if ($Boot) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
}
else {
    $trigger = New-ScheduledTaskTrigger -Daily -At "19:45"
}

# Principal
if ($Boot) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType S4U `
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
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Task '$taskName' registered successfully!" -ForegroundColor Green
if ($Boot) {
    Write-Host "  Schedule:   At system startup (continuous monitoring)"
    Write-Host "  Principal:  SYSTEM (Session 0, no user logon required)"
    Write-Host "  Auth modes: portal_post / http only (browser NOT compatible with Session 0)"
}
else {
    Write-Host "  Schedule: Daily at 19:45"
    Write-Host "  Window:   Fully hidden (no popup)"
}
Write-Host "  Log file:  $scriptDir\logs\"
