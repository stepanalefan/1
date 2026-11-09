@echo off
rem Builds dist\RemoteDesk.exe (client) and dist\RemoteDeskServer.exe (server).
setlocal
cd /d "%~dp0"

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if not defined PY where py >nul 2>nul && set PY=py -3
if not defined PY set PY=python

%PY% -m pip install -r requirements.txt pyinstaller || goto :fail

if not exist "assets\RemoteDesk.ico" %PY% make_icons.py || goto :fail

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name RemoteDesk ^
  --icon assets\RemoteDesk.ico --add-data "assets;assets" ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --collect-data soundcard --collect-submodules av ^
  RemoteDesk.py || goto :fail

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name RemoteDeskServer ^
  --icon assets\RemoteDeskServer.ico --add-data "assets;assets" ^
  RemoteDeskServer.py || goto :fail

echo.
echo Done:  dist\RemoteDesk.exe   and   dist\RemoteDeskServer.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
