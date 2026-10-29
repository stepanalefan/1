@echo off
REM Build FSW Monitor EXE (run on Windows from this folder)
REM Put app.ico (and optionally lupa.ico) next to the scripts first.
python -m pip install --upgrade pyinstaller pyqt5 pyqtgraph numpy pyvisa pyvisa-py pyserial python-can
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

set ICONARGS=
if exist app.ico set ICONARGS=--icon app.ico --add-data "app.ico;."
if exist lupa.ico set ICONARGS=%ICONARGS% --add-data "lupa.ico;."

python -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name FSW_Monitor %ICONARGS% ^
  --hidden-import bench_monitor --hidden-import canable_analyzer ^
  --hidden-import can_commands --hidden-import telemetry ^
  --hidden-import smw_generator --hidden-import dashboard --hidden-import log_viewer --hidden-import onewire_temp --hidden-import slave_switch --hidden-import tfg_generator --hidden-import mxg_generator ^
  --hidden-import pyvisa_py --hidden-import serial.tools.list_ports ^
  --collect-submodules pyvisa --collect-submodules pyvisa_py --hidden-import pyvisa.ctwrapper --collect-submodules can ^
  --runtime-hook rthook_visa.py --exclude-module tkinter --exclude-module matplotlib ^
  fsw_vsa_monitor.py

echo.
echo Done: dist\FSW_Monitor.exe
echo channels.txt, can_commands.json, dashboard.json and CSV logs live next to the EXE.
pause
