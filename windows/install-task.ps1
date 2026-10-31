[CmdletBinding()]
param(
    [string]$ServerPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'sap_rfc_mcp.py'),
    [string]$PythonPath = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [string]$TaskName = 'SAP RFC MCP',
    [int]$Port = 0,
    [switch]$NoTray,
    [switch]$NoStart
)
# Runs the server as a scheduled task at system startup, without anyone signing in
# (S4U logon: no stored password). The tray, if installed, only shows status and controls the task.
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell (Run as administrator).'
}

$ServerPath = (Resolve-Path -LiteralPath $ServerPath).Path
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not (Test-Path -LiteralPath $ServerPath -PathType Leaf)) { throw 'ServerPath must be a file.' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'PythonPath must be a file.' }
$serverDir = Split-Path -Parent $ServerPath

if ($Port -le 0) {
    $Port = 8000
    $envFile = Join-Path $serverDir '.env'
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile -Encoding UTF8 | Where-Object { $_ -match '^\s*MCP_PORT\s*=\s*(\d+)' } | Select-Object -First 1
        if ($line -and $line -match '=\s*(\d+)') { $Port = [int]$Matches[1] }
    }
}

$existingMutex = $null
if ([System.Threading.Mutex]::TryOpenExisting('Local\SAP_RFC_MCP_Tray', [ref]$existingMutex)) {
    $existingMutex.Dispose()
    throw 'Exit the running SAP RFC MCP tray application before installing.'
}

$logDir = Join-Path $serverDir 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# cmd.exe wrapper: sets encoding variables and appends output to log files.
$serverLog = Join-Path $logDir 'server.log'
$errorLog = Join-Path $logDir 'error.log'
$cmdLine = '/d /s /c "set PYTHONIOENCODING=utf-8&& set PYTHONUNBUFFERED=1&& "' + $PythonPath + '" -u "' + $ServerPath + '" 1>>"' + $serverLog + '" 2>>"' + $errorLog + '""'

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\cmd.exe') -Argument $cmdLine -WorkingDirectory $serverDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $taskPrincipal -Description 'SAP RFC MCP server (sap_rfc_mcp.py)' | Out-Null
Write-Output "Scheduled task '$TaskName' registered for $user (starts at system startup, no sign-in needed)."

if (-not $NoTray) {
    $destination = Join-Path $env:LOCALAPPDATA 'SAP RFC MCP'
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($true)
    [IO.File]::WriteAllText((Join-Path $destination 'sap-rfc-tray.ps1'), [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'sap-rfc-tray.ps1')), $utf8)
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launch.vbs') -Destination $destination -Force
    $config = [ordered]@{ mode = 'task'; taskName = $TaskName; port = $Port; logDir = $logDir; python = $PythonPath; server = $ServerPath } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $destination 'config.json'), $config, $utf8)
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'SAP RFC MCP.lnk'))
    $link.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $link.Arguments = '"' + (Join-Path $destination 'launch.vbs') + '"'
    $link.WorkingDirectory = $destination
    $link.Description = 'SAP RFC MCP tray (status of the scheduled task)'
    $link.WindowStyle = 7
    $link.Save()
    Write-Output "Tray (status only) installed: $destination"
}

if (-not $NoStart) {
    $listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listen) {
        Write-Warning "Port $Port is already in use by PID $($listen.OwningProcess). Stop that server, then run: Start-ScheduledTask '$TaskName'"
    } else {
        Start-ScheduledTask -TaskName $TaskName
        Write-Output "Task started. Logs: $logDir"
    }
    if (-not $NoTray) { Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList ('"' + (Join-Path (Join-Path $env:LOCALAPPDATA 'SAP RFC MCP') 'launch.vbs') + '"') }
}
