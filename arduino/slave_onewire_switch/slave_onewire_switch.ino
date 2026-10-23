// =========================================================================
// ВЕДОМАЯ (SLAVE) ARDUINO: датчики температуры 1-Wire + ключи
// Связь с ведущей (master) Arduino по программному последовательному порту:
//   D2 (RX slave) <- A5 (TX мастера)
//   D3 (TX slave) -> A4 (RX мастера)
//   GND slave     -- GND мастера (обязательно!)
// USB этой платы свободен: можно заливать скетч и проверять команды
// в мониторе порта (115200, конец строки "NL") - ответ уходит туда, откуда пришла команда.
//
// Нужна библиотека OneWire (Paul Stoffregen):
// Скетч -> Подключить библиотеку -> Управлять библиотеками -> "OneWire" -> Установить
//
// Команды (каждая - одна строка):
//   OW:CONV          запустить преобразование на всех шинах -> "OW:CONV:OK:<маска шин>" / "OW:CONV:NODEV"
//   OW:READ          прочитать все датчики -> "OW:DEV:<шина>:<ROM>:<scratchpad>:<питание>" ..., "OW:END:<n>"
//   SW:SET:<i>:<0|1> выключить / включить ключ i (0 = A0, 1 = A1)       -> "SW:STATE:<состояния>"
//   SW:PULSE:<i>:<мс> включить ключ i на заданное время (импульс)        -> "SW:STATE:<состояния>"
//   SW:STATE?        текущее состояние ключей                             -> "SW:STATE:<состояния>"
//   Состояния - по символу на ключ: "10" = A0 включён, A1 выключен.
// =========================================================================
#include <OneWire.h>
#include <SoftwareSerial.h>

SoftwareSerial link(2, 3);      // RX, TX - к мастеру
const long LINK_BAUD = 19200;   // должна совпадать с SLAVE_BAUD в скетче мастера

// ---- Ключи ---------------------------------------------------------------
const byte SW_PINS[] = {A0, A1};
const byte SW_COUNT = sizeof(SW_PINS);
// true  - ключ активен уровнем LOW (типично для модулей реле "low level trigger")
// false - ключ активен уровнем HIGH (+5 В)
const bool SW_ACTIVE_LOW = false;
bool swState[SW_COUNT];
unsigned long pulseEnd[SW_COUNT];   // 0 = импульса нет

// ---- 1-Wire: три шины, подтяжка 4,7 кОм к +5 В на каждой, паразитное питание
OneWire bus1(A3);
OneWire bus2(A4);
OneWire bus3(A5);
OneWire *buses[] = {&bus1, &bus2, &bus3};
const byte BUS_COUNT = 3;
unsigned long convStart = 0;
bool convPending = false;

void swWrite(byte i, bool on) {
  swState[i] = on;
  digitalWrite(SW_PINS[i], (on != SW_ACTIVE_LOW) ? HIGH : LOW);
}

void setup() {
  // Ключи - первыми, чтобы сразу после сброса они были выключены
  for (byte i = 0; i < SW_COUNT; i++) {
    swWrite(i, false);              // сначала уровень, потом режим выхода - без "дёргания"
    pinMode(SW_PINS[i], OUTPUT);
    pulseEnd[i] = 0;
  }
  Serial.begin(115200);
  Serial.setTimeout(50);
  link.begin(LINK_BAUD);
  link.setTimeout(50);
}

// Окончание импульсов. Вызывается постоянно, в том числе во время ожидания 1-Wire.
void servicePulses() {
  unsigned long now = millis();
  for (byte i = 0; i < SW_COUNT; i++) {
    if (pulseEnd[i] && (long)(now - pulseEnd[i]) >= 0) {
      pulseEnd[i] = 0;
      swWrite(i, false);
    }
  }
}

void loop() {
  servicePulses();
  if (link.available() > 0) handle(link);
  if (Serial.available() > 0) handle(Serial);
}

void handle(Stream &port) {
  String cmd = port.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "OW:CONV") {
    owConvert(port);
  } else if (cmd == "OW:READ") {
    owRead(port);
  } else if (cmd == "SW:STATE?") {
    printState(port);
  } else if (cmd.startsWith("SW:SET:") || cmd.startsWith("SW:PULSE:")) {
    bool pulse = cmd.startsWith("SW:PULSE:");
    String args = cmd.substring(pulse ? 9 : 7);          // "<i>:<value>"
    int colon = args.indexOf(':');
    int i = args.substring(0, colon).toInt();
    long value = args.substring(colon + 1).toInt();
    if (colon < 1 || i < 0 || i >= SW_COUNT || value < 0 || (pulse && (value < 1 || value > 600000L))) {
      port.println("ERR:SW_BAD_ARGS");
      return;
    }
    if (pulse) {
      swWrite(i, true);
      pulseEnd[i] = millis() + value;
      if (pulseEnd[i] == 0) pulseEnd[i] = 1;
    } else {
      pulseEnd[i] = 0;
      swWrite(i, value != 0);
    }
    printState(port);
  } else {
    port.println("ERR:UNKNOWN_CMD");
  }
}

void printState(Stream &port) {
  port.print("SW:STATE:");
  for (byte i = 0; i < SW_COUNT; i++) port.print(swState[i] ? '1' : '0');
  port.println();
}

void printHex(Stream &port, byte b) {
  if (b < 16) port.print('0');
  port.print(b, HEX);
}

// =========================================================================
// 1-Wire: запуск преобразования на всех шинах (Skip ROM + Convert T)
// =========================================================================
void owConvert(Stream &port) {
  byte mask = 0;
  for (byte b = 0; b < BUS_COUNT; b++) {
    if (buses[b]->reset()) {
      buses[b]->skip();
      // power = 1: пин остаётся в HIGH ("сильная" подтяжка) - питание датчиков
      // на время преобразования при паразитном питании
      buses[b]->write(0x44, 1);
      mask |= 1 << b;
    }
  }
  if (!mask) {
    port.println("OW:CONV:NODEV");
    return;
  }
  convStart = millis();
  convPending = true;
  port.print("OW:CONV:OK:");
  port.println(mask);
}

// =========================================================================
// 1-Wire: поиск датчиков на всех шинах и чтение их памяти
// Строка на датчик: "OW:DEV:<шина 1..3>:<ROM 8 байт>:<scratchpad 9 байт>:<1 внешнее / 0 паразитное>"
// CRC и температуру считает программа на ПК.
// =========================================================================
void owRead(Stream &port) {
  if (convPending) {                       // DS18B20 12 бит: до 750 мс
    while (millis() - convStart < 750) servicePulses();
    convPending = false;
  }
  byte addr[8];
  byte sp[9];
  int n = 0;
  for (byte b = 0; b < BUS_COUNT; b++) {
    OneWire &ow = *buses[b];
    ow.depower();                          // снять сильную подтяжку
    ow.reset_search();
    while (n < 48 && ow.search(addr)) {
      n++;
      ow.reset();
      ow.select(addr);
      ow.write(0xBE);                      // Read Scratchpad
      for (int i = 0; i < 9; i++) sp[i] = ow.read();
      ow.reset();
      ow.select(addr);
      ow.write(0xB4);                      // Read Power Supply
      byte ext = ow.read_bit();

      port.print("OW:DEV:");
      port.print(b + 1);
      port.print(':');
      for (int i = 0; i < 8; i++) printHex(port, addr[i]);
      port.print(':');
      for (int i = 0; i < 9; i++) printHex(port, sp[i]);
      port.print(':');
      port.println(ext);
      servicePulses();
    }
  }
  port.print("OW:END:");
  port.println(n);
}
