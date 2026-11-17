# -*- coding: utf-8 -*-
"""
telemetry_ru.py
===============
Russian texts of the Telemetry tab: field descriptions, value meanings and notes of the ИОК arrays
(telemetry.py) and of the MD_CONFIG / MD_SETTINGS editors (bos_mdfiles.py). Used by i18n.tr() when the interface
is Russian; the English texts in the code stay the source.
"""

from __future__ import annotations

import functools
import re

EXACT = {
    # ---- array header
    "Start of file marker": "Маркер начала файла",
    "Packet length in bytes excluding marker, type, length": "Длина пакета в байтах без маркера, типа и длины",
    "Sender's address": "Адрес отправителя",
    "TLM output frame format version": "Версия формата выдаваемого кадра ТЛМ",
    "TM output frame format version": "Версия формата выдаваемого кадра ТМ",
    "Cyclic counter of TLM frames of this type since power-on / reset (0 after 0xFFFF)":
        "Циклический счётчик кадров ТЛМ этого типа с включения / сброса (0 после 0xFFFF)",
    "Cyclic counter of TM frames of this type since power-on / reset (0 after 0xFFFF)":
        "Циклический счётчик кадров ТМ этого типа с включения / сброса (0 после 0xFFFF)",
    "This TLM type has no cyclic frame counter, constant 0x0000":
        "У этого типа ТЛМ нет циклического счётчика кадров, всегда 0x0000",
    # ---- UPNG ИОК_ОСН
    "Operating mode of the UPNG (from MD CONFIG or commands)": "Режим работы УПНГ (из МД КОНФИГ или по командам)",
    "Receiving channel status, bit 0..3 = channel 1..4 (1 = enabled)":
        "Состояние приёмных каналов, бит 0..3 = канал 1..4 (1 = включён)",
    "Band used": "Используемая полоса",
    "Source of settings": "Источник настроек",
    "Modulation index (from 'Set Modulation Index')": "Индекс модуляции (из «Установить индекс модуляции»)",
    "Operating mode for the IPPD-KPI": "Режим работы по ИППД-КПИ",
    "IU operating mode": "Режим работы ИУ",
    "Letter number, from 'Set reception frequency' (0x0000…0x063E)":
        "Номер литеры, из «Установить частоту приёма» (0x0000…0x063E)",
    "Data transfer rate (from 'Set bit rate')": "Скорость передачи данных (из «Установить скорость»)",
    "reserve": "резерв",
    "Reserve": "Резерв",
    "ADC health per channel, bit 0..3 = channel 1..4 (1 = ADC OK, 0 = faulty)":
        "Исправность АЦП по каналам, бит 0..3 = канал 1..4 (1 = АЦП исправен, 0 = неисправен)",
    "Mask of working channels in SNR mode (0b1111 = all on, default)":
        "Маска рабочих каналов в режиме СР (0b1111 = все включены, по умолчанию)",
    "Mask of working channels in OZhR mode (0b1111 = all on, default)":
        "Маска рабочих каналов в режиме ОЖР (0b1111 = все включены, по умолчанию)",
    "Reference synthesizer PLL capture flag": "Признак захвата ФАПЧ опорного синтезатора",
    "Which synthesizer is working": "Какой синтезатор работает",
    "Angle angX at the time of IOC issuance, step 0.2° (0x80 = no angles)":
        "Угол angX на момент выдачи ИОК, шаг 0,2° (0x80 = нет углов)",
    "Angle angY at the time of IOC issuance, step 0.2° (0x80 = no angles)":
        "Угол angY на момент выдачи ИОК, шаг 0,2° (0x80 = нет углов)",
    "Target designation validity flag (angX, angY)": "Признак достоверности целеуказания (angX, angY)",
    "Criterion for automatic transition of the OZh to SNR": "Критерий автоматического перехода из ОЖР в СР",
    "Angular ambiguity resolution bit on the ES": "Бит разрешения угловой неоднозначности на ЗС",
    "Direction determination bit to the ES": "Бит определения направления на ЗС",
    "Friend/Foe confirmation bit of the ZS": "Бит подтверждения «свой/чужой» ЗС",
    "AGC coefficient 1 *1": "Коэффициент АРУ 1 *1",
    "AGC coefficient 2 *1": "Коэффициент АРУ 2 *1",
    "Parameters of the amplifier *1": "Параметры усилителя *1",
    "Wideband FFT frequency index of the signal *1": "Частотный индекс сигнала по широкополосному БПФ *1",
    "Frequency loop readings *1": "Показания частотной петли *1",
    "Counter of detected CBDs since power-on *2": "Счётчик обнаруженных КБД с включения *2",
    "Counter of KBDs received without error correction *2": "Счётчик КБД, принятых без исправления ошибок *2",
    "Counter of CBDs accepted with error correction *2": "Счётчик КБД, принятых с исправлением ошибок *2",
    "Counter of sent TCs received from the KBD (IPPD-KPI) *2": "Счётчик выданных ТК, полученных из КБД (ИППД-КПИ) *2",
    "Counter of sent TC-Tests *2": "Счётчик выданных ТК-Тест *2",
    "Counter of 'Target designation to the ZS' commands from the UPNG *2":
        "Счётчик команд «Целеуказание на ЗС» от УПНГ *2",
    "Counter of received 'Rough direction to ZS' commands *2": "Счётчик принятых команд «Грубое направление на ЗС» *2",
    "Counter of accepted 'KBD-own' commands *2": "Счётчик принятых команд «КБД-свой» *2",
    "Last accepted command (except 'Rough direction to ZS', 'KBD-your') *2":
        "Последняя принятая команда (кроме «Грубое направление на ЗС», «КБД-свой») *2",
    "Parameters of the last received command, bytes 0-3 *2": "Параметры последней принятой команды, байты 0-3 *2",
    "Parameters of the last received command, bytes 4-7 *2": "Параметры последней принятой команды, байты 4-7 *2",
    "Counter of executed commands (except 'Rough direction', 'KBD-your') *2":
        "Счётчик выполненных команд (кроме «Грубое направление», «КБД-свой») *2",
    "Counter of ignored commands: not in the UPNG list / invalid parameters *2":
        "Счётчик проигнорированных команд: нет в перечне УПНГ / недопустимые параметры *2",
    "Counter of SNR-mode activations in the current session *2": "Счётчик включений режима СР за текущее включение *2",
    "Checksum of the factory CONFIG file in ROM": "Контрольная сумма заводского файла КОНФИГ в ПЗУ",
    "Checksum of the IOK_STATUS memory in EPROM": "Контрольная сумма памяти ИОК_СОСТОЯНИЕ в ПЗУ",
    "Temperature response setting": "Настройка срабатывания по температуре",
    "Temperature status": "Состояние по температуре",
    "Counter of commands changing MD CONFIG parameters": "Счётчик команд, изменяющих параметры МД КОНФИГ",
    "Counter of received MD_CONFIG arrays": "Счётчик принятых массивов МД_КОНФИГ",
    # ---- UPNG ИОК_СОСТ
    "FPGA firmware version": "Версия прошивки ПЛИС",
    "Firmware checksum (p.4.1.2.3)": "Контрольная сумма прошивки (п. 4.1.2.3)",
    "softCPU instruction set version": "Версия набора инструкций softCPU",
    "softCPU instruction set checksum (p.4.1.2.3)": "Контрольная сумма набора инструкций softCPU (п. 4.1.2.3)",
    "Counter of received valid CONFIG MD arrays / 'Save settings in CONFIG file' commands (default 0x01).":
        "Счётчик принятых достоверных массивов МД КОНФИГ / команд «Сохранить настройки в файле КОНФИГ» "
        "(по умолчанию 0x01).",
    "Temperature protection response counter.": "Счётчик срабатываний температурной защиты.",
    "Reset counter on command 'Reset'.": "Счётчик сбросов по команде «Сброс».",
    "Counter of power-on cycles over the whole life of the UPNG.": "Счётчик включений УПНГ за весь срок службы.",
    "Total operating time of the UPNG over its service life, 1 s per unit (counted from power-on, regardless of mode)":
        "Суммарная наработка УПНГ за срок службы, ед. = 1 с (с момента включения, независимо от режима)",
    "Operating time of the main synthesizer, 1 s per unit": "Наработка основного синтезатора, ед. = 1 с",
    "Operating time of the backup synthesizer, 1 s per unit": "Наработка резервного синтезатора, ед. = 1 с",
    # ---- UPNG ИОК_ОТКАЗ
    "CRC of the program read from main memory": "CRC программы, считанной из основной памяти",
    "CRC of the program calculated for main memory": "CRC программы, вычисленная для основной памяти",
    "CRC of the program read from backup memory": "CRC программы, считанной из резервной памяти",
    "CRC of the program calculated for the backup memory": "CRC программы, вычисленная для резервной памяти",
    # ---- BOS ИОК_ОСН
    "BOS time (format see p.4.2.3.12)": "Время БОС (формат см. п. 4.2.3.12)",
    "Received pulse counter 1C. Reset on power-on / reset.": "Счётчик принятых импульсов 1С. Обнуляется при включении / сбросе.",
    "Counter of received 'BSHV Time' commands. Reset on power-on / reset.":
        "Счётчик принятых команд «Время БШВ». Обнуляется при включении / сбросе.",
    "Integer part of the time from 'Set transfer time in SR', 1 s per unit (1)":
        "Целая часть времени из «Установить время передачи в СР», ед. = 1 с (1)",
    "Fractional part of the time from 'Set transfer time in SR', 10 ms per unit (1)":
        "Дробная часть времени из «Установить время передачи в СР», ед. = 10 мс (1)",
    "Pulses of the external 10 MHz generator in the interval Tinterv10MHz":
        "Импульсы внешнего генератора 10 МГц на интервале Tinterv10MHz",
    "EG tuning code (external - internal EG pulse counters), should approach 0, normal ±1000":
        "Код подстройки ОГ (разность счётчиков импульсов внешнего и внутреннего ОГ), стремится к 0, норма ±1000",
    "External 10 MHz generator validity (checked every 1 s) (2)":
        "Достоверность внешнего генератора 10 МГц (проверяется каждую 1 с) (2)",
    "Current 10 MHz reference oscillator source (3)": "Текущий источник опорного генератора 10 МГц (3)",
    "Data randomization mode in SR": "Режим рандомизации данных в СР",
    "Current configuration": "Текущая конфигурация",
    "Number of the BOS kit issuing the IOC": "Номер комплекта БОС, выдающего ИОК",
    "Current operating mode of the BOS (4)": "Текущий режим работы БОС (4)",
    "Error flag for automatic selection of the BPU kit": "Признак ошибки автоматического выбора комплекта БПУ",
    "Included BPU kit": "Включённый комплект БПУ",
    "Quick-start control mode of SR in AFAR": "Режим управления быстрым включением СР в АФАР",
    "Operating mode of the BOS according to CAN": "Режим работы БОС по CAN",
    "Current source of target designations used for the ZS": "Текущий источник целеуказаний на ЗС",
    "Target designation validity (angX, angY) (5)": "Достоверность целеуказания (angX, angY) (5)",
    "Automatic shutdown when the temperature is exceeded": "Автоотключение при превышении температуры",
    "Temperature sensor that caused automatic shutdown (trigger, bit = sensor) (6)":
        "Датчик температуры, вызвавший автоотключение (триггер, бит = датчик) (6)",
    "Sub-mode of the BOS in the SR (current / last) (7)": "Подрежим БОС в СР (текущий / последний) (7)",
    "SR shutdowns by automatic temperature shutdown (8)": "Выключения СР по автоотключению по температуре (8)",
    "SR shutdowns because the SR duration ('Time SNR' in CONFIG) was exceeded (8)":
        "Выключения СР из-за превышения длительности СР («Time SNR» в КОНФИГ) (8)",
    "SR shutdowns on receipt of a new SR control command (8)": "Выключения СР при приёме новой команды управления СР (8)",
    "Forced SR shutdowns by the 'Turn on the JR' command (8)": "Принудительные выключения СР командой «Включить ДЖР» (8)",
    "SR shutdowns on unacceptable angles angX/angY in 'Target designations' (9)":
        "Выключения СР при недопустимых углах angX/angY в «Целеуказаниях» (9)",
    "Number of the last received SR control command": "Номер последней принятой команды управления СР",
    "Parameters of the last received SR control command": "Параметры последней принятой команды управления СР",
    "Logical SR counter for the current BOS enable. Reset on power-on / reset.":
        "Счётчик логических СР за текущее включение БОС. Обнуляется при включении / сбросе.",
    "Physical SR counter (RF signal generated) for the current enable. Reset on power-on / reset. (10)":
        "Счётчик физических СР (формировался ВЧ-сигнал) за текущее включение. Обнуляется при включении / сбросе. (10)",
    "'User' configuration file number (from CONFIG, low byte)":
        "Номер «пользовательского» файла конфигурации (из КОНФИГ, младший байт)",
    "Sub-number of the 'user' configuration file, +1 per valid CONFIG change (12)":
        "Подномер «пользовательского» файла конфигурации, +1 за каждое достоверное изменение КОНФИГ (12)",
    "Sub-number of the 'user' configuration file, +1 per valid CONFIG change (6)":
        "Подномер «пользовательского» файла конфигурации, +1 за каждое достоверное изменение КОНФИГ (6)",
    "Received KBD via IPPD-TMI in the current BOS session (all SRs)":
        "КБД, принятые по ИППД-ТМИ за текущее включение БОС (все СР)",
    "Ignored KBD via IPPD-TMI ('short' criterion)": "Проигнорированные КБД по ИППД-ТМИ (критерий «короткий»)",
    "Ignored KBD via IPPD-TMI ('long' criterion)": "Проигнорированные КБД по ИППД-ТМИ (критерий «длинный»)",
    "Sent KBDs in the current BOS session (all SRs). Reset on power-on / reset.":
        "Выданные КБД за текущее включение БОС (все СР). Обнуляется при включении / сбросе.",
    "Sent CBDs in the current BOS SR session": "Выданные КБД за текущее включение БОС в режиме СР",
    "Received 'Target designation to the ZS from the UPNG' commands": "Принятые команды «Целеуказание на ЗС от УПНГ»",
    "Received 'Target designation to the ZS from the BKU' commands. Reset on power-on / reset.":
        "Принятые команды «Целеуказание на ЗС от БКУ». Обнуляется при включении / сбросе.",
    "Last received command (except cyclic BShV / target designation and SR control). Reset on power-on / reset.":
        "Последняя принятая команда (кроме циклических БШВ / целеуказания и управления СР). Обнуляется при "
        "включении / сбросе.",
    "NOT IN THE PROVIDED TABLES (bytes 65-72)": "НЕТ В ПРЕДОСТАВЛЕННЫХ ТАБЛИЦАХ (байты 65-72)",
    "Executed commands (except cyclic BShV / target designation and SR control)":
        "Выполненные команды (кроме циклических БШВ / целеуказания и управления СР)",
    "Ignored commands: invalid parameters / not in the BOS list":
        "Проигнорированные команды: недопустимые параметры / нет в перечне БОС",
    "Non-zero KBD requests issued in BARL (reset by SR control command)":
        "Ненулевые запросы КБД, выданные в БАРЛ (обнуляются командой управления СР)",
    "Last non-zero number of requested CBDs ('Request CBD in BARL')":
        "Последнее ненулевое число запрошенных КБД («Запрос КБД в БАРЛ»)",
    "Total duration of operation in the SR, 10 ms per unit (13)": "Суммарная длительность работы в СР, ед. = 10 мс (13)",
    "Total duration of work in the SR, 10 ms per unit (13)": "Суммарная длительность работы в режиме СР, ед. = 10 мс (13)",
    "Temperature at the moment of automatic shutdown (trigger) (15)":
        "Температура в момент автоотключения (триггер) (15)",
    "Flash write pointer of IOK_ARCHIVE, in 16-byte steps (one SR entry)":
        "Указатель записи ИОК_АРХИВ во flash, шаг 16 байт (одна запись СР)",
    "Data received from BPU1 via feedback, +1 per second (changing = BPU1 works)":
        "Данные от БПУ1 по обратной связи, +1 в секунду (меняются = БПУ1 работает)",
    "Data received from BPU2 via feedback, +1 per second (changing = BPU2 works)":
        "Данные от БПУ2 по обратной связи, +1 в секунду (меняются = БПУ2 работает)",
    "Reserve, padding to a multiple of 8": "Резерв, дополнение до кратного 8",
    # ---- BOS ИОК_СОСТ
    "MD_CONFIG save counter in flash memory: +1 per valid configuration array or 'Save settings in CONFIG file' "
    "command (1)": "Счётчик сохранений МД_КОНФИГ во flash: +1 за каждый достоверный массив конфигурации или "
                   "команду «Сохранить настройки в файле КОНФИГ» (1)",
    "Flash data storage counter: +1 each time the IOK_STATE parameters are written to flash ('Data Parking' and "
    "'Change' commands) (2)": "Счётчик сохранения данных во flash: +1 при каждой записи параметров ИОК_СОСТОЯНИЕ во "
                              "flash (команды «Парковка данных» и «Изменение») (2)",
    "Automatic shutdown counter based on temperature": "Счётчик автоотключений по температуре",
    "Power-on counter": "Счётчик включений",
    "Logical SR inclusions (= SR control commands)": "Логические включения СР (= команды управления СР)",
    "Physical SR inclusions (RF signal formed), all modes (UR + SR + CARRIER)":
        "Физические включения СР (формировался ВЧ-сигнал), все режимы (УР + ШР + НЕСУЩАЯ)",
    "Physical SR inclusions (RF signal formed), SR mode only":
        "Физические включения СР (формировался ВЧ-сигнал), только режим ШР",
    "KBD sent to the radio line, all modes (3)": "КБД, выданные в радиолинию, все режимы (3)",
    "CBDs sent to the radio line, SR only (3)": "КБД, выданные в радиолинию, только ШР (3)",
    "Total operating time of the BOS in all modes (LH + SR), 1 s per unit":
        "Суммарная наработка БОС во всех режимах (ДЖР + СР), ед. = 1 с",
    "Total duration of operation in SR regardless of BOS mode, 10 ms per unit":
        "Суммарная длительность работы в СР независимо от режима БОС, ед. = 10 мс",
    "Total duration of work in the SR, 10 ms per unit": "Суммарная длительность работы в режиме ШР, ед. = 10 мс",
    # ---- BOS ИОК_ДОП
    "Encoder synthesizer PLL capture": "Захват ФАПЧ синтезатора кодера",
    "JESD PLL capture FPGA": "Захват ФАПЧ JESD ПЛИС",
    "PLL capture FPGA (except JESD PLL)": "Захват ФАПЧ ПЛИС (кроме ФАПЧ JESD)",
    "Encoder PLL DAC capture": "Захват ФАПЧ ЦАП кодера",
    "PLL synthesizer modulator lock (table names it pll_lmx too)":
        "Захват ФАПЧ синтезатора модулятора (в таблице тоже назван pll_lmx)",
    "JESD DAC interface": "Интерфейс JESD ЦАП",
    "reserve (bit 7, not in the table)": "резерв (бит 7, нет в таблице)",
    "NOT IN THE TABLE (the table ends at byte 15, the unit sends 32 B)":
        "НЕТ В ТАБЛИЦЕ (таблица заканчивается байтом 15, блок выдаёт 32 Б)",
    # ---- AFAR ИОК_ОСН
    "Current operating mode of the AFAR (2)": "Текущий режим работы АФАР (2)",
    "Number of the BPU set issuing the IOC": "Номер комплекта БПУ, выдающего ИОК",
    "Allowed source of quick SR-inclusion commands": "Разрешённый источник команд быстрого включения СР",
    "AFAR consumption mode": "Режим потребления АФАР",
    "Specified source of target designations for the ZS": "Заданный источник целеуказаний на ЗС",
    "Target designation validity (angX, angY) (3)": "Достоверность целеуказания (angX, angY) (3)",
    "Operating mode of the AFAR according to CAN": "Режим работы АФАР по CAN",
    "Automatic shutdown when the temperature is exceeded (technological)":
        "Автоотключение при превышении температуры (технологический)",
    "Temperature sensor that caused automatic shutdown (trigger, bit = sensor 1..8) (4)":
        "Датчик температуры, вызвавший автоотключение (триггер, бит = датчик 1..8) (4)",
    "SR shutdowns by automatic temperature shutdown": "Выключения СР по автоотключению по температуре",
    "SR shutdowns because the SR duration ('Time SNR' in CONFIG) was exceeded":
        "Выключения СР из-за превышения длительности СР («Time SNR» в КОНФИГ)",
    "SR counter in the current AFAR activation, +1 at the start of SR enable commands. Reset on power-on / reset.":
        "Счётчик СР за текущее включение АФАР, +1 в начале команд включения СР. Обнуляется при включении / сбросе.",
    "Number of the 'user' configuration file (from CONFIG)": "Номер «пользовательского» файла конфигурации (из КОНФИГ)",
    "Last received command (except cyclic target designation). Reset on power-on / reset.":
        "Последняя принятая команда (кроме циклического целеуказания). Обнуляется при включении / сбросе.",
    "Parameters of the last received command, bytes 0-7 (missing bytes = 0x00). Reset on power-on / reset.":
        "Параметры последней принятой команды, байты 0-7 (отсутствующие = 0x00). Обнуляется при включении / сбросе.",
    "Executed commands (except cyclic target designation)": "Выполненные команды (кроме циклического целеуказания)",
    "Ignored commands: invalid parameters / not in the allowed list":
        "Проигнорированные команды: недопустимые параметры / нет в разрешённом перечне",
    "Total operating time since the AESA was turned on, 1 s per unit": "Суммарная наработка с включения АФАР, ед. = 1 с",
    "Total duration of operation in SR during this activation, 10 ms per unit (7)":
        "Суммарная длительность работы в СР за это включение, ед. = 10 мс (7)",
    "Temperature at automatic shutdown, T = value × 2 °C (trigger) (9)":
        "Температура при автоотключении, T = значение × 2 °C (триггер) (9)",
    "Data issued by the control unit via feedback, +1 per second (changing = unit works)":
        "Данные, выдаваемые блоком управления по обратной связи, +1 в секунду (меняются = блок работает)",
    "Session mode timer: set to 'Time_SNR' from CONFIG at SR on, then -1 per second to zero; 0 in DZhR":
        "Таймер режима СР: при включении СР = «Time_SNR» из КОНФИГ, затем -1 в секунду до нуля; 0 в ДЖР",
    "Temperature sensor 9..12 that caused automatic shutdown (trigger, bit 0 = sensor 9)":
        "Датчик 9..12, вызвавший автоотключение (триггер, бит 0 = датчик 9)",
    "Automatic shutdown on sensors 9..12 (bit 0 = sensor 9, 0b1111 = any)":
        "Автоотключение по датчикам 9..12 (бит 0 = датчик 9, 0b1111 = любой)",
    "Checksum of the MD_TECHN array (p.4.1.2.5)": "Контрольная сумма массива МД_ТЕХН (п. 4.1.2.5)",
    "Active page of the EPROM IOK_CONFIG, changes with each 'Save settings in CONFIG file'":
        "Активная страница ПЗУ ИОК_КОНФИГ, меняется при каждом «Сохранить настройки в файле КОНФИГ»",
    "reserve (not in the table)": "резерв (нет в таблице)",
    "Active page of the EPROM IOK_STATE, changes with each 'Data Parking'":
        "Активная страница ПЗУ ИОК_СОСТОЯНИЕ, меняется при каждой «Парковке данных»",
    "ROM write error IOK_CONFIG (1: no page could be written, active page unchanged)":
        "Ошибка записи ПЗУ ИОК_КОНФИГ (1: не удалось записать ни одной страницы, активная страница не изменена)",
    "ROM write error IOK_STATE (1: no page could be written, active page unchanged)":
        "Ошибка записи ПЗУ ИОК_СОСТОЯНИЕ (1: не удалось записать ни одной страницы, активная страница не изменена)",
    # ---- AFAR ИОК_СОСТ
    "MD_CONFIG save counter in flash: +1 per 'Save settings in CONFIG file' (1)":
        "Счётчик сохранений МД_КОНФИГ во flash: +1 за каждое «Сохранить настройки в файле КОНФИГ» (1)",
    "Flash data store counter, low 16 bits; high bits at bytes 54-55 (2)":
        "Счётчик сохранения данных во flash, младшие 16 бит; старшие - в байтах 54-55 (2)",
    "Automatic shutdown counter by temperature, total for all sensors":
        "Счётчик автоотключений по температуре, всего по всем датчикам",
    "SR counter": "Счётчик СР",
    "Total operating time of the BPU kit, all modes (DZhR + SR), 1 s per unit":
        "Суммарная наработка комплекта БПУ, все режимы (ДЖР + СР), ед. = 1 с",
    "Flash data store counter, high 16 bits (bits 31-16) (2)":
        "Счётчик сохранения данных во flash, старшие 16 бит (биты 31-16) (2)",
    # ---- value meanings (enums)
    "JR mode": "режим ДЖР", "OZhR mode": "режим ОЖР", "SNR mode": "режим СР", "OZhR/SNR mode": "режим ОЖР/СР",
    "narrow band": "узкая полоса", "wide band operation": "работа в широкой полосе",
    "default (factory) parameters": "параметры по умолчанию (заводские)", "from the MD CONFIG file": "из файла МД КОНФИГ",
    "TC only via line 1": "ТК только по линии 1", "TC only via line 2": "ТК только по линии 2",
    "TC on both lines": "ТК по обеим линиям", "via bus IU1": "по шине ИУ1", "via bus IU2": "по шине ИУ2",
    "on both buses": "по обеим шинам", "carrier reception": "приём несущей", "8 kbps": "8 кбит/с",
    "16 kbps": "16 кбит/с", "no PLL capture at last start": "нет захвата ФАПЧ при последнем запуске",
    "PLL frequency capture at last start": "захват частоты ФАПЧ при последнем запуске", "all off": "все выключены",
    "main": "основной", "angles are invalid": "углы недостоверны", "angles are valid": "углы достоверны",
    "on 'KBD-own' command": "по команде «КБД-свой»",
    "on a packet of correct structure (CRC ok)": "по пакету правильной структуры (CRC в норме)",
    "rough orientation to ZS not in tolerance": "грубая ориентация на ЗС вне допуска",
    "rough orientation to ZS in tolerance": "грубая ориентация на ЗС в допуске",
    "no direction finding / no signal from ZS": "нет пеленга / нет сигнала от ЗС",
    "direction to the ZS found": "направление на ЗС найдено",
    "no 'KBD-own' command in the interval": "нет команды «КБД-свой» на интервале",
    "'KBD-own' command in the interval": "есть команда «КБД-свой» на интервале", "OK": "норма",
    "error in both memories": "ошибка в обеих памятях", "auto power-off disabled": "автоотключение запрещено",
    "auto-off by sensor 1": "автоотключение по датчику 1", "auto-off by sensor 2": "автоотключение по датчику 2",
    "auto-off by both sensors": "автоотключение по обоим датчикам", "normal": "норма",
    "TEMPERATURE EXCESS": "ПРЕВЫШЕНИЕ ТЕМПЕРАТУРЫ", "no signal from the generator": "нет сигнала от генератора",
    "norm": "норма", "internal": "внутренний", "external 10 MHz": "внешний 10 МГц", "no": "нет", "yes": "да",
    "factory (from BOS software)": "заводская (из ПО БОС)", "custom (from CONFIG file)": "пользовательская (из файла КОНФИГ)",
    "first set": "первый комплект", "second set": "второй комплект", "standby mode": "дежурный режим",
    "session mode (SR)": "режим сеанса (СР)", "BPU not selected": "БПУ не выбран", "both disabled": "оба выключены",
    "BPU1 included": "включён БПУ1", "BPU2 included": "включён БПУ2", "both sets included": "включены оба комплекта",
    "QuickOn signal only": "только сигнал QuickOn", "commands via CAN only": "только команды по CAN",
    "signal and command": "сигнал и команда", "via CAN1 bus": "по шине CAN1", "via CAN2 bus": "по шине CAN2",
    "on both CAN buses": "по обеим шинам CAN",
    "no reliable designations, source not selected": "нет достоверных целеуказаний, источник не выбран",
    "UPNG": "УПНГ", "BKU": "БКУ", "BKU and UPNG": "БКУ и УПНГ", "invalid from both sources": "недостоверно от обоих источников",
    "BKU invalid, UPNG valid": "БКУ недостоверно, УПНГ достоверно", "BKU valid, UPNG invalid": "БКУ достоверно, УПНГ недостоверно",
    "valid from both sources": "достоверно от обоих источников", "on any sensor": "по любому датчику",
    "no transmission since power-on": "передачи с включения не было", "carrier transmission": "передача несущей",
    "CBD transfer to UR": "передача КБД в УР", "preamble in SR at reception rate": "преамбулы в ШР в темпе приёма",
    "POOSSH in SR at reception rate": "ПООСШ в ШР в темпе приёма", "KBD in SR at reception rate": "КБД в ШР в темпе приёма",
    "preamble in SR at delayed time": "преамбулы в ШР в заданное время", "POOSSH in SR at delayed time": "ПООСШ в ШР в заданное время",
    "KBD in SR at delayed time": "КБД в ШР в заданное время", "PLL unlock": "нет захвата ФАПЧ", "PLL lock": "захват ФАПЧ",
    "error": "ошибка", "no errors": "нет ошибок", "standby mode (DZhR)": "дежурный режим (ДЖР)",
    "factory (from SPO BPU)": "заводская (из СПО БПУ)", "allowed by BOS (QuickOn)": "разрешено от БОС (QuickOn)",
    "allowed from BKU": "разрешено от БКУ", "reduced consumption": "пониженное потребление",
    "normal consumption": "нормальное потребление", "BKU or UPNG": "БКУ или УПНГ",
    "COULD NOT WRITE ANY PAGE": "НЕ УДАЛОСЬ ЗАПИСАТЬ НИ ОДНОЙ СТРАНИЦЫ",
    "disabled": "выключено", "undefined value": "неопределённое значение", "reserve bits not 0": "резервные биты не 0",
    "not in array": "нет в массиве", "all channels off": "все каналы выключены", "no angle (0x80)": "нет угла (0x80)",
    "not triggered (initial -128)": "не срабатывало (начальное -128)",
    "not triggered (initial value)": "не срабатывало (начальное значение)",
    # ---- notes under the tables
    "*1  processing algorithm in Appendix A; 0x0000 at power-on / reset / disabled channel, last value in DZR mode.     "
    "*2  cleared on power cycle and reset.     Angle options 1 and 2 differing by more than 2° indicates problems in "
    "the channels.":
        "*1  алгоритм обработки - в приложении А; 0x0000 при включении / сбросе / выключенном канале, в ДЖР - последнее "
        "значение.     *2  обнуляется при выключении питания и сбросе.     Расхождение вариантов угла 1 и 2 более 2° "
        "указывает на неисправность каналов.",
    "(1) set by 'Set transmission time in SR', 0 after power-on.  (2) checked every 1 s.  (3) falls back to internal "
    "if the external 10 MHz is missing/lost.  (4) set while the SR is logically on.  (5) UPNG validity from its "
    "command, BKU validity vs. CONFIG limits.  (6) trigger, cleared on power-up.  (7) 0x00 after power-on; A2/A3/A4 "
    "become A5/A6/A7 with a delayed time.  (8) counted while in logical SR.  (10) may grow faster than Cnt_SNR_Cmd in "
    "ImpRP mode.  (11) written whether or not the angles are reliable.  (12) save CONFIG before the sub-number "
    "overflows.  (13) since power-on, physical SR (RF radiated).  (14) from CONFIG / 'Set telemetry output period'.  "
    "(15) -128 after power-on/reset.":
        "(1) задаётся командой «Установить время передачи в СР», 0 после включения.  (2) проверяется каждую 1 с.  "
        "(3) переходит на внутренний, если внешний 10 МГц отсутствует / пропал.  (4) устанавливается, пока СР "
        "логически включён.  (5) достоверность УПНГ - из его команды, БКУ - по пределам КОНФИГ.  (6) триггер, "
        "обнуляется при включении.  (7) 0x00 после включения; A2/A3/A4 становятся A5/A6/A7 при заданном времени.  "
        "(8) считается в логическом СР.  (10) в режиме ImpRP может расти быстрее Cnt_SNR_Cmd.  (11) записывается "
        "независимо от достоверности углов.  (12) сохраните КОНФИГ до переполнения подномера.  (13) с включения, "
        "физический СР (ВЧ излучается).  (14) из КОНФИГ / «Установить период выдачи телеметрии».  (15) -128 после "
        "включения / сброса.",
    "(1) counts valid CONFIG arrays and 'Save settings in CONFIG file'.  (2) counts writes of IOK_STATE to flash.  "
    "(3) only KBD generated from IPPD data; KBD-HC generated by the BOS are not counted.":
        "(1) считает достоверные массивы КОНФИГ и «Сохранить настройки в файле КОНФИГ».  (2) считает записи "
        "ИОК_СОСТОЯНИЕ во flash.  (3) только КБД, сформированные из данных ИППД; КБД-заполнители, формируемые БОС, "
        "не считаются.",
    "(1) bit 15 sign, bits 14..3 integer part, 2..0 fractional part (1/8); the values fit millivolts (e.g. 0x20DF = "
    "1051.875 mV).":
        "(1) бит 15 - знак, биты 14..3 - целая часть, 2..0 - дробная часть (1/8); значения соответствуют милливольтам "
        "(например 0x20DF = 1051,875 мВ).",
    "(1) set at the start of each SR, all 0 before the first SR; 'SR with all emitters' = all bits 1.  (2) set by "
    "quick SR on, reset by quick SR off / power-on.  (3) BKU angles valid when both differ from 0x80.  (4) trigger, "
    "cleared at power-up and before SR on.  (5) written whether or not reliable.  (6) save CONFIG before the "
    "sub-number overflows.  (7) physical SR (RF radiated).  (8) from CONFIG / 'Set telemetry output period'.  (9) "
    "initial value 0x7F.  Temperatures and currents are technological parameters, not controlled by the control "
    "unit.":
        "(1) устанавливается в начале каждого СР, до первого СР все 0; «СР со всеми излучателями» = все биты 1.  "
        "(2) устанавливается быстрым включением СР, сбрасывается быстрым выключением СР / включением питания.  "
        "(3) углы БКУ достоверны, если оба отличны от 0x80.  (4) триггер, обнуляется при включении и перед "
        "включением СР.  (5) записывается независимо от достоверности.  (6) сохраните КОНФИГ до переполнения "
        "подномера.  (7) физический СР (ВЧ излучается).  (8) из КОНФИГ / «Установить период выдачи телеметрии».  "
        "(9) начальное значение 0x7F.  Температуры и токи - технологические параметры, блоком управления не "
        "контролируются.",
    "(1) +1 per 'Save settings in CONFIG file'.  (2) +1 each time IOK_STATE is written to flash ('Park Data', "
    "'Change EPROM'); the value is split: low 16 bits at bytes 13-14, high 16 bits at 54-55.  (3) technological "
    "parameter, not controlled by the control unit.":
        "(1) +1 за каждое «Сохранить настройки в файле КОНФИГ».  (2) +1 при каждой записи ИОК_СОСТОЯНИЕ во flash "
        "(«Парковка данных», «Изменение ПЗУ»); значение разделено: младшие 16 бит - в байтах 13-14, старшие 16 бит - "
        "в 54-55.  (3) технологический параметр, блоком управления не контролируется.",
    "no array received yet - connect the CAN tab or open a log there":
        "массив ещё не принят - подключите линию на вкладке CAN или откройте там журнал",
    # ---- MD_CONFIG / MD_SETTINGS editors (bos_mdfiles)
    "user configuration number, +1 at every save (set by the BOS)":
        "номер пользовательской конфигурации, +1 при каждом сохранении (задаёт БОС)",
    "sub-number: +1 per configuration command since the last save":
        "подномер: +1 за каждую команду конфигурации после последнего сохранения",
    "factory": "заводская", "user": "пользовательская", "10 MHz reference source": "источник опорного сигнала 10 МГц",
    "external": "внешний", "data randomisation in ШР": "рандомизация данных в ШР", "off": "выкл.", "on": "вкл.",
    "АФАР control unit kit": "комплект блока управления АФАР", "automatic (default)": "автоматически (по умолчанию)",
    "BPU1": "БПУ1", "BPU2": "БПУ2", "automatic": "автоматически",
    "fast session switch-on of the АФАР": "быстрое включение СР в АФАР",
    "signal + command (default)": "сигнал + команда (по умолчанию)", "CAN commands only": "только команды CAN",
    "signal + command": "сигнал + команда", "over-temperature automatic shutdown": "автоотключение при перегреве",
    "auto shutdown off": "автоотключение выключено", "sensor 1": "датчик 1", "sensor 2": "датчик 2",
    "sensor 3": "датчик 3", "any sensor (default)": "любой датчик (по умолчанию)",
    "target designation source for starting a session": "источник целеуказаний для начала СР",
    "both sources (default)": "оба источника (по умолчанию)", "УПНГ only": "только УПНГ", "БКУ only": "только БКУ",
    "both sources": "оба источника", "first kit": "первый комплект", "second kit": "второй комплект",
    "session limit, 0 = none": "ограничение длительности СР, 0 = нет",
    "permissible angle, 0.2°/LSB (0x4B = 15°, 0x7D = 25°)": "допустимый угол, 0,2°/ед. (0x4B = 15°, 0x7D = 25°)",
    "preambles in ШР/ПООСШ and ШР/КБД = value + 1 (4 → 5)": "преамбул в ШР/ПООСШ и ШР/КБД = значение + 1 (4 → 5)",
    "spacecraft identifier, 10 bits": "идентификатор КА, 10 бит", "preamble value": "значение преамбулы",
    "preamble PRS register start (lower 2 bytes used)": "начальное значение регистра ПСП преамбулы (используются младшие 2 байта)",
    "factory x16+x12+x3+x+1": "заводской x16+x12+x3+x+1", "user preamble polynomial": "пользовательский полином преамбулы",
    "КБД PRS register 1 start (X.1)": "начальное значение регистра 1 ПСП КБД (X.1)",
    "КБД PRS register 2 start (X.2)": "начальное значение регистра 2 ПСП КБД (X.2)",
    "user КБД polynomial (0 = none)": "пользовательский полином КБД (0 = нет)",
    "ИОК_ОСН period, 0 = off": "период ИОК_ОСН, 0 = выкл.", "ИОК_ДОП period, 0 = off": "период ИОК_ДОП, 0 = выкл.",
    "ИОК_СОСТОЯНИЕ period, 0 = off": "период ИОК_СОСТОЯНИЕ, 0 = выкл.",
    "ИОК_КОНФИГ period, 0 = off": "период ИОК_КОНФИГ, 0 = выкл.",
    "-128…127 °C (0x80 = -128, 0xFF = -1, 0x46 = 70)": "-128…127 °C (0x80 = -128, 0xFF = -1, 0x46 = 70)",
    "array type 0xF1": "тип массива 0xF1",
    "Each value is loaded when the temperature rises above its threshold; unused entries must be zero. DAC values are "
    "loaded only when a session starts (sent during a session they are kept for the next one); the OG DAC value is "
    "loaded in standby and turns the 10 MHz reference tuning off.":
        "Каждое значение загружается, когда температура поднимается выше его порога; неиспользуемые записи должны быть "
        "нулевыми. Значения ЦАП загружаются только при начале СР (присланные во время СР сохраняются до следующего); "
        "значение ЦАП ОГ загружается в ДЖР и выключает подстройку по опорному сигналу 10 МГц.",
}

_N = r"(\d+)"
PATTERNS = [  # (regex, replacement) for families of texts
    (r"^Array type: (.+)$", r"Тип массива: \1"),
    (rf"^MD CONFIG byte {_N} \(clause 4\.1\.2\.1\)$", r"Байт \1 МД КОНФИГ (п. 4.1.2.1)"),
    (rf"^Channel status in line {_N}, bit 0\.\.7 = channel 1\.\.8 \(1 = on\) \({_N}\)$",
     r"Состояние каналов линейки \1, бит 0..7 = канал 1..8 (1 = вкл.) (\2)"),
    (rf"^Current consumption of line {_N}: (.+) \(unit 'mA' per table\)$",
     r"Ток потребления линейки \1: \2 (единица «мА» по таблице)"),
    (rf"^Internal ([+-][\d.]+) V of power supply {_N}: (.+)$", r"Внутреннее \1 В источника питания \2: \3"),
    (rf"^Internal ([+-][\d.]+) V of line {_N}: (.+)$", r"Внутреннее \1 В линейки \2: \3"),
    (rf"^Power consumption of emitter {_N} of line {_N}: (.+)$", r"Потребляемая мощность излучателя \1 линейки \2: \3"),
    (rf"^Temperature sensor {_N}, T = value × 2 °C \(technological\)$",
     r"Датчик температуры \1, T = значение × 2 °C (технологический)"),
    (rf"^Temperature sensor {_N}, degrees \(not used\)$", r"Датчик температуры \1, градусы (не используется)"),
    (rf"^Temperature sensor {_N}, degrees \(FPGA\)$", r"Датчик температуры \1, градусы (ПЛИС)"),
    (rf"^Temperature sensor {_N}, degrees$", r"Датчик температуры \1, градусы"),
    (rf"^Temperature sensor {_N} readings, degrees$", r"Показания датчика температуры \1, градусы"),
    (rf"^Operating time of receiving channel {_N}, 1 s per unit$", r"Наработка приёмного канала \1, ед. = 1 с"),
    (rf"^Signal power in channel {_N} after all AGC and transformations \*1$",
     r"Мощность сигнала в канале \1 после всех АРУ и преобразований *1"),
    (r"^Automatic shutdown counter by temperature on (DT\d+) \(technological\) \((\d+)\)$",
     r"Счётчик автоотключений по температуре по \1 (технологический) (\2)"),
    (r"^Automatic shutdown counter by temperature on (DT\d+)$", r"Счётчик автоотключений по температуре по \1"),
    (r"^Period of issuance of (IOK \w+), 1 s per unit, 0 = disabled \((\d+)\)$",
     r"Период выдачи \1, ед. = 1 с, 0 = выключено (\2)"),
    (r"^Angle (ang[XY]) at IOC issuance, option (\d), step 0\.2°$", r"Угол \1 на момент выдачи ИОК, вариант \2, шаг 0,2°"),
    (r"^(ang[XY]) from 'Target designation to the ZS from the (UPNG|BKU)' \(format p\.([\d.]+)\) \((\d+)\)$",
     lambda m: f"{m[1]} из «Целеуказание на ЗС от {'УПНГ' if m[2] == 'UPNG' else 'БКУ'}» (формат п. {m[3]}) ({m[4]})"),
    (r"^Voltage ([\d.]+) V FPGA \((\d+)\)$", r"Напряжение \1 В ПЛИС (\2)"),
    # ---- dynamic meanings
    (r"^channels (.+) on$", r"каналы \1 вкл."),
    (r"^on sensor (.+)$", r"по датчику \1"),
    (r"^TRIGGERED: sensor (.+)$", r"СРАБОТАЛО: датчик \1"),
    (r"^(-?\d+) °C at shutdown$", r"\1 °C при отключении"),
    (r"^(\d+) h (\d+) min ([\d.]+) s$", r"\1 ч \2 мин \3 с"),
    (r"^(\d+) h (\d+) min (\d+) s  \(([\d.]+) days\)$", r"\1 ч \2 мин \3 с  (\4 сут.)"),
    (r"^every (\d+) s$", r"каждые \1 с"),
    (r"^(-?[\d.]+) V  \((-?[\d.]+) mV\)$", r"\1 В  (\2 мВ)"),
    (r"^(-?[\d.]+) mA$", r"\1 мА"),
    (r"^(-?[\d.]+) V$", r"\1 В"),
    (r"^(-?[\d.]+) W$", r"\1 Вт"),
    (r"^(-?\d+) h (\d+) min ([\d.]+) s$", r"\1 ч \2 мин \3 с"),
    (r"^(-?\d+) h (\d+) min (\d+) s  \((-?[\d.]+) days\)$", r"\1 ч \2 мин \3 с  (\4 сут.)"),
    (r"^outside (.+)$", r"вне \1"),
    (r"^expected (0x[0-9A-F]+)$", r"ожидалось \1"),
    (r"^total \(high·65536 \+ low\) = (\d+)$", r"всего (старшие·65536 + младшие) = \1"),
    (r"^MISMATCH with (\S+) \((0x[0-9A-F]+)\)$", r"НЕ СОВПАДАЕТ с \1 (\2)"),
    (r"^Field layout of (.+) not entered yet - raw bytes of the last array:$",
     r"Описание полей \1 ещё не внесено - байты последнего массива:"),
    (r"^(\w+): telemetry not described yet\.$", r"\1: телеметрия ещё не описана."),
]
_PAT = [(re.compile(p), r) for p, r in PATTERNS]
_EP = re.compile(r"^(.*?)\s*\[EPROM addr (\d+), mask (0x[0-9A-F]+)\]$", re.S)
_EPROM_CNT = " Read from EPROM at power-up / reset, written back once per Tzap EPROM."
_EPROM_CNT_RU = " Считывается из ПЗУ при включении / сбросе, записывается обратно раз в Tzap ПЗУ."
_COMBO = re.compile(r"^(\S+) — (.+)$")                   # "1 — user" (MD editor value lists)


def _core(t):
    if t in EXACT:
        return EXACT[t]
    for rx, rep in _PAT:
        m = rx.match(t)
        if m:
            return rep(m) if callable(rep) else m.expand(rep)
    return None


@functools.lru_cache(maxsize=8192)
def tr(text: str):
    """Russian text, or None when not a Telemetry-tab text. Contains generic words ("OK", "on", "no"), so it is
    applied by the Telemetry tab / MD editors to their own cells only, not by the global translator."""
    t = text.strip()
    if not t:
        return None
    if t.startswith("<b>") and "&nbsp;·&nbsp; last " in t:          # array header line (ArrayHeader)
        return (t.replace("&nbsp;·&nbsp; last ", "&nbsp;·&nbsp; последний ").replace("received ", "принято ")
                .replace(" B &nbsp;", " Б &nbsp;").replace(" B<", " Б<").replace(" B, expected ", " Б, ожидалось ")
                .replace("incomplete arrays:", "неполных массивов:") + ("" if not t.endswith(" B") else "")
                ).removesuffix(" B") + (" Б" if t.endswith(" B") else "")
    m = _EP.match(t)
    suffix = ""
    if m:
        t, suffix = m[1].strip(), f"  [адр. ПЗУ {m[2]}, маска {m[3]}]"
    cnt = t.endswith(_EPROM_CNT.strip()) and t != _EPROM_CNT.strip()
    if cnt:
        t = t[: -len(_EPROM_CNT.strip())].strip()
    r = _core(t)
    if r is None:
        mc = _COMBO.match(t)
        if mc and _core(mc[2]) is not None:
            r = f"{mc[1]} — {_core(mc[2])}"
    if r is None:
        return None
    return r + (_EPROM_CNT_RU if cnt else "") + suffix


def ru_on() -> bool:
    try:
        import i18n
        return i18n.CURRENT == "ru"
    except Exception:
        return False


def T(text: str) -> str:
    """text in the interface language (Russian when available)."""
    if not text or not ru_on():
        return text
    r = tr(text)
    return text if r is None else r
