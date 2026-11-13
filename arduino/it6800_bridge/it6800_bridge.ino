/*
  it6800_bridge.ino  -  Arduino as USB-to-TTL serial bridge for the ITECH IT6800-series supply (IT6832)

  The rear DB9 of the IT6800 is a TTL-level serial port (not RS-232, never connect it to a PC RS-232 port).
  This sketch passes every byte unchanged in both directions (binary-safe), so the KTR TEST program can
  use the frame protocol (26-byte binary frames) or SCPI text through it.

  Serial settings on the supply (front panel menu): baud rate 4800 / 9600 / 19200 / 38400 (default 9600),
  8 data bits, no parity, 1 stop bit, communication address (0...254). Set the same baud rate and address
  on the IT6832 tab of the program.

  Boards
  ------
  * Leonardo, Micro, Pro Micro (ATmega32U4), Mega 2560, Nano Every, Uno R4:  hardware Serial1
      pin RX1 <- supply TX,   pin TX1 -> supply RX     (Mega: RX1 = 19, TX1 = 18; Leonardo / Micro: RX = 0, TX = 1)
    ATmega32U4 boards (Leonardo, Micro, Pro Micro) follow the baud rate the PC opens the port with, so the
    "Baud rate" on the IT6832 tab also sets the supply side; on the others PSU_BAUD below is used.
  * Uno R3, Nano, Pro Mini (ATmega328P): the only hardware UART is used by USB, so SoftwareSerial is used:
      pin 10 (RX) <- supply TX,   pin 11 (TX) -> supply RX
    The baud rate is fixed by PSU_BAUD below (both sides); 9600 or 19200 recommended with SoftwareSerial.
    These boards reset when the port is opened: keep "Arduino bridge (wait 2 s)" ticked on the IT6832 tab.

  Wiring
  ------
    supply TX  ->  Arduino RX (see above)
    supply RX  <-  Arduino TX
    supply GND --  Arduino GND
  Do NOT connect any supply-side supply voltage pin to the Arduino. The supply's TTL port uses 5 V levels:
  use a 5 V Arduino (Uno, Nano, Mega, Leonardo, Micro). With a 3.3 V board (Uno R4 is 5 V; Due, Zero,
  ESP32 are 3.3 V) put a level shifter in between.
  Finding the pins on the DB9: with the supply on and nothing connected, the supply's TX pin idles HIGH
  (about +5 V against the GND pin); its RX pin is an input. Check the pinout in the IT6800 user manual
  ("communication interface") or measure before connecting.

  Polarity
  --------
  Some ITECH TTL ports use 5 V levels with RS-232 polarity (inverted: idle LOW, start bit HIGH), see
  forum.arduino.cc "Controlling ITECH power supply with TTL logic" (IT6302). Check with a meter, supply on,
  nothing connected: the supply's TX pin idles at about +5 V = normal TTL, about 0 V = inverted.
  For an inverted port set INVERT_TTL to 1 below: every board then uses SoftwareSerial with inverted logic
  on pin 10 (RX) / pin 11 (TX) (Uno, Nano, Mega 2560, Leonardo, Micro: pin 10 supports pin-change
  interrupts on all of them); keep 9600 or 19200 baud.

  LED (pin 13 / LED_BUILTIN) flashes on traffic.
*/

#define INVERT_TTL 0                   // 1 = the supply's TTL port has RS-232 polarity (idle LOW)

#if !INVERT_TTL && (defined(HAVE_HWSERIAL1) || defined(USBCON) || defined(ARDUINO_ARCH_MEGAAVR) || defined(ARDUINO_ARCH_RENESAS))
  #define PSU Serial1
  #define HW_UART 1
#else
  #include <SoftwareSerial.h>
  SoftwareSerial PSU(10, 11, INVERT_TTL ? true : false);   // RX, TX, inverse logic
  #define HW_UART 0
#endif

const unsigned long PSU_BAUD = 9600;   // must match the supply's front panel setting
const unsigned long LED_MS = 30;

unsigned long ledOff = 0;
unsigned long curBaud = PSU_BAUD;

static bool validBaud(unsigned long b) {
  return b == 4800 || b == 9600 || b == 19200 || b == 38400;
}

static void blink() {
  digitalWrite(LED_BUILTIN, HIGH);
  ledOff = millis() + LED_MS;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(PSU_BAUD);              // USB side (ignored by native-USB boards)
  PSU.begin(PSU_BAUD);
#if !HW_UART
  PSU.listen();
#endif
}

void loop() {
#if defined(USBCON)
  // ATmega32U4 native USB: use the baud rate the PC opened the port with (only the supply's rates)
  unsigned long hb = Serial.baud();
  if (hb != curBaud && validBaud(hb)) {
    PSU.end();
    PSU.begin(hb);
    curBaud = hb;
  }
#endif
  uint8_t buf[64];
  // PC -> supply
  int n = Serial.available();
  if (n > 0) {
    if (n > (int)sizeof(buf)) n = sizeof(buf);
    n = Serial.readBytes(buf, n);
    PSU.write(buf, n);
    blink();
  }
  // supply -> PC
  n = PSU.available();
  if (n > 0) {
    if (n > (int)sizeof(buf)) n = sizeof(buf);
    for (int k = 0; k < n; k++) buf[k] = PSU.read();
    Serial.write(buf, n);
    blink();
  }
  if (ledOff && (long)(millis() - ledOff) >= 0) {
    digitalWrite(LED_BUILTIN, LOW);
    ledOff = 0;
  }
}
