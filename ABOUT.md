# FSW Monitor

**Test bench control and monitoring software**

| Field | Value |
|---|---|
| Version | 1.0 (2026-10) |
| Organization | *Your organization name* |
| Department | *Department / laboratory* |
| Author / inventor | *Name Surname* |
| Contact | *name@example.com* |
| Developed with | Claude Code (Anthropic) |

## What it does
Controls and monitors the test bench: R&S FSW spectrum analyzer, R&S SMW200A, Keysight N5183B and
Suin TFG6806 generators, R&S HMP4040 power supply, 176-channel voltage scanner (Arduino +
multiplexers + Agilent 34405A), 1-Wire temperature sensors, switch outputs with BOS watchdog,
three CAN lines (CANable + IXXAT) with telemetry decoding, test sequences and daily CSV logs.

## Built with
Python, PyQt5, pyqtgraph, NumPy, PyVISA, pyserial, python-can; Arduino (OneWire library).

## Documentation
See the **Readme** and **FAQ** tabs.

---
*Edit `ABOUT.md` next to the program to change this page — press **Reload** to see the changes.*
