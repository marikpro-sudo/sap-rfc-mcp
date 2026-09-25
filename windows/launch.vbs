Option Explicit
Dim shell, files, folder, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & folder & "\sap-rfc-tray.ps1"""
shell.Run command, 0, False
