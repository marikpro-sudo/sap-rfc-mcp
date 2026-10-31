$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\SAP_RFC_MCP_Tray', [ref]$created)
if (-not $created) { $mutex.Dispose(); exit }
$script:server = $null
$script:previousStatus = ''
$logDir = Join-Path $PSScriptRoot 'logs'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# mode 'task': the server runs as a Windows scheduled task; the tray only shows status and controls the task.
$script:taskMode = ($config.PSObject.Properties.Name -contains 'mode') -and ($config.mode -eq 'task')
if ($script:taskMode) {
    $script:taskName = [string]$config.taskName
    $script:port = [int]$config.port
    if ($config.PSObject.Properties.Name -contains 'logDir' -and $config.logDir) { $logDir = [string]$config.logDir }
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
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
function Get-PortOwner {
    $listen = Get-NetTCPConnection -LocalPort $script:port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listen) { return [int]$listen.OwningProcess }
    return $null
}
function Stop-TaskServer {
    Stop-ScheduledTask -TaskName $script:taskName -ErrorAction SilentlyContinue
    # Task Scheduler may leave the python child running; stop whatever still holds the port.
    for ($i = 0; $i -lt 10; $i++) {
        $owner = Get-PortOwner
        if (-not $owner) { return }
        if ($i -eq 3) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    }
}
function Invoke-TaskAction([string]$action) {
    try {
        if ($action -in @('stop', 'restart')) { Stop-TaskServer }
        if ($action -in @('start', 'restart')) { Start-ScheduledTask -TaskName $script:taskName -ErrorAction Stop }
        Write-LauncherLog "Task action: $action"
    } catch {
        Write-LauncherLog "Task action $action failed: $($_.Exception.Message)"
        $icon.ShowBalloonTip(5000, 'SAP RFC MCP', 'Не удалось управлять задачей. Возможно, нужны права администратора.', [System.Windows.Forms.ToolTipIcon]::Error)
    }
}
function Get-TaskStatus {
    $task = Get-ScheduledTask -TaskName $script:taskName -ErrorAction SilentlyContinue
    if (-not $task) { return @{ ok = $false; label = "Задача '$($script:taskName)' не найдена" } }
    $owner = Get-PortOwner
    if ($owner) { return @{ ok = $true; label = "Сервер работает (порт $($script:port))"; serverPid = $owner } }
    if ($task.State -eq 'Running') { return @{ ok = $false; label = "Задача запущена, порт $($script:port) не отвечает" } }
    return @{ ok = $false; label = 'Сервер остановлен' }
}
function Update-Status {
    if ($script:taskMode) {
        $st = Get-TaskStatus
        $status.Text = $st.label
        $icon.Text = ("SAP RFC MCP: " + $st.label).Substring(0, [Math]::Min(63, ("SAP RFC MCP: " + $st.label).Length))
        $icon.Icon = if ($st.ok) { [System.Drawing.SystemIcons]::Information } else { [System.Drawing.SystemIcons]::Warning }
        if ($st.label -ne $script:previousStatus) {
            @{ trayPid = $PID; serverPid = $st.serverPid; status = $st.label; mode = 'task'; updated = (Get-Date -Format o) } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'status.json') -Encoding UTF8
            if (-not $st.ok -and $script:previousStatus -like 'Сервер работает*') { $icon.ShowBalloonTip(5000, 'SAP RFC MCP', 'Сервер перестал отвечать. Проверьте журнал ошибок.', [System.Windows.Forms.ToolTipIcon]::Warning) }
            $script:previousStatus = $st.label
        }
        return
    }
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
if ($script:taskMode) {
    $quit.Text = 'Скрыть значок (сервер продолжит работу)'
    $startItem = New-Object System.Windows.Forms.ToolStripMenuItem 'Запустить сервер'
    $stopItem = New-Object System.Windows.Forms.ToolStripMenuItem 'Остановить сервер'
    $menu.Items.Insert($menu.Items.IndexOf($restart), $startItem)
    $menu.Items.Insert($menu.Items.IndexOf($restart) + 1, $stopItem)
    $startItem.Add_Click({ Invoke-TaskAction 'start'; Update-Status })
    $stopItem.Add_Click({ Invoke-TaskAction 'stop'; Update-Status })
    $restart.Add_Click({ Invoke-TaskAction 'restart'; Update-Status })
} else {
    $restart.Add_Click({ Start-Server; Update-Status })
}
$quit.Add_Click({ $context.ExitThread() })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 3000
$timer.Add_Tick({ Update-Status })
try {
    if (-not $script:taskMode) { Start-Server }
    Update-Status
    $timer.Start()
    [System.Windows.Forms.Application]::Run($context)
} catch {
    Write-LauncherLog $_.Exception.ToString()
} finally {
    $timer.Stop(); $timer.Dispose()
    if (-not $script:taskMode) { Stop-Server }
    $icon.Visible = $false
    $icon.Dispose(); $menu.Dispose(); $context.Dispose()
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot 'status.json') -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
