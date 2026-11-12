[CmdletBinding()]
param(
    [string]$TaskName = 'SAP RFC MCP',
    [int]$Port = 8000,
    [int]$WaitSeconds = 30
)
# Restarts the SAP RFC MCP server that runs as a scheduled task.
# Stop-ScheduledTask alone stops only cmd.exe; python.exe keeps the port and the OLD code.
# This script stops the process that listens on the port, starts the task and checks that the PID changed.
# Run from an elevated PowerShell (the server process may belong to another Windows user).
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell (Run as administrator).'
}
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Scheduled task '$TaskName' not found."
}

function Get-PortOwner {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return $null
}

$old = Get-PortOwner
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old) {
    Stop-Process -Id $old -Force
    for ($i = 0; $i -lt 20 -and (Get-PortOwner); $i++) { Start-Sleep -Milliseconds 500 }
    if (Get-PortOwner) { throw "Port $Port is still in use by PID $(Get-PortOwner) after stopping PID $old." }
}

Start-ScheduledTask -TaskName $TaskName
$new = $null
for ($i = 0; $i -lt $WaitSeconds -and -not $new; $i++) {
    Start-Sleep -Seconds 1
    $new = Get-PortOwner
}
if (-not $new) {
    throw "Server did not start listening on port $Port within $WaitSeconds s. See logs\error.log next to sap_rfc_mcp.py."
}
$proc = Get-CimInstance Win32_Process -Filter "ProcessId=$new"
Write-Output ("Restarted: PID {0} -> {1}, started {2}" -f $(if ($old) { $old } else { '-' }), $new, $proc.CreationDate)
