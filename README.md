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
| **Spectrum Analyzer** | R&S FSW. Sub-tabs **VSA (K70)** (only if FSW-K70 is installed), **Spectrum**, and **I/Q (software demod)**: the FSW I/Q Analyzer captures raw I/Q and the program demodulates it (BPSK, QPSK, OQPSK, 8PSK, 16QAM): constellation, MER, EVM, power, carrier frequency error, symbol rate error, I/Q offset, trend, captured spectrum. Without K70 the I/Q tab is selected automatically. Run continuously or single captures; only changed settings are sent. |
| **SMW200A** | R&S SMW200A vector generator: frequency, level, RF on/off, digital modulation (format, symbol rate, filter, data source). *Copy from VSA* takes the FSW settings. |
| **TFG6806** | Suin TFG6806, both channels: waveform, mode (continuous / modulation / sweep / burst / double pulse), frequency, amplitude, offset, phase, output settings, all modulation types. LAN or USB. Trigger, align phases, save/recall, SCPI console. |
| **N5183B** | Keysight N5183B MXG: frequency, power, ALC, offsets, AM / FM / ΦM, pulse modulation, step sweep, LF out, RF and MOD switches. LAN / USB / GPIB. Single sweep, trigger, preset, save/recall, SCPI console. |
| **HMP4040** | 4 channels (V, I limit, on/off), master output, power cycling, plot of current / voltage / power, daily log. USB (COM / USB-TMC) or LAN. |
| **RS-485** | MOXA UPort 1450I, up to 4 ports at once (FRM main + backup line): COM port, baud rate, data bits, parity, stop bits per port. **FRM control**: all protocol commands (mode, session parameters, read arrays, reset, DAC settings, SETTINGS / power correction, data parking) with answer check, main/backup line fallback, polling. Trace of every frame with decoded FRM header, raw transmit (+CRC), command library, record to a log file. |
| **Telemetry** | FRM state arrays decoded field by field: 1 (state), 2 (receiver), 3 (SETTINGS), 4 (DAC), 5 (power correction); all FRM frames. |
| **Logs** | Load the program's CSV logs (HMP, telemetry …) and overlay them on one time plot; filter, steps / points, cursor readout. |

The log window at the bottom shows every action and error of all tabs.

---

## Test sequence steps (Main tab)

| Button | Step |
|---|---|
| **+ HMP** | Set HMP4040 channels (V, I, on/off) and/or master output |
| **+ SMW** | Set SMW200A parameters / RF on-off |
| **+ FRM** | FRM command (mode, session parameters, read array, reset, DAC …); waits for the answer |
| **+ RS-485** | Send a raw command from the RS-485 library (on its port), no answer check |
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

`frm_protocol.py` implements the protocol (edition 3.0, 27.11.2025): 115200 8N1, FRM address 0x79.

| | Frame |
|---|---|
| Command | `79` · function (2 B, LE) · length · data · CRC-16/MODBUS (LE) |
| Response | `F9` · function · response code (00 accepted, 02 cannot be executed) · state · length · data · CRC |

| Command | Function | Data |
|---|---|---|
| Standby / Service | `01 15` | `01 00` / `03 00` |
| Session | `01 15` | 14 B = Table 5.1 bytes 25…38 |
| Reset counters | `0A 15` | 2 B bit mask (Table 6.2) |
| DAC OG control | `0D 15` | DATA_DAC_OG, 12 bit |
| DAC encoder 1 / 2 setup | `07 15` / `06 15` | 8 B (Table 6.4) / pll_vco |
| SETTINGS array | `09 15` | 232 B (Table 6.6) |
| Power temperature correction | `0F 15` | 47 B (Table 6.7) |
| Data parking | `F7 37` | `E4 00` |
| FRM state, array 1…5 | `0B 15` | `0N 00` → answer 62 / 16 / 234 / 16 / 42 B |

---

## Files

| File | Part |
|---|---|
| `fsw_vsa_monitor.py` | **Start file**: main window, Spectrum Analyzer |
| `iq_demod.py` | Software demodulator for I/Q captures (constellation without K70), signal simulator |
| `dashboard.py` | Main tab, sequences |
| `bench_monitor.py` | HMP4040 tab, VISA loader |
| `rs485_monitor.py` | RS-485 tab (MOXA UPort 1450I), FRM control, command library |
| `frm_protocol.py` | FRM protocol: frames, CRC, commands, decoder, simulator, command editor |
| `smw_generator.py`, `tfg_generator.py`, `mxg_generator.py` | Generator tabs |
| `telemetry.py`, `log_viewer.py` | Telemetry and Logs tabs |
| `help_tab.py` | Help tab (FAQ / Readme / About) |
| `build_exe.bat`, `rthook_visa.py` | Build `dist\FSW_Monitor.exe` (the hook opens VISA before Qt in the EXE) |
| `FAQ.md` | Installation, troubleshooting, wiring |

Settings (`dashboard.json`, `rs485_ports.json`, `rs485_commands.json`) and daily logs
(`hmp_log_YYYY-MM-DD.csv`, `telemetry_log_YYYY-MM-DD.csv`, `rs485_log_*.log`) are kept next to the program.
