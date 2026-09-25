[CmdletBinding()]
param(
    [string]$ServerPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'sap_rfc_mcp.py'),
    [string]$PythonPath = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$ServerPath = (Resolve-Path -LiteralPath $ServerPath).Path
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not (Test-Path -LiteralPath $ServerPath -PathType Leaf)) { throw 'ServerPath must be a file.' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'PythonPath must be a file.' }
$existingMutex = $null
if ([System.Threading.Mutex]::TryOpenExisting('Local\SAP_RFC_MCP_Tray', [ref]$existingMutex)) {
    $existingMutex.Dispose()
    throw 'Exit the running SAP RFC MCP tray application before installing.'
}
$destination = Join-Path $env:LOCALAPPDATA 'SAP RFC MCP'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
# A BOM is required for Cyrillic text in Windows PowerShell 5.1.
$utf8 = New-Object System.Text.UTF8Encoding($true)
[IO.File]::WriteAllText((Join-Path $destination 'sap-rfc-tray.ps1'), [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'sap-rfc-tray.ps1')), $utf8)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launch.vbs') -Destination $destination -Force
$config = @{ python = $PythonPath; server = $ServerPath } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $destination 'config.json'), $config, $utf8)
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'SAP RFC MCP.lnk'))
$link.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$link.Arguments = '"' + (Join-Path $destination 'launch.vbs') + '"'
$link.WorkingDirectory = $destination
$link.Description = 'SAP RFC MCP tray launcher'
$link.WindowStyle = 7
$link.Save()
if (-not $NoStart) {
    Start-Process -FilePath $link.TargetPath -ArgumentList $link.Arguments -WindowStyle Hidden
}
Write-Output "Installed: $destination"
Write-Output 'Autostart enabled for the current Windows user.'
