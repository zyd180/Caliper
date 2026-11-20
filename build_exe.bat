@echo off
setlocal
cd /d "%~dp0"
set "PY=D:\Tools\Python\python.exe"

if not exist "%PY%" goto nopy

echo [1/4] Clean old build...
"%PY%" -c "import shutil,os; [shutil.rmtree(p,ignore_errors=True) for p in ('build','dist/Caliper')]; os.makedirs('dist',exist_ok=True); print('cleaned')"
if not "%errorlevel%"=="0" goto fail

echo [2/4] PyInstaller onedir build...
"%PY%" -m PyInstaller Caliper.spec --noconfirm --log-level=WARN
if not "%errorlevel%"=="0" goto fail

echo [3/4] Move data dirs to exe root + seed out/channels.json (runtime dependency)...
"%PY%" -c "import shutil,os; base='dist/Caliper'; internal=os.path.join(base,'_internal'); [shutil.move(os.path.join(internal,d),os.path.join(base,d)) for d in ('rules','DBC','description') if os.path.isdir(os.path.join(internal,d))]; os.makedirs(os.path.join(base,'out'),exist_ok=True); shutil.copyfile('out/channels.json',os.path.join(base,'out','channels.json')); print('data dirs moved + channels.json seeded')"
if not "%errorlevel%"=="0" goto fail

echo [4/4] Verify exe exists...
if not exist "dist\Caliper\Caliper.exe" goto fail

echo BUILD OK: %~dp0dist\Caliper\Caliper.exe
exit /b 0

:nopy
echo ERROR: Python not found at D:\Tools\Python\python.exe
pause
exit /b 1

:fail
echo ERROR: build failed. See output above.
pause
exit /b 1
