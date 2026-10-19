// =========================================================================
// ПОЛНЫЙ СКЕТЧ УПРАВЛЕНИЯ КОММУТАТОРОМ 176 КАНАЛОВ ДЛЯ БОСС v3.0
// =========================================================================

// Пины адресной шины (подключены параллельно ко всем 11 чипам)
const int pin_S0 = 2;
const int pin_S1 = 3;
const int pin_S2 = 4;
const int pin_S3 = 5;

// Массив пинов EN для каждого из 11 чипов (индексы от 0 до 10)
// Чипы 0...8 (каналы 0-143)   -> пины D6, D7, D8, D9, D10, D11, D12, D13, A0
// Чип 9      (каналы 144-159) -> пин A1 (Отрицательные напряжения -5В...0В)
// Чип 10     (каналы 160-175) -> пин A2 (Высокие напряжения ~16В)
const int pin_EN[] = {6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2};

void setup() {
  // Настраиваем адресную шину на выход
  pinMode(pin_S0, OUTPUT);
  pinMode(pin_S1, OUTPUT);
  pinMode(pin_S2, OUTPUT);
  pinMode(pin_S3, OUTPUT);

  // Настраиваем пины выбора чипа (EN) на выход и жестко выключаем их (HIGH)
  for (int i = 0; i < 11; i++) {
    pinMode(pin_EN[i], OUTPUT);
    digitalWrite(pin_EN[i], HIGH); 
  }

  // Запуск последовательного порта на высокой скорости (синхронизация с ПК)
  Serial.begin(115200);
  Serial.setTimeout(50);  // не ждать 1 с, если строка пришла без '\n'
  while (!Serial) { ; } // Ожидание подключения порта USB
}

void loop() {
  // Ожидаем команду от скрипта на Python
  if (Serial.available() > 0) {
    String inputString = Serial.readStringUntil('\n');
    inputString.trim(); // Очищаем строку от мусора и перевода строки

    // Проверяем формат команды, например: "SET:0" или "SET:175"
    if (inputString.startsWith("SET:")) {
      int globalChannel = inputString.substring(4).toInt();
      
      // Вызываем функцию переключения
      if (switchChannel(globalChannel)) {
        // Отправляем строгое подтверждение (Handshake) обратно в Python
        Serial.print("READY:");
        Serial.println(globalChannel);
      } else {
        // Если пришел некорректный номер канала
        Serial.println("ERR:INVALID_CHANNEL");
      }
    }
  }
}

// Функция физического переключения мультиплексоров
bool switchChannel(int channel) {
  int chipIndex = -1;
  int localChannel = -1;

  // 1. Распределяем глобальный номер канала по конкретным чипам и ножкам
  if (channel >= 0 && channel <= 143) {
    // Стандартные каналы 0-5В (Чипы 0...8)
    chipIndex = channel / 16;       // Вычисляем номер чипа (0-8)
    localChannel = channel % 16;    // Вычисляем номер ножки внутри чипа (0-15)
  } 
  else if (channel >= 144 && channel <= 159) {
    // Выделенный чип для отрицательных напряжений (Чип 9, пин A1)
    chipIndex = 9;
    localChannel = channel - 144;   // Сдвигаем диапазон 144-159 в локальные 0-15
  } 
  else if (channel >= 160 && channel <= 175) {
    // Выделенный чип для напряжений 16В (Чип 10, пин A2)
    chipIndex = 10;
    localChannel = channel - 160;   // Сдвигаем диапазон 160-175 в локальные 0-15
  }

  // Защитная проверка: если индекс чипа вышел за рамки 11 штук
  if (chipIndex < 0 || chipIndex > 10) {
    allChipsOff();
    return false;
  }

  // 2. БЕЗОПАСНОСТЬ: Сначала гарантированно отключаем ВСЕ чипы (уровень HIGH)
  // Это исключает ситуацию, когда два чипа окажутся открыты одновременно
  allChipsOff();

  // 3. Выставляем новый 4-битный адрес на параллельную шину S0-S3
  digitalWrite(pin_S0, (localChannel & 0x01) ? HIGH : LOW);
  digitalWrite(pin_S1, (localChannel & 0x02) ? HIGH : LOW);
  digitalWrite(pin_S2, (localChannel & 0x04) ? HIGH : LOW);
  digitalWrite(pin_S3, (localChannel & 0x08) ? HIGH : LOW);

  // Микросекундная пауза для стабилизации логических уровней на дорожках
  delayMicroseconds(50);

  // 4. Физически включаем ТОЛЬКО ОДИН выбранный чип (уровень LOW)
  digitalWrite(pin_EN[chipIndex], LOW);

  // Задержка на переходные процессы в ключе. CD74HC4067 переключается за доли мкс,
  // 1 мс хватает с запасом; дополнительную паузу можно задать в программе (Settle delay)
  delay(1);
  
  return true;
}

// Функция полного сброса и отключения всей измерительной сети
void allChipsOff() {
  for (int i = 0; i < 11; i++) {
    digitalWrite(pin_EN[i], HIGH);
  }
}
