# FSW Monitor

One program to control and monitor the test bench: spectrum analyzer, signal generators,
power supply, 176-channel voltage scanner, temperature sensors, switch, three CAN lines and
telemetry — with test sequences and daily CSV logs.

```
pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial python-can
python fsw_vsa_monitor.py            # start
python fsw_vsa_monitor.py --demo     # try it without hardware
```
USB instruments need **Keysight IO Libraries Suite**, the IXXAT adapter the **IXXAT VCI 4** driver.
Questions and troubleshooting: see **[FAQ.md](FAQ.md)**.

---

## Windows (tabs)

| Tab | What it does |
|---|---|
| **Main** | Overview: device status (green = connected), HMP4040 outputs, switch + BOS watchdog, 1-Wire temperatures, **test sequence** editor/runner, FSW constellation with MER / power / frequency error, voltage plot, telemetry watch list. |
| **Spectrum Analyzer** | R&S FSW. Sub-tabs **VSA (K70)** (demodulation: modulation, symbol rate, filters, constellation, results, trend) and **Spectrum**. Run continuously or single sweeps; only changed settings are sent. |
| **SMW200A** | R&S SMW200A vector generator: frequency, level, RF on/off, digital modulation (format, symbol rate, filter, data source). *Copy from VSA* takes the FSW settings. |
| **TFG6806** | Suin TFG6806, both channels: waveform, mode (continuous / modulation / sweep / burst / double pulse), frequency, amplitude, offset, phase, output settings, all modulation types. LAN or USB. Trigger, align phases, save/recall, SCPI console. |
| **N5183B** | Keysight N5183B MXG: frequency, power, ALC, offsets, AM / FM / ΦM, pulse modulation, step sweep, LF out, RF and MOD switches. LAN / USB / GPIB. Single sweep, trigger, preset, save/recall, SCPI console. |
| **Bench: 176 ch + HMP4040** | Voltage scanner (Arduino + multiplexers + 34405A): connect Arduino and multimeter separately, scan a range or one channel, plot + table with min/max, daily log. HMP4040: 4 channels (V, I limit, on/off), master output, power cycling, daily log. *Auto-detect devices*, *Test slave link*. |
| **1-Wire Temp** | DS18B20 sensors on the slave Arduino (A3 / A4 / A5): temperature, status, bus, unique ID with CRC, scratchpad, resolution, power mode, min/max. Poll every N s or *Poll now*; double-click a name to rename; daily log. |
| **CAN** | Three CAN lines at once (default BOS = CANable, AFAR = IXXAT CAN1, UPNG = IXXAT CAN2). **Trace** (every frame) and **Monitor** (one row per ID, changed bytes highlighted), ID filter + per-line show/hide, transmit (single / periodic), command library with hotkeys, block scripts, record / save / open candump logs. |
| **Telemetry** | Decoded arrays of BOS, UPNG and AFAR (IOK OSN, SOST, DOP, CONFIG, FAILURE …) with meaning of every field; raw view. |
| **Logs** | Load any CSV logs of the program (voltage, HMP, temperature, switch, telemetry …) and overlay them on one time plot; filter, steps / points, cursor readout. |

The log window at the bottom shows every action and error of all tabs.

---

## Test sequence steps (Main tab)

| Button | Step |
|---|---|
| **+ HMP** | Set HMP4040 channels (V, I, on/off) and/or master output |
| **+ SMW** | Set SMW200A parameters / RF on-off |
| **+ CAN** | Send a command from the CAN library (on its line) |
| **+ Pause** | Wait N ms |
| **+ FSW** | FSW run / stop / single measurement (waits for the result) / auto level |
| **+ FSW set** | Set FSW VSA or Spectrum parameters (frequency, symbol rate, modulation …) |
| **+ Scan** | Start / stop the voltage scan |
| **+ Repeat** | Repeat the following steps N times (0 = forever) up to *End repeat* |
| **+ Log** | Write a message to the log |
| **+ Switch** | Slave switch A0 / A1: ON, OFF or pulse |
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

The CAN sniffer can also run alone: `python canable_analyzer.py [--demo] [--interface slcan|gs_usb|ixxat|socketcan|demo] [--port COM7] [--bitrate 500000] [--connect] [--log file.log]`.

---

## Arduino commands (115200 Bd, one line each, ending with LF)

**Master** (`arduino/sketch_sep24a`) — multiplexer, relays the rest to the slave:

| Command | Reply | |
|---|---|---|
| `SET:<0…175>` | `READY:<n>` | switch the multiplexer to channel n (`ERR:INVALID_CHANNEL`) |
| `OW:…`, `SW:…` | slave's reply | forwarded to the slave (`ERR:SLAVE_TIMEOUT` if it does not answer) |

**Slave** (`arduino/slave_onewire_switch`) — reached through the master, or directly on its own USB:

| Command | Reply | |
|---|---|---|
| `OW:CONV` | `OW:CONV:OK:<bus mask>` / `OW:CONV:NODEV:<line levels>` | start temperature conversion on A3/A4/A5 |
| `OW:READ` | `OW:DEV:<bus>:<ROM>:<scratchpad>:<power>` …, `OW:END:<n>` | read all sensors |
| `SW:SET:<i>:<0\|1>` | `SW:STATE:<bits>` | switch output i (0 = A0, 1 = A1) off / on |
| `SW:PULSE:<i>:<ms>` | `SW:STATE:<bits>` | output on for ms, then off (timed by the slave) |
| `SW:STATE?` | `SW:STATE:<bits>` | e.g. `SW:STATE:10` = A0 on, A1 off |

`arduino/onewire_check` is a standalone 1-Wire test sketch (prints presence and ROM codes).

---

## CAN ID filter syntax

`100-1FF` range · `7E8` single ID · `!7DF` exclude · `18FEF100` 29-bit ID · `700/700` id/mask —
comma-separated, hex.

---

## Files

| File | Part |
|---|---|
| `fsw_vsa_monitor.py` | **Start file**: main window, Spectrum Analyzer |
| `dashboard.py` | Main tab, sequences |
| `bench_monitor.py` | Bench tab, VISA loader |
| `onewire_temp.py`, `slave_switch.py` | 1-Wire tab, switch + watchdog |
| `smw_generator.py`, `tfg_generator.py`, `mxg_generator.py` | Generator tabs |
| `canable_analyzer.py`, `can_commands.py` | CAN tab |
| `telemetry.py`, `log_viewer.py` | Telemetry and Logs tabs |
| `build_exe.bat`, `rthook_visa.py` | Build `dist\FSW_Monitor.exe` (the hook opens VISA before Qt in the EXE) |
| `arduino/` | Master, slave and 1-Wire test sketches |
| `FAQ.md` | Installation, troubleshooting, wiring |

Settings (`channels.txt`, `dashboard.json`, `can_commands.json`, `can_lines.json`, `switch.json`,
`onewire_names.txt`) and daily logs (`*_log_YYYY-MM-DD.csv`) are kept next to the program.
