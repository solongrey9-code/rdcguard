@echo off
rem ---------------------------------------------------------------------------
rem AgentGuard RDC launcher template.
rem Copy to e.g. %USERPROFILE%\.local\bin\start_desktop_commander.bat and set
rem the paths below for your machine. Requires Node.js on PATH and:
rem   npm install -g @wonderwhy-er/desktop-commander
rem
rem Why the proxy env vars: Node's fetch() ignores HTTP(S)_PROXY unless
rem NODE_USE_ENV_PROXY=1 is set. Without it the remote device exits with
rem "fetch failed" shortly after boot when a proxy is required.
rem ---------------------------------------------------------------------------

set "PROXY=http://127.0.0.1:10808"
set "LOG=%USERPROFILE%\.local\bin\rdc_log.txt"
set "DC=%APPDATA%\npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"

if not exist "%USERPROFILE%\.local\bin" mkdir "%USERPROFILE%\.local\bin"

rem Single-instance guard: exit 0 when a desktop-commander node process is alive.
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*desktop-commander*' }) { exit 0 } else { exit 1 }"
if %ERRORLEVEL%==0 exit /b 0

set HTTP_PROXY=%PROXY%
set HTTPS_PROXY=%PROXY%
set NODE_USE_ENV_PROXY=1
node "%DC%" remote >> "%LOG%" 2>&1
