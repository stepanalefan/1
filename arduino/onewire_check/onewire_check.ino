// Minimal 1-Wire check - independent of the bench program.
// Upload to the slave Arduino, open Serial Monitor at 115200. Every 2 s it prints,
// for A3, A4, A5: the idle line level, whether any device answers the reset
// (presence pulse), and the ROM code + CRC check of every device found.
// Needs the OneWire library (Paul Stoffregen).
#include <OneWire.h>

const byte PINS[] = {A3, A4, A5};
const char *NAMES[] = {"A3", "A4", "A5"};

void setup() {
  Serial.begin(115200);
  Serial.println("1-Wire check started");
}

void loop() {
  for (byte b = 0; b < 3; b++) {
    OneWire ow(PINS[b]);
    pinMode(PINS[b], INPUT);
    delay(2);
    Serial.print(NAMES[b]);
    Serial.print(": idle level ");
    Serial.print(digitalRead(PINS[b]) ? "HIGH" : "LOW");
    byte presence = ow.reset();
    Serial.print(", presence ");
    Serial.println(presence ? "YES" : "no");
    if (!presence) continue;
    byte addr[8];
    int n = 0;
    ow.reset_search();
    while (ow.search(addr)) {
      n++;
      Serial.print("   ROM ");
      for (byte i = 0; i < 8; i++) {
        if (addr[i] < 16) Serial.print('0');
        Serial.print(addr[i], HEX);
      }
      Serial.println(OneWire::crc8(addr, 7) == addr[7] ? "  CRC ok" : "  CRC BAD");
    }
    if (!n) Serial.println("   presence seen, but search found no ROM (signal quality)");
  }
  Serial.println();
  delay(2000);
}
