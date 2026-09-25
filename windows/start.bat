@echo off
if not exist "%LOCALAPPDATA%\SAP RFC MCP\launch.vbs" (
  echo Run windows\install.ps1 first.
  pause
  exit /b 1
)
start "" wscript.exe "%LOCALAPPDATA%\SAP RFC MCP\launch.vbs"
exit /b
