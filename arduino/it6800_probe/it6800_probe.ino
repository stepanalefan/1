/*
  it6800_probe.ino  -  stand-alone diagnosis of the ITECH IT6800 (IT6832) TTL serial port

  Runs on its own (no PC program): open the Serial Monitor at 115200 baud, line ending "Newline".
  Wiring (same as the bridge, measured on an IT6832):
      DB9 pin 3 (supply TX) -> Arduino pin 10
      DB9 pin 2 (supply RX) <- Arduino pin 11
      DB9 pin 5 (GND)       -- Arduino GND
  Pins 10 / 11 with SoftwareSerial on every board (Uno, Nano, Mega 2560, Leonardo, Micro).

  What it does
    1. Idle level of the supply's TX line (pin 10): must be HIGH (about 5 V).
    2. Scan: baud rates 9600, 19200, 38400, 4800 x addresses 0...30: sends "remote control" (20h) and shows
       - whether the supply's TX line moved at all (activity),
       - every byte received (hex),
       - a valid 26-byte reply (AAh ... checksum) -> FOUND.
    3. On FOUND: model / version / serial (31h), then voltage, current, state (26h) every 2 s.
  Serial Monitor commands:  s = scan again,  r = read 26h,  i = identify 31h,  l = local (front panel),
                            t = pin test (jumper pin 10 to pin 11, supply disconnected)
*/

#include <SoftwareSerial.h>

const uint8_t PIN_RX = 10, PIN_TX = 11;
SoftwareSerial psu(PIN_RX, PIN_TX);
const long BAUDS[] = {9600, 19200, 38400, 4800};
long foundBaud = 0;
int foundAddr = -1;
unsigned long nextRead = 0;

void hex2(uint8_t b) {
  if (b < 16) Serial.print('0');
  Serial.print(b, HEX);
}

void printBytes(const uint8_t *b, int n) {
  for (int k = 0; k < n; k++) {
    hex2(b[k]);
    Serial.print(' ');
  }
}

void makeFrame(uint8_t *f, uint8_t addr, uint8_t cmd, const uint8_t *data, uint8_t n) {
  memset(f, 0, 26);
  f[0] = 0xAA;
  f[1] = addr;
  f[2] = cmd;
  for (uint8_t k = 0; k < n && k < 22; k++) f[3 + k] = data[k];
  uint8_t s = 0;
  for (uint8_t k = 0; k < 25; k++) s += f[k];
  f[25] = s;
}

bool validFrame(const uint8_t *f, int n) {
  if (n < 26 || f[0] != 0xAA) return false;
  uint8_t s = 0;
  for (uint8_t k = 0; k < 25; k++) s += f[k];
  return s == f[25];
}

// sends a frame, returns number of bytes received within ms; activity = TX line went LOW at least once
int transact(uint8_t addr, uint8_t cmd, const uint8_t *data, uint8_t n, uint8_t *rx, unsigned ms, bool *activity) {
  uint8_t f[26];
  makeFrame(f, addr, cmd, data, n);
  psu.listen();
  while (psu.available()) psu.read();
  psu.write(f, 26);
  int got = 0;
  bool act = false;
  unsigned long t0 = millis();
  while (millis() - t0 < ms) {
    if (digitalRead(PIN_RX) == LOW) act = true;
    while (psu.available() && got < 64) {
      rx[got++] = psu.read();
      act = true;
    }
    if (got >= 26) break;
  }
  if (activity) *activity = act;
  return got;
}

void scan() {
  Serial.println(F("\n--- scan: baud x address, command 20h (remote) ---"));
  pinMode(PIN_RX, INPUT);
  Serial.print(F("Idle level of the supply TX (pin 10): "));
  Serial.println(digitalRead(PIN_RX) == HIGH ? F("HIGH (ok)") : F("LOW  (!) - check wiring / GND / supply on"));
  foundBaud = 0;
  foundAddr = -1;
  uint8_t rx[64];
  const uint8_t on = 1;
  for (uint8_t b = 0; b < 4; b++) {
    psu.begin(BAUDS[b]);
    bool anyAct = false;
    for (int a = 0; a <= 30; a++) {
      bool act;
      int n = transact(a, 0x20, &on, 1, rx, 250, &act);
      anyAct |= act;
      if (n > 0) {
        Serial.print(F("baud "));
        Serial.print(BAUDS[b]);
        Serial.print(F(" addr "));
        Serial.print(a);
        Serial.print(F(": "));
        Serial.print(n);
        Serial.print(F(" bytes: "));
        printBytes(rx, n);
        Serial.println();
      }
      if (validFrame(rx, n)) {
        foundBaud = BAUDS[b];
        foundAddr = a;
        Serial.print(F(">>> FOUND: baud "));
        Serial.print(foundBaud);
        Serial.print(F(", address "));
        Serial.print(foundAddr);
        Serial.print(F(", reply command "));
        hex2(rx[2]);
        Serial.print(F("h, result "));
        hex2(rx[3]);
        Serial.println(rx[3] == 0x80 ? F("h (ok)") : F("h"));
        return;
      }
    }
    Serial.print(F("baud "));
    Serial.print(BAUDS[b]);
    Serial.println(anyAct ? F(": the supply TX line moved, but no valid frame")
                          : F(": no activity on the supply TX line"));
  }
  Serial.println(F("NOT FOUND. Check: supply on; pin 3 -> 10, pin 2 <- 11, pin 5 - GND; baud / address"
                   " in the supply menu; try 't' (loopback) to test the Arduino side."));
}

void identify() {
  uint8_t rx[64];
  int n = transact(foundAddr, 0x31, NULL, 0, rx, 400, NULL);
  if (!validFrame(rx, n)) {
    Serial.print(F("31h: no valid reply ("));
    Serial.print(n);
    Serial.println(F(" bytes)"));
    return;
  }
  Serial.print(F("Model: "));
  for (uint8_t k = 3; k < 8 && rx[k]; k++) Serial.write(rx[k]);
  Serial.print(F("   version V"));
  Serial.print(rx[9], HEX);
  Serial.print('.');
  hex2(rx[8]);
  Serial.print(F("   serial: "));
  for (uint8_t k = 10; k < 20 && rx[k]; k++) Serial.write(rx[k]);
  Serial.println();
}

void readState() {
  uint8_t rx[64];
  int n = transact(foundAddr, 0x26, NULL, 0, rx, 400, NULL);
  if (!validFrame(rx, n)) {
    Serial.print(F("26h: no valid reply ("));
    Serial.print(n);
    Serial.print(F(" bytes) "));
    printBytes(rx, n);
    Serial.println();
    return;
  }
  uint16_t i = rx[3] | (rx[4] << 8);
  uint32_t v = (uint32_t)rx[5] | ((uint32_t)rx[6] << 8) | ((uint32_t)rx[7] << 16) | ((uint32_t)rx[8] << 24);
  uint8_t st = rx[9];
  uint16_t iset = rx[10] | (rx[11] << 8);
  uint32_t vset = (uint32_t)rx[16] | ((uint32_t)rx[17] << 8) | ((uint32_t)rx[18] << 16) | ((uint32_t)rx[19] << 24);
  Serial.print(F("U = "));
  Serial.print(v / 1000.0, 3);
  Serial.print(F(" V   I = "));
  Serial.print(i / 1000.0, 3);
  Serial.print(F(" A   output "));
  Serial.print(st & 1 ? F("ON") : F("OFF"));
  uint8_t m = (st >> 2) & 3;
  Serial.print(m == 1 ? F(" CV") : m == 2 ? F(" CC") : m == 3 ? F(" UNREG") : F(""));
  Serial.print(st & 0x80 ? F("  remote") : F("  local"));
  Serial.print(F("   set "));
  Serial.print(vset / 1000.0, 3);
  Serial.print(F(" V / "));
  Serial.print(iset / 1000.0, 3);
  Serial.println(F(" A"));
}

void loopback() {
  // SoftwareSerial cannot receive its own transmission, so this checks the pins at logic level:
  // pin 11 drives LOW / HIGH, pin 10 must follow (jumper 10-11, supply disconnected).
  Serial.println(F("\n--- loopback: jumper pin 10 to pin 11, supply disconnected ---"));
  psu.end();
  pinMode(PIN_TX, OUTPUT);
  pinMode(PIN_RX, INPUT);
  digitalWrite(PIN_TX, LOW);
  delay(2);
  bool lowOk = digitalRead(PIN_RX) == LOW;
  digitalWrite(PIN_TX, HIGH);
  delay(2);
  bool highOk = digitalRead(PIN_RX) == HIGH;
  Serial.print(F("pin 11 LOW -> pin 10 "));
  Serial.println(lowOk ? F("LOW (ok)") : F("HIGH (!)"));
  Serial.print(F("pin 11 HIGH -> pin 10 "));
  Serial.println(highOk ? F("HIGH (ok)") : F("LOW (!)"));
  Serial.println(lowOk && highOk ? F("Arduino pins OK") : F("loopback failed: jumper / pins?"));
  psu.begin(foundBaud ? foundBaud : 9600);
}

void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 3000) {}   // Leonardo / Micro: wait for the Serial Monitor
  Serial.println(F("ITECH IT6800 probe - commands: s scan, r read, i identify, l local, t loopback"));
  scan();
  if (foundAddr >= 0) {
    psu.begin(foundBaud);
    identify();
  }
}

void loop() {
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 's') {
      scan();
      if (foundAddr >= 0) identify();
    } else if (c == 'r' && foundAddr >= 0) {
      readState();
    } else if (c == 'i' && foundAddr >= 0) {
      identify();
    } else if (c == 'l' && foundAddr >= 0) {
      const uint8_t off = 0;
      uint8_t rx[64];
      transact(foundAddr, 0x20, &off, 1, rx, 300, NULL);
      Serial.println(F("front panel (local) mode"));
      foundAddr = -1;
    } else if (c == 't') {
      loopback();
    }
  }
  if (foundAddr >= 0 && millis() >= nextRead) {
    nextRead = millis() + 2000;
    readState();
  }
}
