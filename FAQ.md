# KTR TEST — FAQ

Automated test station for the **BOS** modulator unit (BPSK / OQPSK; MPF300T FPGA, DAC38J82, LMX2594,
ADMV1013, ADRF5730, CHA3397): R&S FSW signal analyzer, BOS CAN control and telemetry, automatic
fault diagnosis with reports, plus R&S HMP4040 / Keysight N6701C power supplies, Suin TFG6806
generator, three CAN lines (CANable + IXXAT), test sequences and CSV logging.

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
Yes: `--demo`. FSW, TFG6806, HMP4040, N6701C, three CAN lines (BOS / AFAR / UPNG) and a complete
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

## 5. Power supplies (HMP4040, N6701C) and VISA

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

### What does the power watchdog do?
Main tab → *Power watchdog* → *Conditions…*: switch the HMP4040 master and/or all N6701C outputs OFF and stop
the sequence when a step fails, a supply stops communicating, an output stays in current limit (CC) longer
than N s (short inrush is ignored), an N6701C protection trips, or a current exceeds N A. Monitor only during
sequences or always. A trigger stays latched (red) until *Reset* or the next run and is written to
`watchdog_log_YYYY-MM-DD.csv`.

### The TFG6806 waveform display
Drawn from the channel's settings (the generator has no command to read its screen), like the generator's own
display: very fast carriers in modulation / sweep / burst are drawn with fewer cycles ("not to scale").

## 6. BOS Test Station

### What does it do?
It tests a BOS modulator automatically and tells the operator **what is wrong and where to look**. A test plan
switches the unit through its modes over CAN (RF off, carrier, BPSK, OQPSK, attenuator codes), captures the output
with the FSW (raw I/Q from the I/Q Analyzer — K70 is not needed), reads the BOS telemetry (PLL locks, JESD,
FPGA rails, temperatures, 10 MHz reference) and analyses everything. Results: a verdict (PASS / PASS with
warnings / FAIL), a measurement table with limits, **findings** with the most likely faulty block, a block
diagram of the modulator coloured by suspicion, suggested **corrections**, an HTML report and a history.

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
If the BOS answers КВ-, the sender address may not be allowed for that command: try ВМ 1 (0x20) instead of
КПА ИВУ (0x18). The alternatives *CAN library* and *Ask the operator* remain for special set-ups.

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
