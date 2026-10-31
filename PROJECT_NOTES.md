# Project notes — handover

Read this first in a new session, together with `README.md` (windows, commands) and `FAQ.md`
(installation, troubleshooting). It holds what is **not** obvious from the code: the hardware,
the decisions and why, and what was learned the hard way.

Origin: repository `stepanalefan/1`, branch `claude/fsw50-gui-freeze-errors-7qq36m`
(~55 commits; every commit message explains the change and its reason — `git log` is worth reading).

**This branch (`claude/inspiring-tesla-st1o8m`) is the RS-485 / FRM edition**, derived from it:
removed the Arduino multiplexer scanner, Agilent 34405A multimeter, 1-Wire sensors, slave switch +
BOS watchdog and the CAN tab (CANable / IXXAT) with BOS / AFAR / UPNG telemetry. Added the RS-485
tab (MOXA UPort 1450I, `rs485_monitor.py`); Telemetry now has only **FRM** (layout still to come).
The original branch is unchanged.

---

## 1. What the program is

A PyQt5 desktop program (Windows, run from PyCharm or as a PyInstaller EXE) that controls and
monitors one test bench: spectrum analyzer, three signal generators, a power supply, RS-485
communication with the FRM (MOXA UPort 1450I, 4 ports) with telemetry decoding, test sequences
and daily CSV logs.

The user works in PyCharm on Windows, builds the EXE with `build_exe.bat`, and tests on the real bench.
Everything here was developed in a Linux container without the hardware: features were verified
offscreen in `--demo` mode and against fake instruments (local TCP SCPI servers), and the user then
tested on the bench. Keep that workflow: **every device has a Demo class**.

---

## 2. Architecture

```
fsw_vsa_monitor.py   start file: main window (QTabWidget), FSW driver/worker + VSA & Spectrum views,
                     early VISA opening (see 6), creates all other tabs, closeEvent shuts them down
dashboard.py         Main tab: device status, HMP panel, test sequences, constellation,
                     HMP output plot, telemetry watch (stored in dashboard.json)
bench_monitor.py     HMP4040 tab: HmpWorker (HMP4040, power cycling), output plot, daily CSV,
                     auto-detect; visa_manager() (shared VISA ResourceManager)
rs485_monitor.py     RS-485 tab: up to 4 MOXA ports, one PortWorker thread per port (frames split by
                     a gap timeout), trace, transmit, command library (rs485_commands.json)
smw_generator.py     R&S SMW200A tab        tfg_generator.py   Suin TFG6806 tab (LAN + USB)
mxg_generator.py     Keysight N5183B tab    log_viewer.py      Logs tab (all CSV logs on one plot)
telemetry.py         Field layouts + views, FRM tab (raw frames until the layout is entered),
                     array_listeners (Main-tab watch list)
help_tab.py          Help tab: FAQ / Readme / About from the .md files (own Markdown -> HTML)
rthook_visa.py       PyInstaller runtime hook (EXE only), see 6
```

### Patterns used everywhere (reuse them)
* **One worker thread per instrument** (QThread with a job queue, `submit(fn)`); the GUI never
  blocks on I/O. Results come back through Qt signals.
* **Driver / Demo pair** with the same methods (`get`, `set`, `errors`, `raw`, `close`).
* **Parameter table** (generator modules `P = [(key, label, kind, SCPI, group, cond, choices), …]`)
  drives everything: the tab's fields (`Field` class), read-back, Apply, and the sequence-step
  editor in `dashboard.py`. Adding a parameter = one table row.
* **Apply sends only changes** (`same()` compares numbers within 1e-14 relative; values are
  formatted with 15 digits so text <-> number round trips are exact), in table order, output/RF last.
* **After every SCPI change read `SYSTem:ERRor?`** and report the error next to the setting that
  caused it. This is how commands are validated on the real instrument.
* **Optional imports**: each tab module is imported in `try/except` in `fsw_vsa_monitor.py`; a
  missing module disables its tab, not the program.
* **Files next to the program**: `script_dir()` / `_app_dir()` use `sys.executable` when frozen.
* Sequence steps: `dashboard.py` — `StepDialog` (editor), `step_text` (list text), `defaults`,
  `_exec` (returns a `Token` when the step waits for a worker; `run_in(submit, fn, what)`).
* Main-tab device rows: `Dashboard.DEVICES` + `_tick()` (1 s).

---

## 3. The bench (hardware facts)

| Item | Details |
|---|---|
| R&S FSW-50 | LAN, HiSLIP/VXI-11/socket. VSA (K70) + Spectrum. SCPI verified against R&S's RsFsw driver. |
| R&S SMW200A | LAN. SCPI verified against RsSmw. |
| Keysight N5183B | LAN or USB (VISA). X-Series SCPI; *OPT? gates AM/FM/ΦM (UNT) and pulse (UNU/UNW). Full SCPI PDF could not be downloaded — commands validated through SYSTem:ERRor? on the device. |
| Suin TFG6806 | 2 ch, 65 MHz. LAN raw socket **5025** or USB-TMC (`USB0::1267::260::<serial>::0::INSTR`). SCPI from the CD's Programmer's Guide (12/2024); amplitude unit forced to Vpp on connect. |
| R&S HMP4040 | USB virtual COM 9600 Bd, or LAN socket 5025. pymeasure-style SCPI. |
| MOXA UPort 1450I | USB → 4 × RS-232/422/485, each port its own COM port. RS-485 2W/4W is selected in the MOXA driver per port; the program opens the COM port with pyserial (baud, data bits, parity, stop bits per port, stored in rs485_ports.json). |
| FRM | On RS-485 port 1 (port name "FRM"). **Command and telemetry layout not received yet** — the user will send it. |

---

## 4. Decisions and why

* **FSW continuous mode** (`INIT:CONT ON`) for live view, single synced sweeps (`INIT:IMM;*OPC`)
  for sequences; burst/pattern search only sent when applicable (otherwise "Function not available").
* **Logs**: one CSV per day per kind; long format where columns would change (telemetry).
* **UI**: dark theme; top tab bar spans the window and is pinned left (tabs must not jump);
  Main tab compact (devices in two columns, 5 step buttons per row).
* **RS-485 framing**: no protocol known yet, so a frame = bytes without a pause longer than the
  frame gap (default 5 ms). Replace / extend with real framing (start byte, length, CRC) once the
  FRM layout is known.
* **Language**: UI and docs in English.

---

## 5. Open points / to do

* **FRM command layout** (user will send it): add the commands to the RS-485 library / a proper
  command builder in `rs485_monitor.py`, the framing (start byte, length, checksum), and the
  telemetry in `telemetry.py` (`FRM_ARRAYS`, `frm_parse()`, `TelemetryTab.LAYOUTS` with `Field`s).
  The Main-tab telemetry watch list and the telemetry CSV log then work automatically.
* RS-485 on the real UPort 1450I not tested yet (developed against the demo port).
* TFG6806: reply formats of queries were not documented — verify read-back on the real unit;
  percentages are sent without "%" (switch to "%" if the unit answers -104); DC/PRBS only by number.
* N5183B: confirm the less common commands on the device (AM/FM source names, :POWer:OFFSet,
  :LFOutput:AMPLitude) — errors appear in the log if not accepted.
* `ABOUT.md` organization / author fields are placeholders.

---

## 6. Lessons learned (problems that cost time)

1. **VISA on Windows (most important).**
   * pyvisa's default lookup *silently* falls back to pyvisa-py when the installed VISA cannot be
     loaded; pyvisa-py cannot open USB instruments that use Keysight's driver ("PyUSB … no backend").
     → `bench_monitor.visa_manager()` checks which backend it got and reports why.
   * In the program the IVI router (System32 visa32/visa64.dll) returned **VI_ERROR_LIBRARY_NFOUND**
     although it works in a plain console: Keysight's `ktvisa32.dll` needs the *newer* MSVCP140.dll
     from System32, and once PyQt5 has loaded Qt's bundled *older* MSVCP140.dll it cannot load.
     → open VISA **before** PyQt5 is imported (top of `fsw_vsa_monitor.py`, `EARLY_VISA_RM`).
   * In the EXE, PyInstaller's own PyQt5 runtime hook imports QtCore before the main script, and the
     bootloader sets SetDllDirectory to its unpack folder. → `rthook_visa.py` (passed with
     `--runtime-hook`; custom hooks run first) opens VISA with the normal DLL search before Qt.
   * When it fails, `visa_diag.txt` is written next to the program (loaded DLLs and their paths,
     result of loading each VISA DLL). Use 64-bit-safe ctypes types (HMODULE argtypes).
   * An earlier attempt with `os.add_dll_directory()` failed partly because the returned handle was
     discarded (the directory is removed when the handle is garbage-collected).
2. **Never guess SCPI.** Early OQPSK commands were guessed twice and failed; checking the official
   drivers (RsFsw/RsSmw) or the manual fixed it. When the manual is unavailable, rely on the
   per-command `SYSTem:ERRor?` report.
3. **Qt layouts**: a wide page once made the whole window too wide — use scroll areas and compact
   layouts; QTableWidget `setItem` emits itemChanged (it once wiped a names file — block
   signals while filling); a section read must not overwrite fields the user is editing.
4. **build_exe.bat** must find Python itself (PyCharm's `.venv` is not on PATH when the .bat is
   double-clicked).
5. **Reverting**: a series of VISA changes made in a parallel session broke the program; it was
   reverted with `git revert` (history kept). Test with the user before stacking changes.
6. **Serial latency**: USB-serial adapters have a latency timer (16 ms default on many drivers);
   it delays received bytes and can affect RS-485 frame splitting — lower it if frames are split.

---

## 7. How to work on this code

* Run: `python fsw_vsa_monitor.py --demo` (everything simulated). Offscreen tests:
  `QT_QPA_PLATFORM=offscreen`, drive the GUI with QTimer.singleShot, `w.grab().save(...)` for
  screenshots.
* Fake instruments for driver tests: a small threaded TCP server answering SCPI with an error queue
  (see git history of tfg_generator / mxg_generator commits for examples).
* New device tab: copy the structure of `mxg_generator.py` (driver + demo + worker + Field table +
  tab), add the optional import + tab + shutdown in `fsw_vsa_monitor.py`, a device row and a step
  type in `dashboard.py`, `--hidden-import` in `build_exe.bat`, a line in README/FAQ.
* EXE: `build_exe.bat` (includes `--runtime-hook rthook_visa.py`, the .md files and all modules).
* Commit messages: say what changed and why; the user reads the attached files, so send changed
  files after each change.
