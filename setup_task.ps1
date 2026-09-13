param(
    [switch]$Boot,
    [switch]$Silent,
    [string]$ScheduleTime,
    [string]$SilentAt,
    [int]$RunMinutes = 0
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $scriptDir "auto_login_config.json"
$config = $null
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

if ($Silent) {
    # 场景 B：每天到点静默守护（无窗口无托盘），跑满时长后自己退出。
    $taskName = "CampusNetAutoLogin_Silent"
    if (-not $SilentAt -and $config) { $SilentAt = $config.silent_start_time }
    if (-not $SilentAt) {
        throw "需要指定静默启动时刻：.\setup_task.ps1 -Silent -SilentAt 22:50（或先在设置里填好 silent_start_time）"
    }
    if ($RunMinutes -le 0) {
        if ($config -and $config.silent_run_minutes) { $RunMinutes = [int]$config.silent_run_minutes }
        if ($RunMinutes -le 0) { $RunMinutes = 30 }
    }
    if ($SilentAt -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
        throw "SilentAt must use HH:mm format"
    }
}
elseif ($Boot) {
    $taskName = "CampusNetAutoLogin_Boot"
}
else {
    $taskName = "CampusNetAutoLogin"
    if (-not $ScheduleTime) {
        if ($config) { $ScheduleTime = $config.schedule_time }
        if (-not $ScheduleTime) { $ScheduleTime = "20:30" }
    }
    if ($ScheduleTime -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
        throw "schedule_time must use HH:mm format"
    }
}

# 场景 B 与 GUI 版共用同一个 CampusNet.exe（--windowed，没有控制台可闪）；
# 旧版 auto_login.exe（--console）只在非静默模式下作为兜底。
$guiExePath = Join-Path $scriptDir "CampusNet.exe"
$exePath = Join-Path $scriptDir "auto_login.exe"

if ($Silent) {
    if (-not (Test-Path $guiExePath)) {
        Write-Host "ERROR: CampusNet.exe not found — silent mode needs the windowed build." -ForegroundColor Red
        Write-Host "  Build it with: pyinstaller --clean --noconfirm build/CampusNet.spec" -ForegroundColor Yellow
        exit 1
    }
    # --windowed 的 exe 直接运行即可，无需 Start-Process 包一层
    $action = New-ScheduledTaskAction `
        -Execute $guiExePath `
        -WorkingDirectory $scriptDir `
        -Argument "--silent --run-minutes $RunMinutes"

    Write-Host "Using: CampusNet.exe --silent (no window, no tray icon)" -ForegroundColor Green
}
elseif (Test-Path $exePath) {
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
elseif ($Silent) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $SilentAt
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
elseif ($Silent) {
    # 静默守护自己会跑满时长退出，给一点余量即可，不要留 2 小时
    $limit = New-TimeSpan -Minutes ($RunMinutes + 10)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit $limit `
        -RestartCount 0
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
else {
    $expected = if ($Silent) { $SilentAt } else { $ScheduleTime }
    if ($actual -notmatch ('T' + [regex]::Escape($expected) + ':00')) {
        Write-Host "ERROR: task registered but trigger is '$actual', expected $expected." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Task '$taskName' registered successfully!" -ForegroundColor Green
if ($Boot) {
    Write-Host "  Schedule:   At system startup (continuous monitoring)"
    Write-Host "  Principal:  SYSTEM (Session 0, no user logon required)"
    Write-Host "  Auth modes: portal_post / http only (browser NOT compatible with Session 0)"
}
elseif ($Silent) {
    Write-Host "  Schedule:   Daily at $SilentAt (silent guard)"
    Write-Host "  Runs for:   $RunMinutes minutes, then exits on its own"
    Write-Host "  Window:     None at all - no window, no tray icon, no popup"
    Write-Host "  Detect:     keeps probing and re-authenticates the moment the portal drops traffic"
}
else {
    Write-Host "  Schedule: Daily at $ScheduleTime"
    Write-Host "  Window:   Fully hidden (no popup)"
}
Write-Host "  Log file:  $scriptDir\logs\"
