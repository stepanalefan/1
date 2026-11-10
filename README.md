# KTR RAD TEST — BOS Test Station

Automated test station for the **BOS** modulator unit (BPSK / OQPSK). It controls the unit over CAN,
measures its output with an R&S FSW, reads the BOS telemetry, and tells the operator **whether the unit is
good, what is wrong and where to look** — down to the block of the modulator chain:

```
REF 10 MHz ─► clock PLLs ─► FPGA MPF300T ══JESD204B══► DAC38J82 ─► ADMV1013 (I/Q, LO ×4) ─► ADRF5730 ─► CHA3397 ─► FSW
                    └───────────────► LMX2594 (LO) ─────────┘
```

Also on board: R&S HMP4040 and Keysight N6701C supplies, Suin TFG6806 generator, three CAN lines with
telemetry decoding, test sequences, daily CSV logs.

```
pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial python-can
python fsw_vsa_monitor.py            # start
python fsw_vsa_monitor.py --demo     # everything simulated, incl. a BOS with injectable faults
```
USB instruments need **Keysight IO Libraries Suite**, the IXXAT adapter the **IXXAT VCI 4** driver.
Questions and troubleshooting: see **[FAQ.md](FAQ.md)**.

---

## BOS Test Station (first tab)

**Workflow:** enter the unit serial number and operator → pick a test plan → **▶ Run test plan** → read the
verdict, the findings and the report.

| Area | What it shows / does |
|---|---|
| Top bar | Unit S/N, operator, test plan (*Plans ▾* edit / new / delete), Run / Abort, **Live diagnose** (continuous capture + diagnosis of what the unit sends now, without switching it), progress, verdict **PASS / PASS (warnings) / FAIL** |
| **Diagnosis** | Block diagram of the modulator, each block coloured by how suspicious it is; list of findings (fault / warning / info) with explanation, most likely location (weighted) and *what to do*. Click a block to see only its findings. |
| **Measurements** | Every measured value with its limit and PASS / FAIL |
| **Signal** | For each capture: constellation, spectrum, phase error per symbol, all numbers (MER, EVM, frequency / clock error, carrier leakage, gain imbalance, quadrature error, image rejection, phase / magnitude error, MER after I/Q correction, PAPR loss, OBW, ACLR, asymmetry, spurs) |
| **Attenuator** | Output power vs. ADRF5730 code (measured vs. nominal) and the fitted weight of each bit |
| **BOS telemetry** | Live: 6 PLL / JESD flags, FPGA rails 1 V / 1.8 V / 2.5 V, temperatures, 10 MHz reference, Err_Ref_LO, mode, firmware |
| **Corrections** | Suggested DAC38J82 QMC gain / phase / offset, frequency correction (reference trim or LMX2594), attenuator steps to nominal power, attenuator calibration |
| **History** | All runs of all units (`bos_reports/bos_history.csv`), filter, open report, most frequent suspect blocks |
| Left: *Unit / signal* | Carrier, symbol rates (BPSK = УР: 2 × data rate; OQPSK = ШР: always 131.072 Msym/s — set automatically with protocol control), samples per symbol per modulation, roll-off, capture length, LO multiplier, nominal power and attenuator code, **cable loss**, FSW auto level, phase-noise band, stability and linearity settings, QMC scaling, supply output feeding the BOS |
| Left: *Limits* | All pass / fail limits |
| Left: *Unit control* | **BOS protocol** (default): the station sends the BOS commands itself (ПИЛВ 0102-004): sender address, CAN line, frequency code N (37650 + 0.125·N MHz), which session command gives BPSK / OQPSK, data rate, session limit, target-designation keep-alive, telemetry enable; *Test link*, *RF off*. Alternatives: CAN library commands or operator prompts. |
| Left: *Simulator* | Demo only: inject faults into the simulated BOS; *Random fault (blind test)* for training |

### Tests

| Test | Measurement | Typical faults found |
|---|---|---|
| Control link & BOS setup | (protocol control, added automatically) NOP and MODE_D acknowledged, SET_Time_SNR, ИОК periods, ИОК_КОНФИГ read back and CRC-checked | no CAN link, КВ- (sender not allowed), corrupt / unexpected stored configuration |
| Pre-check | BOS telemetry: PLLs (LMK, DAC clock, LMX2594, FPGA JESD, FPGA), JESD link, rails, temperatures, 10 MHz, Err_Ref_LO | unlocked PLL, JESD error, bad rail, overheating, missing reference |
| Supply current | bench supply current RF off → RF on | PA not biased / not enabled, short |
| Carrier (CW) | frequency (Hz, ppm), power, integrated phase noise, spurs | reference offset, LO programming error, noisy LO |
| BPSK, OQPSK | full modulation analysis (see *Signal*), detected modulation | I/Q imbalance, LO leakage, dead Q rail, wrong mode, PA compression, clock error |
| Attenuator | power at 11 Atten_EIRP settings (0…20 dB) → least-squares weight of the 1/2/4/8/16 dB steps | stuck / missing / wrong-weight step |
| PA linearity | compression and ACLR at nominal and +6 dB Atten_EIRP | PA compression vs. digital clipping |
| LO harmonics | lines at carrier × k/4 | ADMV1013 LO / multiplier leakage |
| Stability | power, frequency, MER over time with temperature | thermal drift |

Default plans: *Full test*, *Quick check*, *Modulation only*, *Attenuator*, *Burn-in (stability)*.

### How the diagnosis reasons
Measurements that point at different blocks are cross-checked: equal ppm on carrier and symbol rate → the
common reference; carrier error with a correct clock → LO (LMX2594); clock error with a correct carrier →
DAC/FPGA clock; BPSK good but OQPSK bad → Q rail; phase error without magnitude error → LO phase noise;
PAPR loss that disappears with more attenuation → PA; power low with a verified attenuator → PA / cable;
telemetry flags (PLL, JESD, rails) take precedence. Findings rank where to look first, they are not proof.

The FSW is used through its **I/Q Analyzer** (raw I/Q, demodulated by the program), so the station works with
or without FSW-K70. Each report (`bos_reports/BOS_<serial>_<date>.html`) contains the verdict, findings, block
diagram, all measurements, corrections, telemetry and plots.

---

## Other tabs

| Tab | What it does |
|---|---|
| **Main** | Device status (FSW, HMP4040, N6701C, TFG6806, CAN, BOS), supply outputs, BOS unit summary, power watchdog, **test sequence** editor/runner, FSW constellation with MER / power / frequency error, telemetry watch list |
| **Spectrum Analyzer** | R&S FSW: **VSA (K70)**, **Spectrum**, **I/Q (software demod)** (BPSK, QPSK, OQPSK, 8PSK, 16QAM without K70) |
| **TFG6806** | Suin TFG6806, both channels with waveform display; LAN or USB |
| **HMP4040** | R&S HMP4040 (USB or LAN): 4 channels, master output, power cycling, plot, daily `hmp_log` |
| **N6701C** | Keysight N6701C (LAN / USB / GPIB): per output V, I, OVP, OCP, on/off, plot, daily `n6700_log` |
| **CAN** | Three CAN lines (BOS = CANable, AFAR = IXXAT CAN1, UPNG = IXXAT CAN2): trace, monitor, transmit, command library, scripts, record / open candump logs |
| **Telemetry** | Decoded arrays of BOS, UPNG and AFAR with the meaning of every field |
| **Logs** | Any CSV logs of the program on one time plot |
| **Help** | FAQ / Readme / About |

## Test sequence steps (Main tab)

| Button | Step |
|---|---|
| **+ HMP** / **+ N6701C** | Set supply outputs (V, I, on/off), master / all outputs, clear protection |
| **+ CAN** | Send a command from the CAN library |
| **+ BOS test** | Run a BOS Test Station plan, wait for the verdict, optionally stop on FAIL |
| **+ FSW** / **+ FSW set** | FSW run / stop / single / auto level; set VSA / Spectrum / I/Q parameters |
| **+ TFG** | TFG6806 parameters, trigger / align / recall |
| **+ Pause**, **+ Repeat**, **+ Log** | Wait, repeat N times (0 = forever) … End, log message |

**Power watchdog** (Main tab): switches the HMP4040 master and/or all N6701C outputs OFF and stops the
sequence on a failed step, lost supply communication, long current limit, N6701C protection or over-current.

---

## Command line

```
python fsw_vsa_monitor.py [--ip ADDR] [--protocol hislip|vxi11|socket] [--connect] [--demo] [--demo-no-k70]
```
The CAN sniffer can also run alone: `python canable_analyzer.py [--demo] [--interface slcan|gs_usb|ixxat|socketcan|demo] [--port COM7] [--bitrate 500000] [--connect] [--log file.log]`.

CAN ID filter syntax: `100-1FF` range · `7E8` single · `!7DF` exclude · `18FEF100` 29-bit · `700/700` id/mask.

---

## Files

| File | Part |
|---|---|
| `fsw_vsa_monitor.py` | **Start file**: main window, Spectrum Analyzer |
| `bos_station.py` | BOS Test Station tab: plans, runner, unit control, views, reports, history |
| `bos_analysis.py` | Measurement analysis, diagnosis rules, corrections, BOS simulator (pure NumPy) |
| `bos_protocol.py` | BOS CAN protocol: identifiers, command codes and parameters, acknowledgements, CRC-16, КОНФИГ decoding |
| `iq_demod.py` | Software demodulator (BPSK / QPSK / OQPSK / 8PSK / 16QAM) |
| `dashboard.py` | Main tab, sequences |
| `bench_monitor.py` | HMP4040 tab, VISA / serial / socket helpers |
| `n6700_supply.py`, `tfg_generator.py` | N6701C and TFG6806 tabs |
| `canable_analyzer.py`, `can_commands.py` | CAN tab |
| `telemetry.py`, `log_viewer.py`, `help_tab.py` | Telemetry, Logs and Help tabs |
| `build_exe.bat`, `rthook_visa.py` | Build `dist\KTR_RAD_TEST.exe` |

Settings (`bos_station.json`, `dashboard.json`, `can_commands.json`, `can_lines.json`), reports
(`bos_reports/`) and daily logs (`*_log_YYYY-MM-DD.csv`) are kept next to the program.
