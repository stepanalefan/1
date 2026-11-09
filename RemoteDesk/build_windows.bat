@echo off
rem Builds dist\RemoteDesk\RemoteDesk.exe (client) and dist\RemoteDeskServer\RemoteDeskServer.exe
rem (server), signs them if a certificate is configured (see sign_windows.ps1), and
rem zips each folder for distribution.
rem
rem Folder builds (--onedir) instead of single .exe files: a single-file PyInstaller
rem .exe unpacks unsigned DLLs to a temp folder at start, which Smart App Control
rem blocks even when the .exe itself is signed. In a folder build every file can be signed.
setlocal
cd /d "%~dp0"
if exist sign_config.bat call sign_config.bat

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if not defined PY where py >nul 2>nul && set PY=py -3
if not defined PY set PY=python

%PY% -m pip install -r requirements.txt pyinstaller || goto :fail

if not exist "assets\RemoteDesk.ico" %PY% make_icons.py || goto :fail

%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name RemoteDesk ^
  --icon assets\RemoteDesk.ico --add-data "assets;assets" ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --collect-data soundcard --collect-submodules av ^
  RemoteDesk.py || goto :fail

%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name RemoteDeskServer ^
  --icon assets\RemoteDeskServer.ico --add-data "assets;assets" ^
  RemoteDeskServer.py || goto :fail

powershell -NoProfile -ExecutionPolicy Bypass -File sign_windows.ps1 dist\RemoteDesk dist\RemoteDeskServer || goto :fail

powershell -NoProfile -Command "Compress-Archive -Force dist\RemoteDesk dist\RemoteDesk-windows.zip; Compress-Archive -Force dist\RemoteDeskServer dist\RemoteDeskServer-windows.zip" || goto :fail

echo.
echo Done:  dist\RemoteDesk\RemoteDesk.exe   and   dist\RemoteDeskServer\RemoteDeskServer.exe
echo        (zipped: dist\RemoteDesk-windows.zip, dist\RemoteDeskServer-windows.zip)
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
