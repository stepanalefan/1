# Project notes — handover

Read this first in a new session, together with `README.md` (windows, commands) and `FAQ.md`
(installation, troubleshooting). It holds what is **not** obvious from the code: the hardware,
the decisions and why, and what was learned the hard way.

Origin: repository `stepanalefan/1`, branch `claude/fsw50-gui-freeze-errors-7qq36m`
(~55 commits; every commit message explains the change and its reason — `git log` is worth reading).

---

## 1. What the program is

A PyQt5 desktop program (Windows, run from PyCharm or as a PyInstaller EXE) that controls and
monitors one test bench: spectrum analyzer, three signal generators, a power supply, a 176-channel
voltage scanner, 1-Wire temperature sensors, switch outputs, three CAN lines with telemetry decoding,
test sequences and daily CSV logs. ~13 700 lines of Python.

The user works in PyCharm on Windows, builds the EXE with `build_exe.bat`, and tests on the real bench.
Everything here was developed in a Linux container without the hardware: features were verified
offscreen in `--demo` mode and against fake instruments (local TCP SCPI servers), and the user then
tested on the bench. Keep that workflow: **every device has a Demo class**.

---

## 2. Architecture

```
fsw_vsa_monitor.py   start file: main window (QTabWidget), FSW driver/worker + VSA & Spectrum views,
                     early VISA opening (see 6), creates all other tabs, closeEvent shuts them down
dashboard.py         Main tab: device status, HMP / switch / temperature panels, test sequences,
                     constellation, voltage plot, telemetry watch (stored in dashboard.json)
bench_monitor.py     Bench tab: ScanWorker (ArduinoMux + Multimeter), HmpWorker (HMP4040, power
                     cycling), daily CSVs, auto-detect, "Test slave link"; visa_manager()
onewire_temp.py      1-Wire tab (sensors read through the master Arduino -> slave)
slave_switch.py      SwitchController + panel on Main: outputs A0/A1, BOS watchdog, switch_log
smw_generator.py     R&S SMW200A tab        tfg_generator.py   Suin TFG6806 tab (LAN + USB)
mxg_generator.py     Keysight N5183B tab    log_viewer.py      Logs tab (all CSV logs on one plot)
canable_analyzer.py  CAN tab (3 lines, workers per line) + can_commands.py (library, scripts)
telemetry.py         Reassembler + field layouts of the unit arrays, Telemetry tab, array_listeners
help_tab.py          Help tab: FAQ / Readme / About from the .md files (own Markdown -> HTML)
rthook_visa.py       PyInstaller runtime hook (EXE only), see 6
arduino/             sketch_sep24a (master), slave_onewire_switch (slave), onewire_check (test)
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
| Agilent 34405A | USB-TMC via Keysight IO Libraries (`USB0::0x0957::0x0618::MY53270006::0::INSTR`). |
| Master Arduino | Uno/Nano. Multiplexer: S0–S3 = D2–D5, EN of 11 CD74HC4067 = D6–D13, A0, A1, A2. `SET:n` → `READY:n`, 1 ms settle. Relays `OW:`/`SW:` to the slave over SoftwareSerial **A4 RX / A5 TX, 19200**. |
| Slave Arduino | SoftwareSerial **D2 RX / D3 TX**; 1-Wire buses **A3, A4, A5** (DS18B20, parasitic power, 2.2 kΩ pull-ups, ~10 m cables); switch outputs **A0, A1** (active-high, `SW_ACTIVE_LOW` in sketch); pulses timed on the slave. |
| Voltage channels | 0–143: 0…5 V. 144–159: −5…0 V through an op-amp inverter (program flips sign). 160–175: ~16 V through **100 kΩ / 33 kΩ** divider (× 133/33). Channels named `GND` in channels.txt are skipped. |
| CAN | BOS on **CANable** (slcan), AFAR on **IXXAT CAN1**, UPNG on **IXXAT CAN2** (python-can `ixxat`, channel 0/1). 29-bit IDs `prefix(16) | segment(8) | type(8)`; prefixes BOS 0x1A46, UPNG 0x1A86, AFAR 0x1AC6. Arrays little-endian; segment byte bits 7-6 first/middle/last, bits 3-0 counter mod 16. |

---

## 4. Decisions and why

* **FSW continuous mode** (`INIT:CONT ON`) for live view, single synced sweeps (`INIT:IMM;*OPC`)
  for sequences; burst/pattern search only sent when applicable (otherwise "Function not available").
* **Arduino and multimeter connect separately**: 1-Wire and switch work without the DMM.
* **BOS watchdog**: "BOS data" = CAN frames with prefix 0x1A46 and/or FSW VSA results with
  MER ≥ threshold (10 dB). Default rule *CAN or FSW*, 60 min, pulse 1 s; counting from arming /
  last trigger. Watchdog and switch actions go to switch_log.
* **CAN lines in one monitor**: Frame has `bus` (line name); Monitor keys are (line, ext, id).
  Library commands can be tied to a line; otherwise the Transmit panel's line is used.
* **Logs**: one CSV per day per kind; long format where columns would change (telemetry).
  Voltage log header is `Time,Channel,Voltage` (the old Russian 4-column files still load).
* **UI**: dark theme; top tab bar spans the window and is pinned left (tabs must not jump);
  Main tab compact (devices in two columns, HMP + switch side by side, 5 step buttons per row).
* **Language**: UI and docs in English; Arduino sketch comments in Russian (the original sketch was).

---

## 5. Open points / to do

* Telemetry layouts still missing (user will send tables): **BOS IOK CONFIG (0xFA) and IOK ARCHIVE
  (0xF9)**, **AFAR IOK CONFIG (0xFA) and IOK FV (0xFB)**. Questions left open earlier: AFAR page
  numbers, BOS DOP length (table says 16 B, unit sends 28 → 32), BOS OSN bytes 65–72.
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
3. **1-Wire**: "no presence" on all buses was GND and +5 V swapped at the sensors. Diagnostics added:
   the slave reports each line's idle level (`OW:CONV:NODEV:<levels>`), and *Test slave link* walks
   PC → master → slave → 1-Wire. With parasitic power over 10 m: 2.2 kΩ pull-up, VDD to GND.
4. **Master/slave link**: first failure was wiring (TX must go to RX, common GND).
5. **Multiplexer voltage drop with many units**: inputs outside the CD74HC4067's 0…5 V supply leak
   through its protection diodes even when disabled.
6. **Scan speed**: per-channel timing readout; USB-serial latency timer (16 ms default) matters.
7. **Qt layouts**: the CAN page once made the whole window too wide — use scroll areas and compact
   layouts; QTableWidget `setItem` emits itemChanged (it once wiped onewire_names.txt — block
   signals while filling); a section read must not overwrite fields the user is editing.
8. **build_exe.bat** must find Python itself (PyCharm's `.venv` is not on PATH when the .bat is
   double-clicked).
9. **Reverting**: a series of VISA changes made in a parallel session broke the program; it was
   reverted with `git revert` (history kept). Test with the user before stacking changes.

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
