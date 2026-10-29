# FSW Monitor — FAQ

Bench program for controlling and monitoring the test setup: R&S FSW spectrum analyzer,
R&S SMW200A, Keysight N5183B and Suin TFG6806 generators, R&S HMP4040 power supply,
176-channel voltage scanner (Arduino + multiplexers + Agilent 34405A), 1-Wire temperature
sensors and a switch on a second ("slave") Arduino, three CAN lines (CANable + IXXAT) with
telemetry decoding, test sequences and CSV logging.

---

## 1. Installation and start

### Which Python packages are needed?
```
pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial python-can
```
Optional: `pip install pyinstaller` (to build the EXE), `pip install gs_usb pyusb` (only for a
CANable with *candleLight* firmware).

### What else must be installed on the PC?
| For | Install |
|---|---|
| Instruments over **USB** (34405A, TFG6806 / N5183B via USB) | **Keysight IO Libraries Suite** (or NI-VISA). Restart Windows afterwards. |
| IXXAT CAN adapter | **IXXAT VCI 4** driver |
| Uploading the Arduino sketches | Arduino IDE + library **OneWire** (Paul Stoffregen) |

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
Yes: `--demo`. FSW, generators, scanner, HMP4040, 1-Wire sensors, switch and three CAN lines
(BOS / AFAR / UPNG) are simulated. Demo mode never overwrites your saved CAN line settings.

### How do I build an EXE?
Run `build_exe.bat`, or in the PyCharm terminal (one line):
```
python -m PyInstaller --noconfirm --clean --windowed --onefile --name FSW_Monitor --icon app.ico --add-data "app.ico;." --hidden-import bench_monitor --hidden-import canable_analyzer --hidden-import can_commands --hidden-import telemetry --hidden-import smw_generator --hidden-import dashboard --hidden-import log_viewer --hidden-import onewire_temp --hidden-import slave_switch --hidden-import tfg_generator --hidden-import mxg_generator --hidden-import pyvisa_py --hidden-import pyvisa.ctwrapper --hidden-import serial.tools.list_ports --collect-submodules pyvisa --collect-submodules pyvisa_py --collect-submodules can --exclude-module tkinter --exclude-module matplotlib fsw_vsa_monitor.py
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

All next to the program / EXE (logs: in the log folder chosen on the Bench tab, default the same).

| File | Contents |
|---|---|
| `channels.txt` | Names of the 176 voltage channels (`17=VCC_CORE`). Name a channel `GND` to skip it in scans. |
| `onewire_names.txt` | Names of the 1-Wire sensors (`28FF4A1B931603D8=Cold plate`) |
| `dashboard.json` | Test sequences, telemetry watch list, Main-tab settings |
| `switch.json` | Switch output names, watchdog settings |
| `can_commands.json` | CAN command library and CAN scripts |
| `can_lines.json` | CAN line setup (adapter, channel, bitrate per line) |
| `voltage_log_YYYY-MM-DD.csv` | Voltage scan: Time, Channel, Voltage |
| `hmp_log_YYYY-MM-DD.csv` | HMP4040: master, on/off, set and measured V / A / W per channel |
| `temperature_log_YYYY-MM-DD.csv` | 1-Wire: ROM, name, temperature, CRC checks, status, bus |
| `telemetry_log_YYYY-MM-DD.csv` | Telemetry watch list values (optionally only changes) |
| `switch_log_YYYY-MM-DD.csv` | Every switch action and its reason (manual / sequence / watchdog) |
| `can_log_*.log` | CAN recording (candump format, line name as interface) |

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

## 5. Bench: voltage scanner, multimeter, HMP4040

### Do the Arduino and the multimeter have to be connected together?
No — each has its own Connect button. 1-Wire and the switch need only the Arduino; the voltage
scan needs both.

### Auto-detect does not find the 34405A.
1. Install **Keysight IO Libraries Suite**, restart Windows, re-plug USB.
2. Device Manager must show the 34405A under **"USB Test and Measurement Devices"**
   (not "Other devices" — then the driver is missing; an IVI/MATLAB driver package is *not* the
   USB driver).
3. Check the log line after Auto-detect:
   `VISA: Visa Library at C:\WINDOWS\system32\visa32.dll` → correct.
   `VISA: pyvisa-py …` → the installed VISA could not be loaded; the line says why.

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
USB: the COM port (virtual COM, 9600 Bd), e.g. `COM7`. LAN: the IP (`192.168.1.20` or
`192.168.1.20:5025`). A VISA string also works.

### Power cycling
Bench tab → *Power cycling*: ON time, OFF time, total duration. It switches the master output.
If the program is closed while cycling runs, it asks first and then switches the master output OFF.

### "CC" on the Main tab
The channel is in current limit (current at the set limit and voltage below the set value).

### How are the −5…0 V and 16 V channels converted?
* Channels **144–159**: an op-amp inverter (−3 V in → +3 V at the multiplexer); the program flips
  the sign.
* Channels **160–175**: divider 100 kΩ (to the source) / 33 kΩ (to GND); the program multiplies by
  (100 + 33) / 33 = 4.03. The values are `R_TOP` / `R_BOTTOM` at the top of `bench_monitor.py`.

### How do I skip unused channels?
Name them `GND` in `channels.txt` (or double-click the name in the table). They are greyed out and
left out of range scans; manual mode can still read them.

### The scan is slow.
The timing line under *Last scan* shows *switch* and *DMM* time per channel.
* *switch* ≈ 16–20 ms → set the USB-serial adapter's **Latency Timer** to 1 ms
  (Device Manager → COM port → Port Settings → Advanced).
* *DMM* is the multimeter's own speed (NPLC 1 + auto-zero); scan fewer channels if needed.

### Voltages drop when many units are connected.
Usually signals outside the multiplexer's 0…5 V supply range (leaking through the CD74HC4067
protection diodes even when switched off), high-impedance sources, or ground loops. Every input
must stay within 0…5 V at the multiplexer pin; all grounds should meet at one point.

---

## 6. Arduinos, 1-Wire sensors and switch

### How are the two Arduinos wired?
| Master | | Slave |
|---|---|---|
| A5 (TX) | → | D2 (RX) |
| A4 (RX) | ← | D3 (TX) |
| GND | — | GND (required) |

Sketches: `arduino/sketch_sep24a` on the **master**, `arduino/slave_onewire_switch` on the
**slave** (needs the OneWire library). Link speed 19200 on both sides.

### Switch or 1-Wire does not work — where is the problem?
Bench tab → Arduino connected → **Test slave link**. It checks PC → master → slave → 1-Wire step
by step and tells where it stops (wrong COM port, old master sketch, slave not answering =
wiring/GND/baud, no 1-Wire device, sensors and their readings). It does not switch any output.

You can also test the slave alone: Serial Monitor on its USB, 115200, "Newline":
`SW:SET:0:1` → `SW:STATE:10`, `OW:CONV` then `OW:READ` → one `OW:DEV:…` line per sensor.

### 1-Wire: "No 1-Wire device answers"
* Line **LOW (0 V)**: short to GND, missing pull-up, or a sensor connected the wrong way round
  (it gets warm). **Swapped GND and +5 V** is a common cause.
* Line **+5 V but no answer**: data wire broken, sensor GND not connected to the slave's GND,
  legs swapped, or VDD floating (with parasitic power VDD must be tied to **GND** at the sensor).
* DS18B20 pins (flat side facing you, legs down): **GND – DQ – VDD**.
* For a standalone check upload `arduino/onewire_check` and read the Serial Monitor (115200).

### Long cables (10 m) with parasitic power
Use **2.2 kΩ** pull-up (not below ~1.5 kΩ) from each data line to +5 V of the slave, twisted pair
data + GND, no branches. Most reliable: power the sensors from a third wire (VDD → +5 V).
"85 °C power-on value" = the conversion did not get enough power.

### How do I rename a sensor?
1-Wire Temp tab → double-click the name. Names are kept in `onewire_names.txt`.

### Can the Arduino output 3.3 V?
No — a 5 V Arduino pin is 5 V when high. Use a divider (1 kΩ series + 2 kΩ to GND ≈ 3.33 V) for a
high-impedance input, a 3.3 V logic buffer / level shifter, or a transistor switching the 3V3 rail.

### The switch is active-low (relay module).
Set `SW_ACTIVE_LOW = true` in the slave sketch and upload it again.

### What does the BOS watchdog do?
If no BOS data arrives for the set time (default 60 min) it switches an output (pulse or ON).
"BOS data" = CAN frames with the BOS ID prefix `0x1A46` and/or FSW VSA results with MER above the
threshold (default 10 dB). Choose *CAN or FSW*, *both*, *CAN only* or *FSW only*. The time counts
from arming / the last trigger. Every trigger goes to `switch_log`.

---

## 7. CAN and telemetry

### How are the three lines set up?
CAN tab → *CAN lines*: one row per line (default **BOS** = CANable, **AFAR** = IXXAT CAN1,
**UPNG** = IXXAT CAN2) with interface, channel/port, bitrate. *Connect* opens all ticked lines;
each runs in its own thread, so all are captured at the same time.

### Can the IXXAT read both channels at once?
Yes — CAN1 and CAN2 are separate controllers; each line has its own reader. A single-channel
IXXAT model has only CAN1. The IXXAT driver has **no listen-only mode**.

### Listen-only — on or off?
On: the adapter is invisible on the bus. **Off** if the adapter is the only other node on that
line (otherwise the unit retransmits its first frame forever, because nobody ACKs it).

### How do I see only one line?
Untick the other lines under *Show lines* next to the ID filter. The ID filter accepts
`100-1FF, 7E8, !7DF, 18FEF100, 700/700`.

### On which line is a command sent?
A library command with a fixed line (*Send on line* in its dialog) always goes there; otherwise the
line selected in the Transmit panel. Commands captured from received frames take that frame's line.

### Telemetry shows nothing.
Telemetry is decoded from the CAN frames of BOS (`0x1A46…`), UPNG (`0x1A86…`) and AFAR (`0x1AC6…`).
Check that the line carrying the unit is connected and frames arrive (Monitor view).

---

## 8. Main tab and test sequences

### Which steps can a sequence contain?
HMP outputs, SMW, CAN command, Pause, FSW (run / stop / single / auto level), FSW settings,
voltage Scan start/stop, Repeat … End, Log message, **Switch** (ON / OFF / pulse), **TFG**
(channel(s), any parameters, trigger / align / recall), **MXG** (any parameters, single sweep /
trigger / recall / preset).

### What happens when a step fails?
The sequence stops and the log says why (device not connected, SCPI error, timeout).
Steps that talk to an instrument wait until it has confirmed.

### Telemetry watch on the Main tab
Pick unit → array → value → *+ Add*. *Log to CSV* writes `telemetry_log_*.csv`;
*only changes* writes a row only when the value changes.

---

## 9. Where do I get help?
Check the log at the bottom of the window first — almost every error is reported there with the
reason. For instrument problems compare with Keysight Connection Expert / the instrument's own
error queue (`SYSTem:ERRor?` in the SCPI console).
