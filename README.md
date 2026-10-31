# FSW Monitor (RS-485 / FRM edition)

One program to control and monitor the test bench: spectrum analyzer, signal generators,
HMP4040 power supply, RS-485 communication with the FRM (MOXA UPort 1450I) and telemetry —
with test sequences and daily CSV logs.

```
pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial
python fsw_vsa_monitor.py            # start
python fsw_vsa_monitor.py --demo     # try it without hardware
```
USB instruments need **Keysight IO Libraries Suite**, the MOXA UPort 1450I the **MOXA UPort driver**
(each port set to RS-485 in the driver).
Questions and troubleshooting: see **[FAQ.md](FAQ.md)**.

---

## Windows (tabs)

| Tab | What it does |
|---|---|
| **Main** | Overview: device status (green = connected), HMP4040 outputs, **test sequence** editor/runner, FSW constellation with MER / power / frequency error, HMP4040 output plot, telemetry watch list. |
| **Spectrum Analyzer** | R&S FSW. Sub-tabs **VSA (K70)** (demodulation: modulation, symbol rate, filters, constellation, results, trend) and **Spectrum**. Run continuously or single sweeps; only changed settings are sent. |
| **SMW200A** | R&S SMW200A vector generator: frequency, level, RF on/off, digital modulation (format, symbol rate, filter, data source). *Copy from VSA* takes the FSW settings. |
| **TFG6806** | Suin TFG6806, both channels: waveform, mode (continuous / modulation / sweep / burst / double pulse), frequency, amplitude, offset, phase, output settings, all modulation types. LAN or USB. Trigger, align phases, save/recall, SCPI console. |
| **N5183B** | Keysight N5183B MXG: frequency, power, ALC, offsets, AM / FM / ΦM, pulse modulation, step sweep, LF out, RF and MOD switches. LAN / USB / GPIB. Single sweep, trigger, preset, save/recall, SCPI console. |
| **HMP4040** | 4 channels (V, I limit, on/off), master output, power cycling, plot of current / voltage / power, daily log. USB (COM / USB-TMC) or LAN. |
| **RS-485** | MOXA UPort 1450I, up to 4 ports at once (port 1 = **FRM**): COM port, baud rate, data bits, parity, stop bits per port. Trace of every frame (hex + ASCII, RX / TX), port filter, transmit once or periodically, command library, record to a log file. |
| **Telemetry** | FRM telemetry. Until the FRM layout is entered: the frames received on the FRM port (raw). |
| **Logs** | Load the program's CSV logs (HMP, telemetry …) and overlay them on one time plot; filter, steps / points, cursor readout. |

The log window at the bottom shows every action and error of all tabs.

---

## Test sequence steps (Main tab)

| Button | Step |
|---|---|
| **+ HMP** | Set HMP4040 channels (V, I, on/off) and/or master output |
| **+ SMW** | Set SMW200A parameters / RF on-off |
| **+ RS-485** | Send a command from the RS-485 library (on its port) |
| **+ Pause** | Wait N ms |
| **+ FSW** | FSW run / stop / single measurement (waits for the result) / auto level |
| **+ FSW set** | Set FSW VSA or Spectrum parameters (frequency, symbol rate, modulation …) |
| **+ Repeat** | Repeat the following steps N times (0 = forever) up to *End repeat* |
| **+ Log** | Write a message to the log |
| **+ TFG** | TFG6806 CH1 / CH2 / both: any ticked parameters, then trigger / align / recall |
| **+ MXG** | N5183B: any ticked parameters; recall / preset first, single sweep / trigger after |

▲ ▼ move, *Copy*, *Edit* (or double-click), *Delete*; *Run sequence*, *Loop*. A step that fails
stops the sequence with the reason in the log.

---

## Command line

```
python fsw_vsa_monitor.py [--ip ADDR] [--protocol hislip|vxi11|socket] [--connect] [--demo]
```
`--ip` FSW IP / hostname or full VISA string · `--connect` connect at start · `--demo` simulate everything.

The RS-485 tab can also run alone: `python rs485_monitor.py [--demo]`;
the HMP4040 tab: `python bench_monitor.py [--demo]`.

---

## RS-485 / FRM protocol

The FRM command layout is **not entered yet** — commands in the library are raw hex bytes.
When the layout is available it goes into `rs485_monitor.py` (commands) and `telemetry.py`
(`FRM_ARRAYS`, `frm_parse()`, `TelemetryTab.LAYOUTS`).

---

## Files

| File | Part |
|---|---|
| `fsw_vsa_monitor.py` | **Start file**: main window, Spectrum Analyzer |
| `dashboard.py` | Main tab, sequences |
| `bench_monitor.py` | HMP4040 tab, VISA loader |
| `rs485_monitor.py` | RS-485 tab (MOXA UPort 1450I), command library |
| `smw_generator.py`, `tfg_generator.py`, `mxg_generator.py` | Generator tabs |
| `telemetry.py`, `log_viewer.py` | Telemetry and Logs tabs |
| `help_tab.py` | Help tab (FAQ / Readme / About) |
| `build_exe.bat`, `rthook_visa.py` | Build `dist\FSW_Monitor.exe` (the hook opens VISA before Qt in the EXE) |
| `FAQ.md` | Installation, troubleshooting, wiring |

Settings (`dashboard.json`, `rs485_ports.json`, `rs485_commands.json`) and daily logs
(`hmp_log_YYYY-MM-DD.csv`, `telemetry_log_YYYY-MM-DD.csv`, `rs485_log_*.log`) are kept next to the program.
