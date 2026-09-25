$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\SAP_RFC_MCP_Tray', [ref]$created)
if (-not $created) { $mutex.Dispose(); exit }
$script:server = $null
$script:previousStatus = ''
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Application
$icon.Text = 'SAP RFC MCP'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$status = $menu.Items.Add('Запуск...')
$status.Enabled = $false
[void]$menu.Items.Add('-')
$logs = $menu.Items.Add('Открыть журнал')
$restart = $menu.Items.Add('Перезапустить сервер')
$quit = $menu.Items.Add('Выход — остановить сервер')
$icon.ContextMenuStrip = $menu
$icon.Visible = $true
$context = New-Object System.Windows.Forms.ApplicationContext
function Write-LauncherLog([string]$message) {
    Add-Content -LiteralPath (Join-Path $logDir 'launcher.log') -Value "$(Get-Date -Format o) $message" -Encoding UTF8
}
function Stop-Server {
    if ($script:server -and -not $script:server.HasExited) {
        $script:server.Kill()
        [void]$script:server.WaitForExit(5000)
    }
    if ($script:server) { $script:server.Dispose(); $script:server = $null }
}
function Start-Server {
    try {
        Stop-Server
        foreach ($name in @('server.log', 'error.log')) {
            $path = Join-Path $logDir $name
            if (Test-Path -LiteralPath $path) { Move-Item -LiteralPath $path -Destination ($path + '.previous') -Force }
        }
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONUNBUFFERED = '1'
        $script:server = Start-Process -FilePath $config.python -ArgumentList ('-u "' + $config.server + '"') -WorkingDirectory (Split-Path -Parent $config.server) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'server.log') -RedirectStandardError (Join-Path $logDir 'error.log') -PassThru
        Write-LauncherLog "Started PID $($script:server.Id)"
    } catch {
        Write-LauncherLog $_.Exception.Message
        $icon.ShowBalloonTip(5000, 'SAP RFC MCP', 'Ошибка запуска. Откройте журнал через меню значка.', [System.Windows.Forms.ToolTipIcon]::Error)
    }
}
function Update-Status {
    $running = $script:server -and -not $script:server.HasExited
    $label = if ($running) { 'Процесс сервера запущен' } else { 'Сервер остановлен' }
    $status.Text = $label
    $icon.Text = "SAP RFC MCP: $label"
    $icon.Icon = if ($running) { [System.Drawing.SystemIcons]::Information } else { [System.Drawing.SystemIcons]::Warning }
    if ($label -ne $script:previousStatus) {
        $serverId = if ($running) { $script:server.Id } else { $null }
        @{ trayPid = $PID; serverPid = $serverId; status = $label; updated = (Get-Date -Format o) } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'status.json') -Encoding UTF8
        if (-not $running -and $script:previousStatus) { $icon.ShowBalloonTip(5000, 'SAP RFC MCP', 'Сервер остановился. Проверьте журнал ошибок.', [System.Windows.Forms.ToolTipIcon]::Warning) }
        $script:previousStatus = $label
    }
}
$logs.Add_Click({ Start-Process explorer.exe -ArgumentList ('"' + $logDir + '"') })
$icon.Add_DoubleClick({ Start-Process explorer.exe -ArgumentList ('"' + $logDir + '"') })
$restart.Add_Click({ Start-Server; Update-Status })
$quit.Add_Click({ $context.ExitThread() })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 3000
$timer.Add_Tick({ Update-Status })
try {
    Start-Server
    Update-Status
    $timer.Start()
    [System.Windows.Forms.Application]::Run($context)
} catch {
    Write-LauncherLog $_.Exception.ToString()
} finally {
    $timer.Stop(); $timer.Dispose()
    Stop-Server
    $icon.Visible = $false
    $icon.Dispose(); $menu.Dispose(); $context.Dispose()
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot 'status.json') -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
