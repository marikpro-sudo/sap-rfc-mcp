$ErrorActionPreference = 'Stop'
$existingMutex = $null
if ([System.Threading.Mutex]::TryOpenExisting('Local\SAP_RFC_MCP_Tray', [ref]$existingMutex)) {
    $existingMutex.Dispose()
    throw 'Exit the running SAP RFC MCP tray application before uninstalling.'
}
$destination = Join-Path $env:LOCALAPPDATA 'SAP RFC MCP'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'SAP RFC MCP.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if ($shortcut.Arguments -eq ('"' + (Join-Path $destination 'launch.vbs') + '"')) {
        Remove-Item -LiteralPath $shortcutPath
    } else { throw 'The startup shortcut belongs to another installation; it was not changed.' }
}
foreach ($name in @('sap-rfc-tray.ps1', 'launch.vbs', 'config.json', 'status.json')) {
    $path = Join-Path $destination $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
}
Write-Output "Tray launcher removed. Logs retained in: $destination\logs"
Write-Output 'Server source, Python environment and .env were not changed.'
