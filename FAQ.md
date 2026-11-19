# KTR TEST — FAQ

Automated test station for the **BOS** modulator unit (BPSK / OQPSK; MPF300T FPGA, DAC38J82, LMX2594,
ADMV1013, ADRF5730, CHA3397): R&S FSW signal analyzer, BOS CAN control and telemetry, automatic
fault diagnosis with reports, plus R&S HMP4040 / Keysight N6701C power supplies, Suin TFG6806
generator, six CAN lines (two per unit: BOS / AFAR / UPNG CAN1 + CAN2), test sequences and CSV logging.

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
| Instruments over **USB** (TFG6806, N6701C via USB) | **Keysight IO Libraries Suite** (or NI-VISA). Restart Windows afterwards. |
| IXXAT CAN adapter | **IXXAT VCI 4** driver |

Instruments on **LAN** (FSW, TFG6806, HMP4040, N6701C) need no driver.

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
Yes: `--demo`. FSW, TFG6806, HMP4040, N6701C, six CAN lines (BOS / AFAR / UPNG, CAN1 + CAN2) and a complete
BOS unit with injectable faults (BOS Test Station → *Simulator*) are simulated. Demo mode never overwrites your saved CAN line settings.

### How do I build an EXE?
Run `build_exe.bat`, or in the PyCharm terminal (one line):
```
python -m PyInstaller --noconfirm --clean --windowed --onefile --name KTR_TEST --icon app.ico --add-data "app.ico;." --hidden-import bench_monitor --hidden-import canable_analyzer --hidden-import can_commands --hidden-import telemetry --hidden-import dashboard --hidden-import log_viewer --hidden-import tfg_generator --hidden-import help_tab --hidden-import n6700_supply --hidden-import iq_demod --hidden-import bos_station --hidden-import bos_analysis --hidden-import pyvisa_py --hidden-import pyvisa.ctwrapper --hidden-import serial.tools.list_ports --collect-submodules pyvisa --collect-submodules pyvisa_py --collect-submodules can --runtime-hook rthook_visa.py --exclude-module tkinter --exclude-module matplotlib fsw_vsa_monitor.py
```
Result: `dist\KTR_TEST.exe`. Without `app.ico`, remove `--icon app.ico --add-data "app.ico;."`.
When adding a new module to the program, add a `--hidden-import <module>` for it.

### The EXE closes immediately / shows nothing.
Build once **without** `--windowed` and start it from a terminal — the error is printed there.

### The EXE starts slowly.
`--onefile` unpacks itself on every start. Use `--onedir` and copy the whole `dist\KTR_TEST`
folder instead.

---

## 2. Files the program creates

All next to the program / EXE (logs: in the log folder chosen on the HMP4040 tab, default the same).

| File | Contents |
|---|---|
| `bos_station.json` | BOS Test Station: signal / chain settings, limits, unit control, test plans |
| `bos_reports\BOS_<serial>_<date>.html` | Test report per run (verdict, findings, measurements, plots, corrections) |
| `bos_reports\bos_history.csv` | One row per run of every unit (History sub-tab) |
| `dashboard.json` | Test sequences, telemetry watch list, Main-tab settings |
| `can_commands.json` | CAN command library and CAN scripts |
| `can_lines.json` | CAN line setup (adapter, channel, bitrate per line) |
| `device_addresses.json` | Instrument addresses and connection settings (FSW, HMP4040, N6701C, IT6832, TFG6806), saved after each successful connection |
| `hmp_log_YYYY-MM-DD.csv` | HMP4040: master, on/off, set and measured V / A / W per channel |
| `telemetry_log_YYYY-MM-DD.csv` | Telemetry watch list values (optionally only changes) |
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

### The FSW has no K70 option — can I still see the constellation?
Yes. On connect the program checks the options (`*OPT?`). Without K70 the **VSA (K70)** tab is disabled and
**I/Q (software demod)** is selected: the FSW's standard I/Q Analyzer records raw I/Q, and the program
demodulates it (BPSK, QPSK, OQPSK, 8PSK, 16QAM; RRC filter, frequency / phase / timing recovery). Set center,
level, modulation, symbol rate, filter / roll-off, samples per symbol and capture length on the left.
Results (MER, EVM, power, carrier frequency error, symbol-rate error, I/Q offset) feed the Main tab, the log and *FSW single* / *FSW set* steps. MER / EVM are not calibrated like K70's, and the signal
should be continuous (no burst search). Try it without hardware: `python fsw_vsa_monitor.py --demo-no-k70`.

## 4. Generator (TFG6806)

### General behaviour
* **Apply** sends only the values that differ from the instrument; output/RF switches are sent last.
* After every change the instrument's error queue is read; an error appears in the log next to
  the setting that caused it (e.g. `Frequency: -222,"Data out of range"`).
* Each tab has a SCPI console for anything without a field.

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

## 5. Power supplies (HMP4040, N6701C, IT6832) and VISA

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
HMP4040 tab → *Power cycling*: ON time, OFF time, total duration. It switches the master output.
If the program is closed while cycling runs, it asks first and then switches the master output OFF.

### "CC" on the Main tab
The channel is in current limit (current at the set limit and voltage below the set value).

### N6701C: how do I connect?
N6701C tab: IP address (raw socket 5025) or a VISA string (`TCPIP0::…::inst0::INSTR`, USB via Keysight IO
Libraries). The module models and ranges are read from the unit. *Clear protection* resets OV / OC / OT.

### ITECH IT6832: how do I connect it?
The IT6800-series supplies (IT6832: 0-32 V, 0-6 A, 192 W) have a **TTL-level** serial port on the rear DB9 -
not RS-232, never connect it to a PC's RS-232 port. Use ITECH's adapter (IT-E121 / IT-E122) or an **Arduino as
USB-to-TTL bridge**: load `arduino/it6800_bridge/it6800_bridge.ino` and wire **DB9 pin 3 (supply TX) → Arduino
RX, DB9 pin 2 (supply RX) ← Arduino TX, DB9 pin 5 → Arduino GND** (measured on an IT6832; the DB9 shell is not
signal ground; normal TTL polarity, `INVERT_TTL 0`) (Leonardo / Micro / Mega: hardware Serial1; Uno / Nano: pins 10 RX / 11 TX with
SoftwareSerial). The port uses 5 V levels: a 5 V Arduino, or a level shifter for a 3.3 V board.
On the supply's front panel set the baud rate (4800-38400, default 9600) and the communication address; set the
same on the **IT6832** tab, choose the COM port and *Connect*. Keep *Arduino bridge (wait 2 s)* ticked for an
Uno / Nano (they reset when the port is opened).

*Protocol*: **Frame** for the IT6800 series (IT6821...IT6834, 26-byte binary frames: AAh, address, command,
22 data bytes, checksum = sum of the first 25 bytes) - remote 20h, output 21h, max. voltage 22h, voltage 23h
(mV), current 24h (mA), read 26h; or **SCPI** for the IT6800A/B models (USB / RS-232 built in, e.g. IT6832A):
SYSTem:REMote, VOLTage, CURRent, OUTPut, MEASure:VOLTage? / CURRent?, VOLTage:PROTection, SYSTem:ERRor?.
The tab has V / I / max. voltage, output on / off, measured V / I / P with CV / CC, a plot, a daily
`it6832_log_YYYY-MM-DD.csv` and a **command console** (frame: type the command byte and data in hex, e.g.
`26` or `23 E0 2E 00 00`, or a whole frame `AA 00 26 …`; the sent and received frames are shown in hex).

The protocol is implemented as in ITECH's *IT6800 Series Frame Format Programming Guide* (chapter 3): set
commands are answered with a 12h frame (80h ok, 90h checksum error, A0h parameter error / out of range, B0h not
executed, C0h not effective); 26h returns current [mA], voltage [mV], state (output, over-temperature, CV / CC /
unregulated, fan speed, remote), set current, max. voltage and set voltage; 31h (read on connect) returns model,
software version and serial number - the tab warns if the model differs from the one selected; 37h enables the
front-panel Local key. IT6832: programming resolution 10 mV / 10 mA, max. voltage (LVP) up to 33 V, address 0...30
(front panel >ADDR).
**No connection?** Load `arduino/it6800_probe/it6800_probe.ino` instead of the bridge (same wiring) and open the
Serial Monitor at 115200 baud: it talks to the supply without the PC program, checks the idle level of the
supply's TX line, scans all baud rates and addresses 0...30 with the remote command (20h), shows any byte received
in hex and, when found, reads model / serial (31h) and voltage / current (26h) every 2 s. `t` checks the Arduino
pins with a jumper 10-11. Its output tells whether the problem is the wiring, the supply settings or the PC side.
**Polarity:** some ITECH TTL ports have 5 V levels but RS-232 polarity (idle LOW) - reported for an IT6302 on the
Arduino forum. With the supply on, the supply's TX pin idles at about +5 V (normal TTL) or about 0 V (inverted).
For an inverted port set `INVERT_TTL 1` in the sketch: it then uses SoftwareSerial with inverted logic on pins 10
(RX) / 11 (TX) on every board. If nothing answers, also try swapping the two signal wires.
In the BOS station the IT6832 can be chosen as the BOS supply (*Unit / signal*); the Main tab shows its status and
the power watchdog can switch its output off.

### What does the power watchdog do?
Main tab → *Power watchdog* → *Conditions…*: switch the HMP4040 master and/or all N6701C outputs OFF and stop
the sequence when a step fails, a supply stops communicating, an output stays in current limit (CC) longer
than N s (short inrush is ignored), an N6701C protection trips, or a current exceeds N A. Monitor only during
sequences or always. A trigger stays latched (red) until *Reset* or the next run and is written to
`watchdog_log_YYYY-MM-DD.csv`.

### The TFG6806 waveform display
Drawn from the channel's settings (the generator has no command to read its screen), like the generator's own
display: very fast carriers in modulation / sweep / burst are drawn with fewer cycles ("not to scale").

## 5a. Control KTR (manual unit control)

### What is the Control KTR tab?
The first tab. Left: sub-tabs **BOS**, **AFAR**, **UPNG** (AFAR and UPNG follow later). BOS shows the commands of
ПИЛВ 0102-004 **version 2** in three groups — **Basic commands** (36), **Session (СР) control commands** (12),
**Technological commands** (4) — click a group to open it, or type in *Filter* (code, mnemonic, text). Codes in
orange are executed only in standby (ДЖР).

### How do I send a command?
Pick it: the editor shows its section, where it is executed, receiver, number of data bytes, notes, and **every
data field** of the document (lists for coded values, ranges, units; time fields take `now` / `now+30`). A
*Preset* fills the fields. The frame is shown live (`BOS CAN1: ID 0x08696004 [8] FF 00 …`); values the document
prohibits are refused before sending. Choose sender, CAN line 1 / 2, *Request КВ* and the expected answer
(КВ+ / КВ- / any), then **▶ Send now** — the answer (КВ+ / КВ- with the reason, the line it came back on, the time)
appears under the button. In demo mode the commands go to the simulated BOS.

### How does the queue work?
**＋ Add to queue →** appends the command (after the selected step). Equipment steps are the same as in the Main
tab sequences: Pause, HMP4040, N6701C, IT6832, TFG6806, FSW run / single / auto level, FSW settings, CAN library
frame (AFAR / UPNG meanwhile), BOS test plan, Repeat … End, Log message, plus an **Operator prompt** that waits for
OK. Double-click a step to edit it — a BOS command opens in the editor on the left with *✔ Update queue step*.
**▶ Run queue** / **▶ From selected** run it; every КВ and every instrument is waited for, the results are listed
under the queue. *Stop on wrong / missing КВ* stops at the first unexpected answer; an instrument error always
stops. Several named queues; stored in `ktr_control.json` next to the program. The tab does not send while the
BOS Test Station is running a plan.

### How do I create, load and send a CONFIG (MD_CONFIG) or MD_SETTINGS file?
**Telemetry → BOS → MD_CONFIG / MD_SETTINGS.** Every field of table 4.4 (MD_CONFIG, 136 bytes) or tables 4.6 / 4.7
(MD_SETTINGS, 1024 bytes) is listed with its byte, coded values and meaning; MD_SETTINGS is split into DAC pll_vco,
DAC offsets (16 temperature ranges × qmc_offseta/b, qmc_gaina/b, qmc_phaseab, coarse_dac), power adjustment and
OG DAC tables. Grey fields are set by the BOS (not checked when written).
* **New (defaults)** — the document defaults (MD_CONFIG checksum 0xF372, as in §4.1.2); **Example (doc)** fills the
  OG DAC example of MD_SETTINGS.
* **Load file… / Save file…** — `.json` with named values (readable, editable), `.bin` raw bytes; loading also
  takes an ИОК_КОНФИГ / ИОК_SETTINGS dump with its 8-byte header, or hex text.
* **⇣ Read from BOS** — REQ_TM 0xFA / 0xFE: the values the BOS uses now.
* **⇡ Send to BOS** — the array in 17 / 128 CAN packets (start / current / stop with КВ request, MD number
  0x01 / 0x04, table 4.2) on the station's CAN line 1 and sender; the КВ is shown. The CONFIG checksum is
  recomputed on every change; a wrong checksum or prohibited value asks before sending.
* **Save in BOS flash** (MD_CONFIG) — SAVE_CONFIG_NEW: a sent CONFIG is only in RAM until then.
MD_SETTINGS values are loaded into the DAC when a session starts; the OG DAC value in standby (it switches the
10 MHz reference tuning off). The document page saying which command stores MD_SETTINGS in EPROM is cut off in
the copy I have — check before relying on it surviving a power cycle.

### What is new in 0102 v2?
Pause_SR 0x52 (delay of the physical session, OG tuning in session); session commands Send_UB_BERT 0xAE,
Send_UB_Data_60 0xAD (BPSK index +60°), Send_BERT 0xAF (test mode); technological commands Change_DAC_1 0x50
(QMC offsets / gains / phase, coarse_dac), Change_DAC_2 0x53 (pll_vco), Change_DAC_OG 0x51 (50 MHz OG DAC); the
data fields of Change_FlashTM 0xE0 and Set_TimeSend 0xAA; Stop_I2C 0x01 from the BOS. All are in
`bos_commands_0102.json` (the station tests and the CAN tab use the same file).


### What does it do?
It tests a BOS modulator automatically and tells the operator **what is wrong and where to look**. A test plan
switches the unit through its modes over CAN (RF off, carrier, BPSK, OQPSK, attenuator codes), captures the output
with the FSW (raw I/Q from the I/Q Analyzer — K70 is not needed), reads the BOS telemetry (PLL locks, JESD,
FPGA rails, temperatures, 10 MHz reference) and analyses everything. Results: a verdict (PASS / PASS with
warnings / FAIL), a measurement table with limits, **findings** with the most likely faulty block, a block
diagram of the modulator coloured by suspicion, suggested **corrections**, an HTML report and a history.

### Where are the tests and plans stored?
| What | Where |
|---|---|
| Built-in tests (Carrier, BPSK, OQPSK, attenuator, PA linearity, ШР, BER, …) | program code: `bos_station.py`, methods `_t_<id>` (e.g. `_t_cw`); measurements in `bos_analysis.py` |
| Their step-by-step description (*Plan sequence* column) | `bos_plan.py` |
| **Plans** (which tests / steps, in which order) | `bos_station.json` next to the program → `"plans"` |
| Plan options: preparation, **plan parameters** | `bos_station.json` → `"plan_opts"` |
| Station settings, limits, unit control | `bos_station.json` → `"cfg"`, `"lim"`, `"ctl"` |
| BOS commands and their data fields (ПИЛВ 0102-004) | `bos_commands_0102.json` |
| CAN tab command library and scripts | `can_commands.json` |
| Main tab sequences | `dashboard.json` |

Back up / copy `bos_station.json` to move plans to another PC.

### How do I make my own plan or change one?
**Plans ▾ → New empty plan…** (or *New plan…*, *Duplicate plan*, *Edit plan…*). The plan editor lists the items;
**+ Add ▾** inserts after the selected one:
* **Built-in test** — any of the station's tests;
* **BOS command (0102)** — any command of `bos_commands_0102.json`; its data fields appear as inputs (lists for coded
  values, ranges from the document), a preset fills them, the CAN frame is shown live and prohibited values are
  refused; expected answer КВ+ / КВ- / none, CAN line 1 or 2;
* **CAN frame** — any command of the CAN tab library (AFAR, UPNG, other lines);
* **BOS RF mode** — RF off / carrier / BPSK / OQPSK session with an attenuation (10 MHz reference check and
  target designation included, as in the built-in tests);
* **Power supply** — station supply or a given HMP4040 / N6701C / IT6832 output: set V and current limit, output
  on / off, or **measure V and I against limits** (with a settle time);
* **Telemetry check** — any telemetry value (temperatures, rails, PLL flags, PulsNumb_10MHz, Err_Ref_LO …) against
  min / max, on the PC;
* **Instrument SCPI** — command or query to the FSW or TFG6806, numeric limits or expected text;
* **Operator step** — instruction to confirm, a Pass / Fail question (with a remark), or a value the operator
  enters (multimeter, visual check …) against limits; *Abort plan* stops the run;
* **Wait**, **Note / section**.
Every step gives rows in the results table and the report (PASS / FAIL against its limits). Double-click edits,
drag or ▲ ▼ reorders. **Preparation**: *Automatic* runs reference & BOS power-up, CAN1 / CAN2 check and link setup
only when the plan has built-in BOS tests — a plan of supply / operator / PC steps runs without them and without
the FSW; *Always* / *Never* force it.

### Can I change command parameters directly in the *Plan sequence* column?
Yes (when no plan is running):
* **✎** on a step of your own opens its editor (command parameters, limits, texts); **▲ ▼** move it, **✕**
  removes it; **+ Step** adds a test or step at the end;
* **✎** on a built-in test, or next to its →BOS command lines, opens the **plan parameters** of that test —
  frequency code N, session command, УР / ШР rates, Time_SNR, telemetry periods, sender, CAN lines, reference /
  TFG settings. Ticked values apply **to this plan only** (the Control / Reference pages stay the station
  defaults); the column, the CAN frames and the FSW carrier follow them at once, e.g. one plan at N = 656 and
  another at N = 944.
Limits stay common (Limits page).

### Can I remove or change a single command inside a built-in test?
Yes — every →BOS command line of a built-in test (and the MODE_D after the plan) has **✎** and **✕** in the
*Plan sequence* column (open the line with ▸ if needed):
* **✕** — the command is **not sent in this plan**; the line is struck through and marked *[not sent]*, **↺**
  sends it again. During the run the log says so and the row that would have shown its КВ+ becomes **SKIP**
  "not sent (removed in this plan)". A test that needs the answer (e.g. REQ_TM before a telemetry check) may then
  fail — that is reported, not hidden.
* **✎** — the command's data fields from `bos_commands_0102.json` with what the test sends; tick any field to
  replace it (e.g. fix Atten_EIRP of the carrier session at 5 dB, a different SET_PeriodTM period, SET_CAN = 03).
  The frame the plan will send is shown; values the document prohibits are refused. *Restore original* removes
  the change. Fields the test changes while it runs (session frequency / attenuation) stay fixed at your value
  every time it is sent — the test's own limits do not know that (e.g. power then reads 5 dB low).
A command is identified by its test and code (SET_PeriodTM / REQ_TM also by the telemetry array), so one change
applies to every time that test sends it. Stored per plan in `bos_station.json` → `"plan_opts"` → plan →
`"cmds"`; other plans are not affected. TFG6806 SCPI lines are not affected (use *Plan parameters* for the
reference settings).

### What exactly does a plan do? (the *Plan sequence* column)
The column between the settings and the results lists the selected plan **step by step, in the order the station
runs it**, built from the current settings (it changes as soon as you change a setting or the plan):
* **→BOS** commands to the BOS over CAN: mnemonic, parameters (frequency code N and carrier, Atten_EIRP, rate,
  number of КБД) and - one click further - the CAN identifier and data bytes exactly as sent; **BOS→** what comes
  back (acknowledgement КВ+ within 300 ms, ИОК_КОНФИГ, telemetry);
* **FSW** each I/Q capture with centre frequency, sample rate, number of samples, duration and size, and - one click
  further - the SCPI commands (INST:SEL, FREQ:CENT, TRAC:IQ:SRAT / RLEN, ADJ:LEV, INIT, TRAC:IQ:DATA:MEM?, the IF
  power trigger of the ШР packet capture);
* **PSU** supply current readings (HMP4040 / N6701C), **Oper.** dialogs for the operator (CAN-library or manual
  control), **Wait** settling times, **Loop** repeated steps (attenuator settings, LO lines, stability, BER),
  **PC** what is calculated from the data.
"Before the plan" and "After the plan" show the automatic parts (FSW options read, control link test added in
protocol mode, MODE_D at the end, report). Click a line with ▸ to open it, ⊞ / ⊟ open or close everything. While a
plan runs, the current test is highlighted and followed, finished tests get ✓. The splitter handle hides the column.
In Russian the column is in Russian; in the other languages it stays in English.

### Which tests are there?
| Test | Measures | Typical faults it finds |
|---|---|---|
| Pre-check | Telemetry present, 6 PLL / JESD flags, 1 V / 1.8 V / 2.5 V rails, temperatures, 10 MHz, Err_Ref_LO | Unlocked LMK / LMX2594 / DAC clock / FPGA PLLs, JESD link error, bad rail, overheating |
| Supply current | Bench supply current RF off → RF on | PA not biased / not enabled, short circuit |
| Carrier (CW) | Frequency (Hz, ppm), power, integrated phase noise, spurs | Reference offset, LO programming error, noisy LO |
| BPSK / OQPSK | MER, EVM, frequency, symbol-rate error, carrier leakage, gain imbalance, quadrature error, phase / magnitude error, PA compression (PAPR), ACLR, OBW, asymmetry, spurs, detected modulation | DAC / ADMV1013 I/Q errors, LO leakage, Q-rail fault, wrong mode, PA compression, clock errors |
| Attenuator | Output power at 11 codes → least-squares weight of each ADRF5730 bit | Stuck / missing bit, wrong bit weight |
| PA linearity | Compression and ACLR at the nominal and a higher attenuation | Confirms PA compression vs. digital clipping |
| LO harmonics | Lines at carrier × k/4 | LO / multiplier leakage of the ADMV1013 |
| Stability | Power, frequency, MER over time with temperature | Thermal drift of PA / reference |

### OQPSK shows 16 points (each point a 2 × 2 square) / BPSK points doubled along I - what is measured?
**I/Q time skew (OQPSK, QPSK):** the I and Q rails reach the modulator at different times. The analyser can only
choose one timing, so each rail is sampled off its eye centre and its value depends on one neighbour symbol: two
levels per rail → a 2 × 2 square per point. The station finds each rail's own eye centre (the symbol-rate line of
the squared rail, in blocks of 256 symbols - a clock-rate error cancels) and reports the **skew in ps** (limit 250
ps, ≈ 3 % of a ШР symbol), the spread between blocks, and the **MER with each rail at its own centre** (what the
skew costs). The size is reliable (±~15 ps in tests); the **sign cannot be told** from the signal because a 90°
carrier rotation swaps the rails. The stability test tracks it against temperature. Typical cause: unequal baseband
filters between DAC38J82 and ADMV1013 - X7R / X5R capacitors drift with temperature (worse in the cold); fix with
C0G / NP0 parts and matched rails, or a fractional delay of one rail in the FPGA (Corrections tab).

**BPSK level split:** the amplitude depends on whether the neighbour symbol has the same sign; with SP-L one
neighbour is always opposite, so each point is doubled along the I axis. Reported in % of the amplitude (limit
10 %) with the side: **previous symbol dominant** = slow edges in the transmitter (the old level lingers - baseband
filter / DAC / ADMV1013 input, temperature-dependent parts); **both alike** = symmetric band limitation, usually the
measurement (VSA measurement filter not rectangular, < 8 samples per symbol). Simulator faults for training:
*I/Q time skew*, *Baseband filter too slow*.

### How does the diagnosis decide?
By cross-checking measurements that point at different blocks, for example:
* carrier error and symbol-rate error with the **same ppm** → the common 10 MHz reference; carrier error with a
  correct symbol clock → LMX2594 / LO frequency plan; symbol-rate error with a correct carrier → DAC/FPGA clock;
* **BPSK good but OQPSK bad** → the Q rail (DAC channel B, its lanes, ADMV1013 Q input);
* high phase error with little magnitude error → LO phase noise (slow wander = loop, fast = VCO / clock jitter);
* PAPR loss that **disappears with more attenuation** → PA compression; that does not → digital clipping;
* power low with a verified attenuator → PA / cable; telemetry PLL / JESD flags override signal-based guesses.

Each finding lists suspect blocks with weights and concrete next steps. The rules rank where to look first; they
are not proof. Click a block in the diagram to see only its findings.

### How is the unit switched between modes?
By default the station talks the BOS protocol itself (*Unit control* → *BOS protocol*): standby `MODE_D` (0x01),
carrier `Send_Carrier` (0xA0) and the session commands 0xA1…0xA4 with frequency code N, Atten_EIRP (1 dB, 0…20 dB),
rate and polynomials; every command asks for an acknowledgement and a КВ- or a missing acknowledgement stops the
test with the reason. Choose which session command produces BPSK and which OQPSK, and the frequency code N
(37650 + 0.125·N MHz; the carrier on *Unit / signal* follows it). Before a plan the station runs *Control link &
BOS setup*: NOP, standby, SET_Time_SNR (default 255 s), telemetry periods (ИОК_ДОП is off by default!) and reads
ИОК_КОНФИГ (CRC-16 checked). While transmitting it sends a target designation (0°, 0°) every 0.5 s — the BOS only
transmits with a valid designation (valid 3 s) — and re-starts the session before captures so Time_SNR never ends
it. At the end the unit is put back into standby.
The station sends as **ВМ 2 (sender 0x21)**: command IDs to the BOS are `0x0869xxxx` (e.g. MODE_D
`0x08690001`), target designation Angle_ErdStation_BKU always from БИОС 1.1 (sender 0x1C) = `0x070D200C`, acknowledgements come back as `0x0A61xxxx`. If the BOS answers КВ-,
the sender address may not be allowed for that command: choose another sender (ВМ 1 0x20, КПА ИВУ 0x18) on the
Unit control page / in Control KTR. Settings and CAN-library BOS commands saved with the old default 0x18 are
switched to 0x21 once, at the first start of this version (the log says so). The alternatives *CAN library* and *Ask the operator* remain for special set-ups.

### Which symbol rates are used?
BPSK is the narrowband mode (УР): symbol rate = 2 × data rate (32 kbit/s → 64 ksym/s, 128 kbit/s → 256 ksym/s).
OQPSK is the wideband mode (ШР): always 131.072 Msym/s, whatever the data rate. With protocol control both follow
the УР / ШР rates on *Unit control*.

### FSW with 160 MHz or with a bandwidth extension?
Both work. At the start of every plan the station reads the FSW options (`*OPT?`: B160, B320, B512, B1200 …)
and sets *FSW I/Q bandwidth* and *OQPSK capture sample rate* (switch off *FSW bandwidth from options* to set
them by hand). The OQPSK signal (RRC, roll-off 0.22) occupies 131.072 × 1.22 ≈ 160 MHz:
* **Bandwidth extension (≥ 320 MHz):** captured at 524.288 MS/s = 4 samples/symbol; PA compression is also seen
  in the OQPSK peak-to-average ratio and ACLR is measured.
* **160 MHz:** the whole signal fits; captured at 200 MS/s and resampled to 4 samples/symbol in the program.
  Spectral regrowth from PA compression falls outside 160 MHz, so compression is judged from the output power
  (PA-linearity test: a 6 dB attenuation step must lower the power by 6 dB); ACLR is n/a.

### 10 MHz reference
On the bench the external 10 MHz comes from the TFG6806; the BOS also has an internal oscillator. The BOS reports
the source in use (Sour_10MHz) and the validity of the external signal (Stat_10MHz) in ИОК_ОСН; the pre-check
compares them with *External 10 MHz expected*. Only the RF is locked to the external reference — the digital part
(symbol clock) always runs on the BOS's own oscillator. So: carrier and symbol-rate error with the same ppm → the
internal oscillator; carrier error alone while locked to the external 10 MHz → the TFG6806 or the LMX2594 frequency
plan (repeat on the internal source to tell which).

### Which roll-off does the unit really use? (Roll-off tab)
BOS Test Station → **Roll-off** (next to DAC calibration). Every OQPSK measurement estimates the RRC roll-off α:
* **spectrum** - the power spectrum of an RRC signal is a raised cosine; its 75 % / 50 % / 25 % power crossings
  give α = 6·Δf / symbol rate on both sides (PA regrowth and noise stay below -6 dB and do not bias it). When the
  signal is wider than the FSW bandwidth (131 Msym/s, α 0.25 = 164 MHz > B160) the plans add two captures tuned
  ±0.4·symbol rate off the carrier, so each transition band is measured inside the analysis bandwidth; about ±0.02;
* **matched filter** - the receive RRC α with the highest MER (after removing the I/Q errors): shows how much a
  wrong α costs; a flat curve means the roll-off does not limit the MER; with a band-limited capture its peak
  describes the capture, not the unit.
The measurement table gets a row *RRC roll-off α of the signal* (WARN when it differs from the set α by more than
0.05). Live diagnose averages the last N captures. **Use estimated α in the station** sets the OQPSK roll-off
(demodulation, OBW / ACLR, K70 filter); **Set FSW-K70 filter to it** writes DDEM:TFIL RRC / ALPH and MFIL:AUTO ON
and reads them back.

### Is the FSW set to the right filter?
Software demodulation: the measurement table shows *Demodulation filter (software)* - RRC with the station's α
(matched) for OQPSK, none for the SP-L BPSK (УР is rectangular SP-L per 0102, not RRC). FSW-K70: with the plan
option below the K70 Tx filter (RRC, α) and the measurement filter (auto = matched) are set, read back and checked
(*FSW-K70 filter (read back)*, PASS / FAIL). The Spectrum Analyzer tab's defaults are now RRC α = 0.25 as well.

### How is the modulation measured - I/Q recording or FSW-K70?
Unit control → **Modulation measured by**: *I/Q recording + software analysis* (raw I/Q from the FSW I/Q Analyzer,
all station diagnostics: I/Q errors, skew, roll-off, spectrum, decoding, findings), *FSW-K70 (VSA)* (the
analyzer's own demodulation: MER, EVM, frequency / symbol rate error, I/Q offset, gain imbalance, quadrature - with
limits; no deeper diagnostics) or *both* side by side. Without K70 the I/Q recording is used (SKIP row). **This
setting applies to every plan**; a plan measures differently only when its editor says so explicitly (*Modulation
measured by* ≠ "as on the Unit control page"). The log says at the start of each run which setting is used. K70
measures 4000 symbols per capture (2000 in the result range). With K70 the Tx filter (RRC,
α) and the measurement filter are set, read back and checked.

### The analyzer lags during the tests - what helps?
Every capture logs its times: `capture OQPSK (…): setup 0.05 s, level 0.62 s, acquire 0.01 s, transfer 0.08 s,
analysis 0.9 s - longest: …` (K70 steps: settings / level / measure), and the end of a plan sums them up. The
messages of the log panel at the bottom of the window are also written to `messages_log_YYYY-MM-DD.txt` next to
the program (one file per day) - send that file when something is slow or fails.

### The Signal tab shows an old capture / "nothing happens" during the BER test
The Signal tab now follows every capture as it arrives: the entry **▶ latest capture: …** at the top of the list
(BER loop, DAC calibration, roll-off edges, …) is redrawn after each capture. Pick another entry to look at it -
the list then stays where you put it until the next run. A capture without the BOS signal (less than 10 dB over the
FSW noise floor) is marked **NO SIGNAL** above its values (those are then noise, not the BOS); the modulation tests
wait 2 s and capture again, then re-send the session command and try once more before reporting NO SIGNAL. What was done:
* **auto level once per signal** (Unit control → FSW level): ADJ:LEV is a measurement of its own and ran before
  every capture; now it runs at the first capture of a mode / attenuation / frequency and the reference level is
  reused;
* **FSW screen off during plans** (Unit control): the FSW stops redrawing while it is remote-controlled (big
  saving with long records), restored at the end;
* roll-off estimation in Live diagnose only every 5th capture.
The recording itself ("exposure") is short for ШР (32 000 symbols ≈ 0.25 ms), but long for УР with decoding:
64 000 symbols at 64 ksym/s = 1 s of signal per capture - untick *Decode УР data* on the Unit / signal page when
the data are not needed, or lower *Symbols per capture*. If *transfer* is the longest, use LAN (HiSLIP) rather than
USB / VXI-11; if *analysis*, switch the measurement to FSW-K70.

### Sessions end too early - how long does a session last?
Unit control → **Minimum session duration** (default 10 s, also a per-plan parameter). Every session command is
sent with the largest count (2047), SET_Time_SNR is raised to at least this time, and the station estimates how
long the session really lasts: carrier = count in s; УР КБД = 2047 × 10232 bit at the УР rate; ШР КБД = 2047 ×
17912 bit (+ preambles) at the ШР rate - 36 s at 1024 kbit/s; ШР preambles = 2047 × 128 bit - only 2 s at
128 kbit/s, 0.26 s at 1024 kbit/s. When that (or Time_SNR) is shorter than the minimum, the station re-issues the
session command before it ends - never during a capture (a capture that would not fit restarts the session first).
Preamble sessions shorter than 2 s are captured with the FSW IF power trigger (armed, then the session is started).
The plan view shows the estimate for each session command.

### How do I calibrate the DAC (QMC offsets, gain, phase) from the FSW?
BOS Test Station → **DAC calibration** tab. After any BPSK / OQPSK measurement (plan or Live diagnose) it shows the
carrier offset (LO leakage), I/Q gain imbalance and quadrature error and how many LSB the DAC38J82 QMC registers
of Change_DAC_1 / MD_SETTINGS DAC_offsets (table 4.7) are off: qmc_offseta / b, qmc_gaina / b, qmc_phaseab.
One measurement gives the size but not the direction: the demodulator locks to one of four orientations on every
capture, so the sign / rail of the error changes from capture to capture.

**▶ Auto-calibrate (live)** runs a **coordinate search** (Live diagnose is started if needed), strictly in this order:

1. **Baseline** - the metric of the starting registers is measured and is the first best result. Metric = total
   error power relative to the signal in dB (carrier offset² + gain imbalance + quadrature error + remaining MER
   error), lower is better; it does not depend on the demodulator's lock orientation.
2. **One at a time** - every candidate changes ONE register by its step; all others stay at the best values. Order:
   coarse_dac (only lowered, step 1), qmc_phaseab, qmc_gaina, qmc_gainb, qmc_offseta, qmc_offsetb (BPSK:
   coarse_dac and the offsets).
3. **Delta** - each candidate's metric is compared with the current best: Δ = best − candidate (dB, positive =
   better), logged for every candidate.
4. **Greedy pruning** - a candidate with Δ ≤ *Keep a change only for Δ >* (default 0.05 dB, just above the noise),
   or one that breaks a constraint (register range, coarse_dac below its minimum, output down more than *Max.
   output drop*), is discarded at once and the next candidate starts from the best registers. An accepted one is
   the new best, and the search goes on in the same direction while it keeps improving.

A register where both directions are discarded gets half the step next cycle. The calibration is **complete** when a
whole cycle at the smallest steps brings no improvement; it also ends with **■ Stop calibration**. Either way the best
registers go back into the unit and a file is written: `bos_reports/dac_calibration/DAC_<serial>_<date>.txt` - the
final registers (signed, hex, binary), the Change_DAC_1 data bytes, baseline and final metric, and every candidate
with its Δ and verdict.
The BOS must be transmitting (session on, e.g. from Control KTR), the DAC is off in standby.

Measurement settings (panel *Auto-calibration measurement*): **captures per setting** (default 3 - |offset|²,
gain² and phase² are averaged; the spread is logged), **discard after a change** (1 capture thrown away after every
Change_DAC_1), **settle after a change** (1 s), **symbols per capture** during the calibration (32 000 instead of
the station's 4000), and **station starts and keeps the session** (MODE_D, SET_Time_SNR, session command, target
designation, re-issued before it ends, MODE_D at the end). One round is then ≈ 41 captures.

Then **Write to MD_SETTINGS…** puts the values into a DAC_offsets row with a DAC temperature threshold (send it
from Telemetry → BOS → MD_SETTINGS) - Change_DAC_1 alone lasts only until a reset / MD SETUP; repeat at a few
temperatures to fill the table. **Open in Control KTR** fills the Change_DAC_1 editor; **Send to BOS** sends the
values in the left column.

### Order at the start of every plan: TFG6806 → BOS power → 10 MHz check
The first test of every plan, **Reference & BOS power-up**, is added automatically (Unit control page,
*10 MHz reference (TFG6806) and BOS power-up*):
1. **TFG6806 first** (it must give the 10 MHz before the BOS gets power): `UTILity:CLOck:SOURce EXTernal`
   (TFG on an external reference), `UTILity:INSide:CLOck OFF` (the rear 10 MHz In/Out port is then an input), then
   on the output feeding the BOS the standard 10 MHz - **sine, continuous, 10 MHz, 2 Vpp, offset 2 V** - and
   `OUTPut1 ON`; everything is read back (source, waveform, frequency, amplitude, offset, output).
2. **BOS power**: ask the operator, or switch on the selected supply output (voltage / current from its tab;
   HMP4040 also the master), or "already on".
3. **10 MHz pulses**: first ИОК_ОСН after power-up (REQ_TM ИОК_ОСН if none comes) - `PulsNumb_10MHz` > 0, Stat_10MHz.

**Before every mode command** (carrier Send_Carrier, УР Send_UB_Data, ШР Send_Preamb / Send_KBD - also when a
session is restarted for another attenuation, and with CAN-library / operator control) the station reads a fresh
ИОК_ОСН and requires **PulsNumb_10MHz = 10 000 000 ± 5** and **|Err_Ref_LO| ≤ 3** (Limits page). It waits up to
*Reference wait* (30 s) for the BOS to settle; otherwise the test ends with an error and **no mode command is
sent**. The pre-check and the diagnosis report both values (PulsNumb off by n pulses = n / 10 ppm).

### Does the TFG6806 know whether an external reference is present? Does it fall back to internal?
From the TFG6800 manuals: `UTILity:CLOck:SOURce {INTernal|EXTernal}` / `UTILity:CLOck:SOURce?` (Programmer's Guide
2.11.17-18), `UTILity:INSide:CLOck {ON|OFF}` outputs the internal 10 MHz on the rear port (2.11.19-20). With
*External* the generator **detects the external clock itself** and switches to it only when it finds a qualified
signal (10 MHz ± 500 Hz, 100 mVpp-5 Vpp, Users' Guide 5.13): message "External clock access", **"Ext Ref On" in the
status bar**. Without it, or when it is removed ("External clock removal"), it **automatically runs on its internal
clock** (Users' Guide 3.24). There is **no SCPI query** for "external clock present" - `UTILity:CLOck:SOURce?`
returns the setting - so a missing external reference is visible only on the TFG screen; on the BOS side it shows
as PulsNumb_10MHz off by the TFG's own crystal error.

### What else is on the TFG6806 tab?
Left, below the connection: **Instrument** (trigger, phase align, read all, reset, save / recall state),
**Reference** (clock source, internal 10 MHz on the rear port, *BOS 10 MHz preset*: sine 10 MHz 2 Vpp offset 2 V,
output on), **Coupling** (CH1 / CH2 frequency and amplitude coupling, difference / ratio, direction), **Counter**
(type, gate time, trigger level, coupling, sensitivity, low-pass - the result is shown on the TFG screen, the guide
has no command to read it), **System** (power-on state *Last* so the 10 MHz settings return after a power cycle,
front panel language, brightness, key sound; network settings read only).

### Which limits come from the technical specification (TZ)?
Defaults follow the TZ: carrier 37650 + 0.125·n MHz, frequency error ≤ 2·10⁻⁶ (75.5 kHz at 37.75 GHz), symbol-rate
error ≤ 5·10⁻⁵ (50 ppm), MER ≥ 20 dB, maximum output power −9…−2 dBm at the HF output (nominal −5.5 ± 3.5 dB; at the
technological connector −30…−10 dBm: set −20 ± 10), power range 20 dB, supply current ≤ 1.3 A in session and
≤ 0.5 A in standby (at 14 V). BPSK (УР) is SP-L coded with ±90° modulation, i.e. constant envelope with
rectangular pulses: occupied bandwidth, ACLR and PAPR compression are not judged on BPSK. PA compression is found
by the PA-linearity test from the output power (a 6 dB attenuation step must lower the power by 6 dB) and, with
a wide FSW, from the OQPSK PAPR. The OQPSK chip rate is 262.144 MHz (131.072 Msym/s per rail), RRC roll-off 0.22.

### Does the station decode the data?
УР (BPSK), yes — document 0202 section 4.2: the BPSK symbols are SP-L (Manchester) decoded, the 32-bit
synchromarker 0x1ACFFC1D is searched (this also resolves the BPSK 180° ambiguity), each 1275-byte code block is
Reed-Solomon (255,223) decoded with interleaving 5 (CCSDS 131.0-B-3, dual basis) and the 1115-byte transport frame
is checked (CRC-16, SCID, virtual channel counter, idle-frame content per table 4.4). The channel BER is counted
exactly: received code bits vs. the re-encoded corrected block. Results appear in the BPSK test, on the Signal page
and in the *Data / BER (УР)* test, which collects *BER test: bits* (default 10⁶; proving the TZ's 10⁻⁷ needs about
3·10⁷ error-free bits). Errors with a clean RF signal (good MER) point at the digital data path (FPGA, buffer,
interface). Switch it off with *Decode УР data*.
ШР (OQPSK with spreading), yes — document 0202 sections 4.3, 6 and appendix Д: the capture is turned into chips
(262.144 Mchip/s, chip 2n on I, 2n+1 on Q), the preambles are despread with PRS 3 (0x00008805, restarted every bit),
the preamble value and the end-of-preamble marker 0xDA2B1E5E are checked, then the КБД are despread with
PRS 1 ⊕ PRS 2 (0x00800043 / 0x00400010), the synchromarker 0x034776C7272895B0 is checked, the code word is
de-randomised (CCSDS) and turbo decoded (CCSDS r = 1/2, k = 8920), and the transport frames are checked like УР.
The *ШР packet* test first checks the preambles (preamble-only session, works at every rate), and at ≥ 512 kbit/s
(*ШР: decode КБД from rate*) captures a whole packet from the RF onset (FSW IF-power trigger, then the session
command) and decodes two КБД. *Despreading efficiency* shows whether the BOS uses the expected spreading
sequence: with a strong signal the bits decode even with a wrong sequence, but a ground station would lose the
processing gain. Some details are not fixed by the document and are assumptions until checked on a real capture:
the shift-register form (polynomial word bit k-1 = x^k, which gives the maximal period), the chip order on I/Q, the
start of the КБД sequence (first КБД bit) and the turbo-code termination.

### Can I analyse a recording made with the FSW?
Yes: **📂 Analyze I/Q file…** on the BOS Test Station. Supported: R&S **.iq.tar** (FSW I/Q export: sample rate and
centre frequency are read from the file), **.iqw** (float32, I/Q pairs or I block + Q block), **.csv** (I,Q
columns), .npy/.npz and raw float32/int16 binaries (enter the sample rate). The program identifies the signal from
the spectrum (carrier / УР BPSK / ШР OQPSK — or choose it), finds the УР rate (lowest rate that demodulates
cleanly) or the ШР rate (preamble search), runs the same analysis and decoding as a live test and gives findings,
measurements, plots and a report. BOS telemetry is not available offline, so the diagnosis uses the signal only.
Power is right if the file is in volts (FSW export) and the cable loss is set. For a ШР decode the recording must
contain the start of the packet (preambles). *Save every capture (.iq.tar)* keeps all station captures in
`bos_reports/captures` for later re-analysis.

### How should I record a BOS signal with the FSW for analysis?
I/Q Analyzer, centre on the channel (37650 + 0.125·n MHz), **sample rate ≥ 16 × data rate for УР** (≥ 8 samples per
symbol, e.g. 1.024 MS/s at 64 kbit/s), length ≥ 3 frames (УР 64 kbit/s: ≥ 0.5 s), export as .iq.tar. With only 2
samples per symbol the FSW bandwidth cuts the Manchester pulses and even a perfect signal measures only ~28 dB MER
(decoding still works). Lock the FSW to the same 10 MHz as the BOS (TFG6806 → FSW REF IN), otherwise the analyzer's
own reference drift is mixed into the carrier-wander measurement. For ШР include the start of the packet.

### Large recordings (ШР at 275 MS/s, several GB): how fast, and what are the limits?
A 1.67 s ШР recording at 275 MS/s (about 3.6 GB, the most an FSW50 holds at that bandwidth) is handled in one go:
* **Opening** is instant: .iq.tar / .iqw / raw float32 files are *memory-mapped* (read from disk only where
  needed), other formats are converted in pieces with a progress bar. The window no longer freezes.
* **Select the part to analyse** reads the recording once for the power overview (a few seconds, progress bar);
  dragging the selection is instant.
* **ШР analysis** streams through the file in blocks at its own sample rate (no resampling of the whole record),
  with the FFT work spread over all processor cores (up to 8), and only from the start to the end of the
  transmission. Reference: 1.13 s of packet at 275 MS/s, 64 kbit/s, 4 КБД → about 1 minute on a 4-core PC, about
  2 GB of RAM besides the file itself. More cores → proportionally faster. The window stays usable; the status
  line shows the progress.
* **УР / carrier recordings at a high sample rate** are first shifted to the carrier and decimated to ~8 × the
  occupied bandwidth, then analysed as usual.

What one recording can contain: one ШР КБД takes 17 912 bits, so at 16 kbit/s it lasts 1.12 s (64 kbit/s: 0.28 s,
1024 kbit/s: 17.5 ms). At 16 kbit/s a 1.67 s recording holds one КБД only if it starts at the beginning of the
packet; otherwise the station reports the preambles / despreading and says how many bits of the КБД were missing.
To catch the packet start, trigger the FSW on the RF burst (IF power trigger) with a short pre-trigger.

**GPU (video card) acceleration** is not used: the work is limited by reading the file and by the parts that
cannot be parallelised (turbo decoding), and a GPU would need an NVIDIA card with CUDA libraries on every station
PC. Multi-core processing gives most of the possible gain without extra installation.

### What did the first real УР recording show?
`bos_UR_5s_64spd_256fd.iq.tar` (64 kbit/s, 256 kS/s, 5 s): all 30 КБД decoded, RS without corrections, CRC ok, BER 0 in
306 000 bits, idle frames exactly as table 4.4, frame counter continuous, SCID 768 (0x300), carrier on channel
n = 799 with −33 kHz (−0.9 ppm, inside 2·10⁻⁶). With the first 0.1 s (noise before the transmission) excluded
by the selection: MER 25.6 dB, phase error 1.2° (the whole file gave 16 dB / 10.5° only because of that noise).
Still found: the carrier frequency wanders about 1 kHz p-p over 5 s (up to ~450 Hz/s) - repeat with the FSW
locked to the BOS 10 MHz to see whether it is the BOS or the reference difference.

### How do I analyse only part of a recording?
After the file dialog, **Select the part to analyse** shows the power of the whole recording over time. Drag the
green edges (or the whole region), or type start / stop; *Auto (signal only)* selects where the signal is on
(within 10 dB of its strongest part), *Zoom to selection* / *Show all* change the view. Only the selection is
analysed; the report shows which part. For a ШР packet keep the preambles (start of the transmission) inside.

### How do I change the language?
**Help** tab → *Language*: English, Français, Deutsch, Italiano, 日本語, Русский (the languages of the G8 countries).
It applies at once and is remembered (`ui_language.txt` next to the program). Translated: tabs, buttons, labels,
settings, limits, table headers, test and measurement names. With **Русский** the findings (titles, explanations,
most likely location, what to do), the step / verdict line, the corrections and the HTML report (verdict, measurements,
units, PASS → НОРМА / FAIL → НЕ НОРМА) are in Russian too; in the other languages they stay in English. Kept in English
everywhere: technical identifiers (telemetry field names, SCPI, model names), log messages and these help pages.
Reports already written keep the language they were written in.

### What do I have to set first?
*Unit / signal*: carrier frequency, symbol rate, roll-off, nominal output power, nominal attenuator code and the
**cable loss** (otherwise power results are off by that much). *Limits*: the pass/fail limits. *BOS supply
output*: which HMP4040 / N6701C output powers the unit (for the supply-current test).

### What are the Corrections?
Values for the DAC38J82 QMC gain / phase / offset (from the measured gain imbalance, quadrature error and
carrier leakage), the frequency correction (reference trim or LMX2594), the attenuator steps to reach nominal
power and an attenuator calibration table. The LSB scaling factors are on *Unit / signal* — check them against
the datasheet. The offset sign can be swapped by the demodulator's 90° ambiguity: apply half, re-measure.

### Live diagnose
Captures and diagnoses continuously what the unit transmits now (pick OQPSK / BPSK / CW), without switching it.
Useful while adjusting the board or probing with a scope.

### Can I practise without a unit?
Start with `--demo`. The *Simulator* page injects faults into the simulated BOS (reference offset, LMX unlock,
JESD error, I/Q imbalance, LO leakage, PA compression, stuck attenuator bit, dead Q rail, wrong modulation, …).
*Random fault (blind test)* hides which one — run a plan and see whether you (and the diagnosis) find it.

### Can a test plan run from a sequence?
Yes: Main tab → *+ BOS test*. The step waits for the verdict and can stop the sequence on FAIL.

---

### Do I have to type the instrument addresses every time?
No. After every successful connection the address and its settings are written to `device_addresses.json` next to
the program (FSW: address, protocol, timeout; HMP4040 and N6701C: address; IT6832: COM port, baud rate, device
address, protocol, Arduino option, model; TFG6806: LAN / USB, IP, port, USB resource) and filled in at the next
start - like `can_lines.json` for the CAN lines. Demo connections are not saved. The address lists (HMP4040 address,
IT6832 COM port) also offer the last few addresses used. A `--ip` given on the command line still wins for the FSW.

### Can the program connect everything by itself at start-up?
Yes (on by default). About a second after the start, every instrument that has a saved connection in
`device_addresses.json` is connected (FSW, TFG6806, HMP4040, N6701C, IT6832), and the CAN lines ticked in
`can_lines.json` are opened - one after the other; the log says what is being connected, and a device that is
switched off only logs an error. Switch it off on the **Help** tab (*Connect the saved devices and CAN lines at
start-up*) or start the program with `--no-autoconnect`. Not done in `--demo`.

### Can I put a tab into its own window (second monitor)?
Yes. **Right-click a tab → Open in a separate window**, or double-click it. The page then lives in its own window
(e.g. *Main* on one screen, *Telemetry* or *BOS Test Station* on the other) and keeps working as before. *Back to
the main window* - or closing that window - puts it back at its old place; right-click a tab → *Bring all windows
back* does it for all. Which tabs were separate, and where their windows were, is remembered for the next start. Each
separate window has its own taskbar button and Alt-Tab entry.

### Why does every plan send SET_BPU_AFAR_mode?
After power-up the BOS starts no session until the АФАР БПУ set has been chosen. Every plan therefore sends
**SET_BPU_AFAR_mode** in standby after SET_Time_SNR (*Control link & BOS setup*), again after a КОНФИГ reload, and in
Live diagnose; if a plan has no link step, it is sent before the first session command. The set is chosen on
**Unit control → АФАР БПУ set**: БПУ1 (default), БПУ2, automatic selection, or *don't send*.

## 7. CAN and telemetry

### How are the CAN lines set up? (two per unit)
Every unit - BOS, AFAR, UPNG - has **two CAN lines, CAN1 and CAN2**, so the CAN tab → *CAN lines* has six rows:
**BOS CAN1 / BOS CAN2 / AFAR CAN1 / AFAR CAN2 / UPNG CAN1 / UPNG CAN2**, each with its own adapter / channel,
bitrate, listen-only. Defaults: BOS CAN1 = CANable, AFAR CAN1 = IXXAT channel 0, UPNG CAN1 = IXXAT channel 1 (as
before); the **CAN2 lines start unticked** - choose their adapter (second CANable, a second IXXAT, ...) and tick
them. *Connect* opens all ticked lines, each in its own thread, so all are captured at the same time; Trace /
Monitor show the line of every frame. An older setup (one line per unit) is taken over as the CAN1 lines, and a
command or setting that names only a unit ("BOS") goes to that unit's CAN1 line. Telemetry arrays are collected
per line: with both buses enabled the BOS sends its periodic arrays alternately on CAN1 and CAN2.

### How does the station use the two BOS lines?
Protocol control, first after the power-up (*CAN1 / CAN2 lines*, Unit control: *CAN line 1 / 2*): via **CAN1**
NOP with acknowledgement (КВ+ must come back on CAN1 with the "via CAN1" bit) and REQ_TM ИОК_ОСН (the array must
arrive on CAN1 within 3 s), then the same via **CAN2**. The BOS receives on both buses and answers on the bus the
request came from (ПИЛВ 4.2.3.6). After that the whole plan uses **CAN1 only**. Optional: *Then SET_CAN* sends
SET_CAN = 01 so the BOS also transmits only on CAN1 (standby only, not saved - power cycle or SET_CAN = 11 restores
both).

### Can I use a Marathon CAN-bus-USB adapter?
Yes: CAN tab → *CAN lines* → interface **Marathon CAN-bus-USB (CHAI driver)** on any line. Install Marathon's
driver package (CHAI: `chai.dll`, copied to the Windows system folder by the installer; on Linux `libchai.so`).
The channel list shows the detected adapters and their channels (CAN1 = channel 0, CAN2 = channel 1 for the first
adapter), so a 2-channel adapter can carry e.g. *BOS CAN1* and *BOS CAN2*. Bit rates 10 kbit/s ... 1 Mbit/s,
listen-only supported. It can be combined freely with the CANable and the IXXAT on the other lines.

**Connector:** the CAN-bus-USB has **both channels on one 9-pin D-sub** with non-standard pins - CAN1: L pin 2,
H pin 7, GND 3 / 6; CAN2: L pin 4, H pin 8, GND 5. Use Marathon's **branch cable** (supplied) to get two standard
CiA DS 102 connectors (L 2, H 7, GND 3 / 6). The **120 Ω terminators** are jumpers inside: P6 for CAN1, P7 for CAN2
(the device is supplied without termination).

**If the library is not found:** put the `chai.dll` matching the Python / program bitness (CHAI installation folder
`x64` for 64-bit, `x32` for 32-bit) next to the program / KTR_TEST.exe, or set `CHAI_LIB` to its full path.
CAN-bus-USBnp adapters with firmware ≤ 1.3 have no CiTransmit - the program then sends with CiWrite automatically.

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

### Where do the BOS commands come from? (bos_commands_0102.json)
`bos_commands_0102.json` holds every command of ПИЛВ 0102-004 part 1: tables 4.6 / 4.7 (49 codes to the BOS,
incl. the session commands 0xA0-0xA7, Set_TimeSend 0xAA, Chang_FlashTM 0xE0) and table 4.8 (commands the BOS
sends to the АФАР). For each command: code, mnemonic, data bytes, section, whether it runs only in standby (ДЖР),
receiver (BOS / group 0x0D), acknowledgement, and the data-field layout with allowed values (tables 4.9-4.35).

* **Station tests** build every data field from this file and check it against the document before sending (a
  prohibited value would make the BOS silently ignore the command — the test stops with the reason instead).
* **Command set (0102)** plan / test: in standby, every preset marked `"test": true` is sent and must be answered
  as the document says (КВ+, or КВ- for the reserved codes 0x05 / 0x07); then *SET_CONFIG_PROM user* reloads the
  stored КОНФИГ, the station's own link setup (Time_SNR, telemetry periods, SET_CAN) is applied again and
  ИОК_КОНФИГ must equal the one read before. EPROM / flash writers (SAVE_CONFIG_NEW, CLR_Flash_Cmd_SNR) and
  TurnOff_BPU_AFAR are never sent automatically.
* **CAN tab**: *+ BOS 0102…* adds the presets as library commands for the chosen sender and line (duplicates
  skipped). They can then be sent, given hotkeys or used in scripts.
* **Command line**: `python bos_protocol.py --list` prints every preset frame;
  `python bos_protocol.py SET_Temp_Max temp=-5 sensor=2` encodes one command;
  `python bos_protocol.py --export lib.json` writes a can_commands.json-style file.

Add your own presets by editing the `presets` list of a command (`"params"` use the field names; add
`"test": true` to include it in the Command set test). A copy next to `KTR_TEST.exe` overrides the built-in one.
Assumptions to confirm against the full document: the broadcast address of *Time_KA* (sent to the BOS address
without acknowledgement) and the data layout of *Set_TimeSend* (entered as a raw value).

### Telemetry shows nothing.
Telemetry is decoded from the CAN frames of BOS (`0x1A46…`), UPNG (`0x1A86…`) and AFAR (`0x1AC6…`).
Check that the line carrying the unit is connected and frames arrive (Monitor view).

---

## 8. Main tab and test sequences

### Which steps can a sequence contain?
HMP outputs, N6701C outputs, CAN command, Pause, **BOS test** (a BOS Test Station plan, waits for the
verdict), FSW (run / stop / single / auto level), FSW settings, **TFG** (channel(s), any parameters,
trigger / align / recall), Repeat … End, Log message.

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
