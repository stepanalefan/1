@echo off
setlocal EnableDelayedExpansion
REM Build KTR TEST EXE (run on Windows from this folder)
REM Put app.ico (and optionally lupa.ico) next to the scripts first.
cd /d "%~dp0"

REM Find Python: PyCharm's project environment first, then the py launcher, then python on PATH
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set PY="venv\Scripts\python.exe"
if not defined PY (py -3 --version >nul 2>&1 && set PY=py -3)
if not defined PY (python --version >nul 2>&1 && set PY=python)
if not defined PY (
  echo Python not found. Open the project in PyCharm and run the build command in its Terminal,
  echo or install Python from python.org with "Add python.exe to PATH" ticked.
  pause
  exit /b 1
)
echo Using Python: %PY%
%PY% -m pip install --upgrade pyinstaller pyqt5 pyqtgraph numpy pyvisa pyvisa-py pyserial python-can
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

set ICONARGS=
if exist app.ico set ICONARGS=--icon app.ico --add-data "app.ico;."
if exist lupa.ico set ICONARGS=%ICONARGS% --add-data "lupa.ico;."
for %%F in (FAQ.md README.md ABOUT.md) do if exist %%F set ICONARGS=!ICONARGS! --add-data "%%F;."

%PY% -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name KTR_TEST %ICONARGS% ^
  --hidden-import bench_monitor --hidden-import canable_analyzer ^
  --hidden-import can_commands --hidden-import telemetry ^
  --hidden-import dashboard --hidden-import log_viewer --hidden-import tfg_generator --hidden-import help_tab --hidden-import n6700_supply --hidden-import iq_demod --hidden-import bos_station --hidden-import bos_analysis --hidden-import bos_protocol --hidden-import bos_decode --hidden-import bos_shr --hidden-import bos_iqfile --hidden-import i18n --hidden-import bos_ru --hidden-import bos_plan --hidden-import it6800_supply ^
  --hidden-import pyvisa_py --hidden-import serial.tools.list_ports ^
  --collect-submodules pyvisa --collect-submodules pyvisa_py --hidden-import pyvisa.ctwrapper --collect-submodules can ^
  --runtime-hook rthook_visa.py --exclude-module tkinter --exclude-module matplotlib ^
  fsw_vsa_monitor.py

echo.
echo Done: dist\KTR_TEST.exe
echo bos_station.json, can_commands.json, dashboard.json, bos_reports\ and CSV logs live next to the EXE.
pause
