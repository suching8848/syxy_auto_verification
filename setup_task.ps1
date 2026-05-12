$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "CampusNetAutoLogin"

# Check for auto_login.exe first (no Python needed, runs fully hidden via VBS)
$exePath = Join-Path $scriptDir "auto_login.exe"
$vbsPath = Join-Path $scriptDir "run_hidden.vbs"

if (Test-Path $exePath) {
    # Create VBS launcher if not present
    if (-not (Test-Path $vbsPath)) {
        'CreateObject("Wscript.Shell").Run "auto_login.exe", 0, False' | Out-File -FilePath $vbsPath -Encoding ASCII
    }

    $action = New-ScheduledTaskAction `
        -Execute "wscript.exe" `
        -WorkingDirectory $scriptDir `
        -Argument "`"$vbsPath`""

    Write-Host "Using: auto_login.exe (no console, fully hidden)" -ForegroundColor Green
}
else {
    # Fallback: find pythonw.exe (no console window) or python.exe
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

    Write-Host "Using Python: $pythonPath" -ForegroundColor Green

    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -WorkingDirectory $scriptDir `
        -Argument "auto_login.py"
}

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed existing task '$taskName'" -ForegroundColor Yellow
}

# Trigger: daily at 17:55
$trigger = New-ScheduledTaskTrigger -Daily -At "17:55"

# Principal: run as current user
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
Write-Host "  Schedule: Daily at 17:55"
Write-Host "  Window:   Fully hidden (no popup)"
Write-Host "  Log file: $scriptDir\logs\"
