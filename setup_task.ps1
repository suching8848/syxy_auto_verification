$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "CampusNetAutoLogin"

# Find pythonw.exe (preferred, no console window) or python.exe
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
        Write-Host "ERROR: Python not found. Please install Python from https://python.org" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using Python: $pythonPath" -ForegroundColor Green

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed existing task '$taskName'" -ForegroundColor Yellow
}

# Action: run auto_login.py with pythonw (no console)
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -WorkingDirectory $scriptDir `
    -Argument "auto_login.py"

# Trigger: daily at 17:55
$trigger = New-ScheduledTaskTrigger -Daily -At "17:55"

# Principal: run as current user, interactive (needed for browser + keybd_event)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 0

# Register
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Task '$taskName' registered successfully!" -ForegroundColor Green
Write-Host "  Schedule: Daily at 17:55"
Write-Host "  Log file: $scriptDir\auto_login.log"
