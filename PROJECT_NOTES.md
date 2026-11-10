# Project notes — handover

Read this first in a new session, together with `README.md` (windows, commands) and `FAQ.md`
(installation, troubleshooting). It holds what is **not** obvious from the code: the hardware,
the decisions and why, and what was learned the hard way.

Origin: repository `stepanalefan/1`, branch `claude/fsw50-gui-freeze-errors-7qq36m` (~55 commits; every
commit message explains the change and its reason). This branch (`claude/confident-cerf-u7q1dx`) starts from
it and turns the bench program into the **BOS Test Station**: SMW200A, N5183B, the 176-channel voltage
scanner (Arduino + multiplexers + 34405A), the 1-Wire sensors and the slave-Arduino switch / BOS watchdog
were removed; CAN, telemetry, FSW, HMP4040, N6701C, TFG6806, sequences and logs were kept.

---

## 1. What the program is

Name: **KTR RAD TEST** (EXE `KTR_RAD_TEST.exe`; start file still `fsw_vsa_monitor.py`).

A PyQt5 desktop program (Windows, run from PyCharm or as a PyInstaller EXE) that tests the **BOS modulator**
(BPSK / OQPSK; Microsemi MPF300T FPGA → TI DAC38J82 (JESD204B) → ADI ADMV1013 I/Q upconverter with LO ×4
from TI LMX2594 → ADI ADRF5730 step attenuator → UMS CHA3397 PA). It switches the unit over CAN, captures the
output with an R&S FSW (raw I/Q), reads the BOS CAN telemetry and diagnoses faults down to the block.
Also: HMP4040 / N6701C supplies, TFG6806 generator, three CAN lines, sequences, daily CSV logs.

The user works in PyCharm on Windows, builds the EXE with `build_exe.bat`, and tests on the real bench.
Everything here was developed in a Linux container without the hardware: features were verified
offscreen in `--demo` mode and against fake instruments (local TCP SCPI servers), and the user then
tested on the bench. Keep that workflow: **every device has a Demo class**.

---

## 2. Architecture

```
```
fsw_vsa_monitor.py   start file: main window (QTabWidget), FSW driver/worker + VSA, Spectrum & I/Q views,
                     early VISA opening (see 6), creates all other tabs, closeEvent shuts them down
bos_station.py       BOS Test Station tab: settings, test plans, generator-based runner, unit control (CAN
                     library / operator prompts), FSW capture jobs, views, HTML report, history
bos_analysis.py      pure NumPy: analysis of modulated / CW captures, attenuator fit, diagnosis rules,
                     suspicion scores, corrections, BosSimulator (demo + fault injection)
iq_demod.py          software demodulator used by the I/Q tab and by bos_analysis
dashboard.py         Main tab: device status, supply panels, BOS summary, power watchdog, sequences,
                     constellation, telemetry watch (dashboard.json)
bench_monitor.py     HMP4040 tab (BenchTab: HmpWorker, power cycling, hmp_log) + visa_manager(), open_link()
n6700_supply.py      Keysight N6701C tab      tfg_generator.py   Suin TFG6806 tab (LAN + USB)
canable_analyzer.py  CAN tab (3 lines, workers per line) + can_commands.py (library, scripts)
telemetry.py         Reassembler + field layouts of the unit arrays, Telemetry tab, array_listeners
log_viewer.py        Logs tab        help_tab.py   Help tab (FAQ / Readme / About)
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
| Suin TFG6806 | 2 ch, 65 MHz. LAN raw socket **5025** or USB-TMC (`USB0::1267::260::<serial>::0::INSTR`). SCPI from the CD's Programmer's Guide (12/2024); amplitude unit forced to Vpp on connect. |
| R&S HMP4040 | USB virtual COM 9600 Bd, or LAN socket 5025. pymeasure-style SCPI. |
| Keysight N6701C | LAN socket 5025 / VISA. |
| BOS modulator | MPF300T → JESD204B → DAC38J82 (I/Q) → ADMV1013 (LO ×4 from LMX2594) → ADRF5730 (6 bit, 0.5 dB) → CHA3397. Telemetry IOK DOP (0xFB): pll_lmk, pll_jesd, pll_all, pll_lmx (DAC), pll_lmx (mod), jesd_err, FPGA rails (fix3 mV); IOK OSN (0xFC): temperatures, Stat_10MHz / Sour_10MHz, Err_Ref_LO, Mode, Mode_SNR. |
| CAN | BOS on **CANable** (slcan), AFAR on **IXXAT CAN1**, UPNG on **IXXAT CAN2** (python-can `ixxat`, channel 0/1). 29-bit IDs `prefix(16) | segment(8) | type(8)`; prefixes BOS 0x1A46, UPNG 0x1A86, AFAR 0x1AC6. Arrays little-endian; segment byte bits 7-6 first/middle/last, bits 3-0 counter mod 16. |

---

## 4. Decisions and why

* **FSW continuous mode** (`INIT:CONT ON`) for live view, single synced sweeps (`INIT:IMM;*OPC`)
  for sequences; burst/pattern search only sent when applicable (otherwise "Function not available").
* **BOS station uses the I/Q Analyzer**, not K70: one capture path for every FSW, and the program needs the
  raw symbols anyway (I/Q impairment fit, phase-error series, PAPR, spectrum). Only SCPI already verified in
  fsw_vsa_monitor.py is used (FREQ:CENT, TRAC:IQ:SRAT / RLEN, ADJ:LEV, INIT:IMM;*OPC, TRAC:IQ:DATA:MEM?).
* **Runner = Python generator**: each test is linear code that `yield`s requests (FSW job, sleep, operator
  prompt); the GUI resumes it when the FSW worker's job_done('bos', …) arrives. Capture + analysis run in the
  FSW worker thread, the GUI never blocks.
* **Unit control** by the BOS protocol (default; every command acknowledged, target-designation keep-alive,
  session re-started before captures because of Time_SNR), or by CAN library commands / operator prompts.
* **Diagnosis rules** live in `bos_analysis.diagnose`: each Finding has severity, explanation, suspect blocks
  with weights and actions; `suspicion()` combines them (1 − Π(1 − w·sev)). Add a rule there and a simulated
  fault in `FAULTS` / `BosSimulator` to test it. In demo the telemetry comes from the simulator.
* **CAN lines in one monitor**: Frame has `bus` (line name); Monitor keys are (line, ext, id).
  Library commands can be tied to a line; otherwise the Transmit panel's line is used.
* **Logs**: one CSV per day per kind; long format where columns would change (telemetry). The Logs tab still
  reads old voltage / temperature / switch logs.
* **UI**: dark theme; top tab bar spans the window and is pinned left (tabs must not jump);
  Main tab compact (devices in two columns, HMP + switch side by side, 5 step buttons per row).
* **Language**: UI and docs in English.

---

## 5. Open points / to do

* Telemetry layouts still missing (user will send tables): **BOS IOK CONFIG (0xFA) and IOK ARCHIVE
  (0xF9)**, **AFAR IOK CONFIG (0xFA) and IOK FV (0xFB)**. Questions left open earlier: AFAR page
  numbers, BOS DOP length (table says 16 B, unit sends 28 → 32), BOS OSN bytes 65–72.
* BOS protocol (bos_protocol.py, from ПИЛВ 0102-004). Confirmed by the user: УР = BPSK (symbol rate
  2 × data rate), ШР = OQPSK (always 131.072 Msym/s). Still to confirm on the unit: the allowed sender address
  (default КПА ИВУ 0x18), the FSW I/Q bandwidth for 131 Msym/s (~180 MHz needed at 4 samples/symbol), and the bit order of the 11-bit N field (byte 3 bits 7-5 = low bits assumed, like Freq).
  Confirm the DAC38J82 QMC register scaling
  (gain / phase / offset LSB factors on the Unit / signal page) against the datasheet; set real limits.
* BOS station on the real FSW: verify capture levels (auto level) and the cable loss; validate the diagnosis
  rules on units with known faults and tune weights / thresholds.
* TFG6806: reply formats of queries were not documented — verify read-back on the real unit.
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
3. (removed hardware: 1-Wire, Arduino link, multiplexer — see the origin branch for those lessons)
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
* New device tab: copy the structure of `tfg_generator.py` (driver + demo + worker + Field table +
  tab), add the optional import + tab + shutdown in `fsw_vsa_monitor.py`, a device row and a step
  type in `dashboard.py`, `--hidden-import` in `build_exe.bat`, a line in README/FAQ.
* EXE: `build_exe.bat` (includes `--runtime-hook rthook_visa.py`, the .md files and all modules).
* Commit messages: say what changed and why; the user reads the attached files, so send changed
  files after each change.
