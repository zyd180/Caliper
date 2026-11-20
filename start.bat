@echo off
setlocal
cd /d "%~dp0"
set "PY=D:\Tools\Python\python.exe"
set "PYW=D:\Tools\Python\pythonw.exe"

if not exist "%PY%" goto nopy
if not exist "%PYW%" goto nopy

"%PY%" "%~dp0start.py" --check
if not "%errorlevel%"=="0" goto fail

if "%~1"=="--check" goto ok

start "" "%PYW%" "%~dp0start.py"
goto ok

:ok
echo Caliper preflight OK. Workspace: %~dp0
exit /b 0

:nopy
echo ERROR: Python not found at D:\Tools\Python\python.exe
echo Please send this window output to the developer.
pause
exit /b 1

:fail
echo ERROR: preflight failed. Please send this window output to the developer.
echo Workspace: %~dp0
pause
exit /b 1
