# FSW Monitor — FAQ

Bench program for controlling and monitoring the test setup: R&S FSW spectrum analyzer,
R&S SMW200A, Keysight N5183B and Suin TFG6806 generators, R&S HMP4040 power supply,
RS-485 communication with the FRM through a MOXA UPort 1450I (4 ports) with telemetry
decoding, test sequences and CSV logging.

---

## 1. Installation and start

### Which Python packages are needed?
```
pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial
```
Optional: `pip install pyinstaller` (to build the EXE).

### What else must be installed on the PC?
| For | Install |
|---|---|
| Instruments over **USB** (TFG6806 / N5183B / HMP4040 via USB-TMC) | **Keysight IO Libraries Suite** (or NI-VISA). Restart Windows afterwards. |
| MOXA UPort 1450I (RS-485) | **MOXA UPort 1200/1400/1600 Windows driver** (moxa.com). Then set each port to **RS-485 2W** (see section 5). |

Instruments on **LAN** (FSW, SMW200A, N5183B, TFG6806, HMP4040) need no driver.

### How do I start the program?
Run **`fsw_vsa_monitor.py`** (in PyCharm: right-click → Run). All other `.py` files must be in
the same folder. Always start from this file — it opens VISA before the rest of the program loads
(see *VISA problems* below).

Command-line options:
```
python fsw_vsa_monitor.py --demo                       # everything simulated, no hardware
python fsw_vsa_monitor.py --ip 192.168.0.10 --connect  # FSW address, connect at start
python fsw_vsa_monitor.py --ip 192.168.0.10 --protocol socket
```

### Can I try the program without instruments?
Yes: `--demo`. FSW, generators, HMP4040 and the FRM are simulated (the FRM answers on port 1
"FRM"; port 2 "FRM-B" is a backup line that does not answer).
Demo mode never overwrites your saved RS-485 port settings.

### How do I build an EXE?
Run `build_exe.bat`, or in the PyCharm terminal (one line):
```
python -m PyInstaller --noconfirm --clean --windowed --onefile --name FSW_Monitor --icon app.ico --add-data "app.ico;." --hidden-import bench_monitor --hidden-import rs485_monitor --hidden-import frm_protocol --hidden-import iq_demod --hidden-import telemetry --hidden-import smw_generator --hidden-import dashboard --hidden-import log_viewer --hidden-import tfg_generator --hidden-import mxg_generator --hidden-import help_tab --hidden-import pyvisa_py --hidden-import pyvisa.ctwrapper --hidden-import serial.tools.list_ports --collect-submodules pyvisa --collect-submodules pyvisa_py --runtime-hook rthook_visa.py --exclude-module tkinter --exclude-module matplotlib fsw_vsa_monitor.py
```
Result: `dist\FSW_Monitor.exe`. Without `app.ico`, remove `--icon app.ico --add-data "app.ico;."`.
When adding a new module to the program, add a `--hidden-import <module>` for it.

### The EXE closes immediately / shows nothing.
Build once **without** `--windowed` and start it from a terminal — the error is printed there.

### The EXE starts slowly.
`--onefile` unpacks itself on every start. Use `--onedir` and copy the whole `dist\FSW_Monitor`
folder instead.

---

## 2. Files the program creates

All next to the program / EXE (HMP log: in the log folder chosen on the HMP4040 tab, default the same).

| File | Contents |
|---|---|
| `dashboard.json` | Test sequences, telemetry watch list, Main-tab settings |
| `rs485_ports.json` | RS-485 port setup (name, COM port, baud rate, data bits, parity, stop bits, frame gap) and FRM settings (main / backup line, address, timeout, poll interval) |
| `rs485_commands.json` | RS-485 command library |
| `hmp_log_YYYY-MM-DD.csv` | HMP4040: master, on/off, set and measured V / A / W per channel |
| `telemetry_log_YYYY-MM-DD.csv` | Telemetry watch list values (optionally only changes) |
| `rs485_log_*.log` | RS-485 recording: one line per frame (time, port, RX/TX, hex bytes) |

Every log starts a new file each day. All CSV logs can be plotted together on the **Logs** tab
("Today's logs" loads today's files in one click).

---

## 3. Spectrum Analyzer (FSW)

### How do I connect the FSW?
Enter the IP address and choose HiSLIP (recommended), VXI-11 or raw socket 5025. A full VISA
string (`TCPIP0::…::INSTR`) can be typed instead.

### Continuous or single measurement?
*Run* uses the FSW's continuous mode; *Single* makes one synchronised sweep and waits for it.
Only changed settings are sent to the FSW.

### The FSW has no K70 option — can I still see a constellation?
Yes: sub-tab **I/Q (software demod)**. The FSW's I/Q Analyzer (standard firmware) records raw
I/Q samples; `iq_demod.py` demodulates them in the program. When the FSW reports no K70, the VSA
tab is disabled and this tab is selected automatically.
* Set **center**, **ref level** (or *Auto Level*), then the signal: **modulation** (BPSK, QPSK,
  OQPSK, 8PSK, 16QAM), **symbol rate**, **Tx filter** (RRC / RC / none) and **roll-off α** — they
  must match the transmitter. *Apply* sets the FSW sample rate = symbol rate × *samples per symbol*
  and the record length = *capture length* × samples per symbol.
* Results: MER, EVM, power, carrier frequency error, symbol rate error, I/Q offset (+ trend and the
  spectrum of the capture). They feed the Main tab and the sequence step *FSW single*;
  *FSW set* can change the I/Q settings too.
* Limits: continuous signals only (no burst / pattern search, no equalizer); the constellation may
  be rotated by a multiple of 90° (BPSK 180°) because there is no sync pattern; MER/EVM are not a
  calibrated measurement (implementation floor > 45 dB). A wrong modulation / symbol rate / filter
  gives a smeared constellation and a low MER.
* The absolute power assumes the FSW I/Q data in volts into 50 Ω — compare once with the Spectrum
  tab's channel power or a known SMW200A level.

### "Function not available" / errors about burst or pattern search.
Burst search and pattern search are only sent when they apply to the selected signal/modulation.
If the error still appears, check the K70 settings on the FSW itself.

---

## 4. Generators (SMW200A, N5183B, TFG6806)

### General behaviour
* **Apply** sends only the values that differ from the instrument; output/RF switches are sent last.
* After every change the instrument's error queue is read; an error appears in the log next to
  the setting that caused it (e.g. `Frequency: -222,"Data out of range"`).
* Each tab has a SCPI console for anything without a field.

### N5183B: how do I connect?
LAN (HiSLIP / VXI-11 / socket 5025) with the IP address, or *USB / GPIB / VISA address* → ⟳ lists
instruments (★ = Keysight). After connecting, the status shows the frequency/power range and
installed options. Sections greyed out = option not installed (AM/FM/ΦM need **UNT**, pulse needs
**UNU/UNW**).

### TFG6806: how do I connect?
* **LAN:** IP from the generator's Utility → LAN menu, port **5025**.
* **USB:** rear **USB Device** port (the front port is only for flash drives), Interface = USB,
  press ⟳ and pick the ★ entry. Do **not** type the example address from the manual
  (`…CH372USB…`) — every unit has its own.
* Connecting sets the amplitude unit to **Vpp** on both channels.

### TFG6806: `VI_ERROR_RSRC_NFOUND`
The VISA address does not exist. Press ⟳ and pick the listed device; if nothing is listed, VISA
does not see the generator (cable, power, Keysight IO Libraries — it must appear in Keysight
Connection Expert).

### DC or PRBS waveform on the TFG6806?
Select *Built-in / arb #* and enter the waveform number (the manual does not list the numbers).

---

## 5. HMP4040 and VISA

### Error "PyUSB does not seem to be properly installed … No backend available".
The program used pyvisa's pure-Python fallback instead of the installed VISA. Make sure the
program is started from `fsw_vsa_monitor.py` (it opens VISA first) and read the `VISA:` log line.
Installing pyusb does **not** help for instruments that use Keysight's USB driver.

### `VI_ERROR_LIBRARY_NFOUND` although Keysight IO Libraries are installed.
PyQt5 changes Windows' DLL search so that VISA cannot find Keysight's library through PATH.
The program avoids this by opening VISA at start-up — start it from `fsw_vsa_monitor.py`
(or the EXE built from it). A quick check outside the program:
```python
import pyvisa; rm = pyvisa.ResourceManager(); print(rm); print(rm.list_resources())
```

### How do I connect the HMP4040?
USB: the COM port (virtual COM, 9600 Bd), e.g. `COM7` (*Auto-detect* finds it; the MOXA ports
are skipped). LAN: the IP (`192.168.1.20` or `192.168.1.20:5025`). A VISA string also works.

### Power cycling
HMP4040 tab → *Power cycling*: ON time, OFF time, total duration. It switches the master output.
If the program is closed while cycling runs, it asks first and then switches the master output OFF.

### "CC" on the Main tab
The channel is in current limit (current at the set limit and voltage below the set value).

## 6. RS-485 (MOXA UPort 1450I) and telemetry

### How do I set up the MOXA UPort 1450I?
1. Install the MOXA UPort Windows driver and plug the UPort in: four new COM ports appear
   (Device Manager → *Ports (COM & LPT)* → *MOXA USB Serial Port*).
2. For each port used: Device Manager → MOXA UPort 1450I → port → *Properties / Port
   Settings* (or MOXA's port configuration utility) → **Interface = RS-485 2W** (2-wire, half
   duplex; RS-485 4W for 4-wire wiring). This is a driver setting — the program only opens the COM port.
3. RS-485 tab: tick *Use*, give the port a name (port 1 is **FRM**), choose the COM port
   (⟳ lists MOXA ports first), baud rate, data bits, parity, stop bits → *Connect*.

### Nothing is received / garbage is received.
* Baud rate, parity, stop bits must match the FRM.
* A/B (D+/D−) swapped is the most common wiring fault — swap them. Common GND recommended.
* Termination (120 Ω) at both ends of long lines; see the UPort 1450I manual for its built-in
  termination / pull-up / pull-down settings.
* Interface left at RS-232 in the driver → no RS-485 signals at all.

### Which port settings does the FRM need?
115200 bit/s, 8 data bits, no parity, 1 stop bit (protocol 2, Table 1.1). FRM address **0x79**
(answers come from 0xF9).

### How are bytes grouped into frames?
A complete FRM frame (its length field says how long it is, and the CRC-16/MODBUS is correct) is
shown at once. Anything else is cut by a pause longer than *Frame gap* (default 2 ms; the
protocol ends a frame after 750 µs, which Windows/USB timing cannot resolve).

### FRM control
RS-485 tab → *FRM control*:
* **Main line / Backup**: the ports of the FRM's main and backup RS-485 lines. If the FRM does not
  answer on one line within *Timeout*, the command is repeated main → backup → main → backup
  (protocol 4.4); the line that answered is used for the next commands.
* **Standby / Service / Read 1 / Read 2** quick buttons; *Poll arrays 1 + 2 every …* reads the
  telemetry periodically (*+ 3, 4, 5* also the technological arrays).
* **Command**: any FRM command with its parameters — mode, **Session** (function, PRM / PRD
  frequency codes, modulation, speed, index, PRD data, attenuator), read array 1–5, reset counters,
  DAC OG, DAC encoder 1 / 2, SETTINGS array, power temperature correction, data parking.
  *Send command* sends it and shows the FRM's answer (accepted / cannot be executed / no response).
  *Copy from last array 3 / 5* fills the SETTINGS / power correction data from what was read.
* The trace shows every frame decoded in the **FRM** column (red = error code or CRC error).

### Mode changes are refused ("cannot be executed").
Session ↔ Service is not allowed (protocol Table 5.4): go through Standby. Resetting the
"total time" counters works only in Service mode. A mode change can take up to 2 s.

### Command library (raw)
*Raw transmit / library*: hex bytes with a key, a name and optionally a fixed port; double-click or
*Send* transmits them without waiting for an answer. *+ CRC-16* appends the checksum to typed bytes.
*→ Library* on the FRM control page stores a complete built command.

### Telemetry shows nothing.
The Telemetry tab decodes the answers to "FRM state" (arrays 1–5) from any RS-485 port. Read an
array (*Read 1*) or switch on polling. *All frames* lists every FRM frame with its decoded header.
Temperature, voltage and PLL values are shown as raw codes — the protocol gives no conversion.

---

## 7. Main tab and test sequences

### Which steps can a sequence contain?
HMP outputs, SMW, **FRM** command (any command of the FRM control page; waits for the answer,
an error code or no answer stops the sequence), raw RS-485 command (from the library), Pause, FSW (run / stop / single /
auto level), FSW settings, Repeat … End, Log message, **TFG**
(channel(s), any parameters, trigger / align / recall), **MXG** (any parameters, single sweep /
trigger / recall / preset).

### What happens when a step fails?
The sequence stops and the log says why (device not connected, SCPI error, timeout).
Steps that talk to an instrument wait until it has confirmed.

### Telemetry watch on the Main tab
Pick unit → array → value → *+ Add*. *Log to CSV* writes `telemetry_log_*.csv`;
*only changes* writes a row only when the value changes.

---

## 8. Where do I get help?
Check the log at the bottom of the window first — almost every error is reported there with the
reason. For instrument problems compare with Keysight Connection Expert / the instrument's own
error queue (`SYSTem:ERRor?` in the SCPI console).
