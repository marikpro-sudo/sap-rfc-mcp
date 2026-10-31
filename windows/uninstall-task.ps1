[CmdletBinding()]
param(
    [string]$TaskName = 'SAP RFC MCP'
)
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell (Run as administrator).'
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Scheduled task '$TaskName' removed."
} else {
    Write-Output "Scheduled task '$TaskName' not found."
}
Write-Output 'If a python process still listens on the MCP port, stop it manually.'
Write-Output 'To remove the tray as well: exit it from its menu, then run windows\uninstall.ps1.'
