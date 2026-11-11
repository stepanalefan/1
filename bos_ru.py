# -*- coding: utf-8 -*-
"""
bos_ru.py
=========
Russian translation of the diagnosis texts (findings, actions, corrections, measurement names,
notes, verdict summary) and of the HTML report.

The English texts are built with measured values. Each entry below is an English template where
{} marks a variable part; `ru()` matches the English text against the templates (regular
expressions built from them), translates the captured values that are words (e.g. "external")
and fills the Russian template. Unknown texts are returned unchanged.
"""

from __future__ import annotations

import re

# English template -> Russian template ({} in the same order)
T = r"""
10 MHz source in КОНФИГ is {}|Источник 10 МГц в КОНФИГ: {}
All measured parameters are within limits.|Все измеренные параметры в допуске.
Atten_EIRP +{} dB lowered the output by only {} dB: at the nominal setting the PA (CHA3397) is compressed. This works for constant-envelope BPSK too.|Atten_EIRP +{} дБ снизил выход лишь на {} дБ: при номинальной настройке УМ (CHA3397) в компрессии. Проверка работает и для BPSK с постоянной огибающей.
Attenuator results not evaluated|Результаты аттенюатора не оценены
Attenuator {} dB step has no effect|Ступень аттенюатора {} дБ не действует
Attenuator {} dB step measures {} dB|Ступень аттенюатора {} дБ измерена как {} дБ
BER {}, RS corrected {} bytes, uncorrectable {}, CRC errors {}, MER {} dB: the bits are wrong before modulation - digital data path (FPGA coding / buffer / interface).|BER {}, RS исправлено {} байт, неисправимых {}, ошибок CRC {}, MER {} дБ: биты искажены до модуляции — цифровой тракт данных (кодирование в ПЛИС / буфер / интерфейс).
BER {}, RS corrected {} bytes, uncorrectable {}, CRC errors {}.|BER {}, RS исправлено {} байт, неисправимых {}, ошибок CRC {}.
BOS is locked to the external 10 MHz|БОС синхронизирован от внешних 10 МГц
BOS telemetry is stale|Телеметрия БОС устарела
BOS temperature high|Высокая температура БОС
BOS uses its internal 10 MHz oscillator|БОС работает от внутреннего генератора 10 МГц
BPSK (I rail only) {} dB but OQPSK {} dB → the Q rail (DAC channel B, its JESD lanes or the ADMV1013 Q input) is faulty.|BPSK (только канал I) {} дБ, а OQPSK {} дБ → неисправен канал Q (канал B ЦАП, его линии JESD или вход Q ADMV1013).
Carrier error {} Hz ({} ppm) while the symbol clock is {} → the LO path is off: LMX2594 frequency word / fractional-N setup or LO multiplier.|Ошибка несущей {} Гц ({} ppm), при этом символьная частота {} → ошибка в тракте гетеродина: кодовое слово частоты / дробный делитель LMX2594 или умножитель гетеродина.
Carrier error {} Hz ({} ppm). The RF is locked to the external 10 MHz (TFG6806) while the digital part (symbol clock) runs on the BOS's own oscillator, so an external reference offset moves only the carrier.|Ошибка несущей {} Гц ({} ppm). ВЧ синхронизирована от внешних 10 МГц (TFG6806), а цифровая часть (символьная частота) работает от собственного генератора БОС, поэтому уход внешней опоры смещает только несущую.
Carrier error {} Hz = {} ppm and symbol-rate error {} ppm agree: RF and symbol clock are off by the same ratio → the BOS's internal 10 MHz oscillator is off-frequency (TZ: ≤ 2·10⁻⁶ without external reference).|Ошибка несущей {} Гц = {} ppm и ошибка символьной скорости {} ppm совпадают: ВЧ и символьная частота смещены в одинаковой пропорции → уход внутреннего генератора 10 МГц БОС (ТЗ: ≤ 2·10⁻⁶ без внешней опоры).
Carrier frequency drifts {} Hz during the run|Частота несущей дрейфует на {} Гц за время проверки
Carrier frequency error (LO)|Ошибка частоты несущей (гетеродин)
Carrier frequency error (external reference or LO)|Ошибка частоты несущей (внешняя опора или гетеродин)
Carrier frequency error near the limit|Ошибка частоты несущей близка к допуску
Carrier: integrated phase noise {}° RMS|Несущая: интегральный фазовый шум {}° СКЗ
Carrier: spur {} dBc at {} kHz|Несущая: побочная составляющая {} дБн на {} кГц
DC offset at the modulator input or LO feed-through in the ADMV1013 → a line at the carrier frequency.|Постоянное смещение на входе модулятора или просачивание гетеродина в ADMV1013 → линия на частоте несущей.
Despreading recovers only {} % of the chip energy: the {} in the BOS differs from the expected one (polynomial, initial state, chip order or chip rate). With a strong signal the bits may still decode, but a ground station would lose {} dB of processing gain.|Сжатие спектра восстанавливает лишь {} % энергии чипов: {} в БОС отличается от ожидаемой (полином, начальное состояние, порядок или частота чипов). При сильном сигнале биты ещё декодируются, но наземная станция потеряет {} дБ выигрыша обработки.
Discrete line next to the signal: LMX2594 fractional / integer-boundary spur, DAC clock coupling or switching regulator.|Дискретная линия рядом с сигналом: дробная / граничная побочная LMX2594, наводка тактов ЦАП или импульсного стабилизатора.
Discrete line next to the unmodulated carrier: synthesizer (LMX2594 fractional / integer-boundary) spur, reference or clock coupling, or a switching regulator.|Дискретная линия рядом с немодулированной несущей: побочная синтезатора (дробная / граничная LMX2594), наводка опоры или тактов, либо импульсного стабилизатора.
Err_Ref_LO = {} (normal ±{}).|Err_Ref_LO = {} (норма ±{}).
Error {} dB.|Ошибка {} дБ.
External 10 MHz reference missing|Нет внешней опоры 10 МГц
FPGA rail {} V out of tolerance|Питание ПЛИС {} В вне допуска
Fast phase jitter → VCO noise / spurs of the LMX2594 or clock jitter on the DAC. Magnitude error only {} %.|Быстрый фазовый джиттер → шум ГУН / побочные LMX2594 или джиттер тактов ЦАП. Ошибка амплитуды лишь {} %.
Frame counter jumps ({})|Скачки счётчика кадров ({})
IOK OSN arrives but IOK DOP does not - its output period may be 0 (Period_FB).|ИОК_ОСН поступает, а ИОК_ДОП нет — возможно, период его выдачи равен 0 (Period_FB).
Idle frame content differs from table 4.4|Содержимое пустых кадров отличается от табл. 4.4
Image rejection {} dB. Unequal I and Q paths: DAC channel gain, filter / balun mismatch or ADMV1013 quadrature.|Подавление зеркального канала {} дБ. Неодинаковые тракты I и Q: усиление каналов ЦАП, разброс фильтров / симметрирующих трансформаторов или квадратура ADMV1013.
JESD204B link error FPGA → DAC38J82|Ошибка канала JESD204B ПЛИС → DAC38J82
LO-related line at {} GHz: {} dBc ({})|Составляющая гетеродина на {} ГГц: {} дБн ({})
Last IOK DOP {} s ago.|Последний ИОК_ДОП {} с назад.
Leakage of the LO or its multiples through the ADMV1013 ×4 multiplier and output.|Просачивание гетеродина или его гармоник через умножитель ×4 ADMV1013 на выход.
Limit {}°, band {}…{} kHz: noisy LO.|Допуск {}°, полоса {}…{} кГц: шумящий гетеродин.
MER as {} {} dB, as BPSK {} dB: the Q rail is silent - either the FPGA sends BPSK, or the Q path (DAC channel B / ADMV1013 Q) is dead.|MER как {} {} дБ, как BPSK {} дБ: канал Q молчит — либо ПЛИС передаёт BPSK, либо неисправен тракт Q (канал B ЦАП / Q ADMV1013).
MER as {} {} dB, as {} {} dB.|MER как {} {} дБ, как {} {} дБ.
MER {} dB but the synchromarker 0x1ACFFC1D is not in the bit stream (SP-L violations {} %): data format, rate or framing in the FPGA is wrong, or the buffer sends no КБД.|MER {} дБ, но синхромаркера 0x1ACFFC1D в потоке нет (нарушений SP-L {} %): неверный формат, скорость или кадрирование в ПЛИС, либо буфер не выдаёт КБД.
Main contributors: {}.|Основные причины: {}.
Max sensor temperature {} °C (limit {} °C).|Максимальная температура датчиков {} °C (допуск {} °C).
Measured over {} s in 10 ms windows (the receiver tracks it, so decoding works). The protocol allows ≤ 100 Hz/s for the reference adjustment (dF, table 7.1). Either the BOS reference / LO drifts (Err_Ref_LO tuning, temperature, LMX2594), or the FSW runs on a different 10 MHz than the BOS.|Измерено за {} с в окнах 10 мс (приёмник отслеживает дрейф, декодирование работает). Протокол допускает ≤ 100 Гц/с при подстройке опоры (dF, табл. 7.1). Дрейфует опора / гетеродин БОС (подстройка Err_Ref_LO, температура, LMX2594), либо FSW работает от другой опоры 10 МГц, чем БОС.
Measured weight {} dB.|Измеренный вес {} дБ.
Mostly slow phase wander → close-in noise of the LO loop (LMX2594 loop filter, charge pump, reference). Magnitude error only {} %.|В основном медленный уход фазы → шум вблизи несущей в петле гетеродина (фильтр петли LMX2594, накачка заряда, опора). Ошибка амплитуды лишь {} %.
No BOS arrays (ID 0x1A46xxxx) were received - PLL / JESD / rail checks are skipped and the diagnosis is less precise.|Массивы БОС (ID 0x1A46xxxx) не приняты — проверки ФАПЧ / JESD / питания пропущены, диагноз менее точен.
No BOS telemetry|Нет телеметрии БОС
No IOK DOP telemetry (PLL / JESD / rails)|Нет телеметрии ИОК_ДОП (ФАПЧ / JESD / питание)
No RF output detected|ВЧ-сигнал на выходе не обнаружен
No faults found|Неисправностей не обнаружено
No preamble (PRS 3, 0x00008805) in the capture although the OQPSK signal itself is clean: wrong spreading sequence, preamble value, chip rate or rate setting in the FPGA (or the capture starts after the preambles).|В записи нет преамбулы (ПСП 3, 0x00008805), хотя сам сигнал OQPSK чистый: неверная ПСП, значение преамбулы, частота чипов или скорость в ПЛИС (или запись начата после преамбул).
No preamble (PRS 3, 0x00008805) in the capture.|В записи нет преамбулы (ПСП 3, 0x00008805).
No single impairment dominates: broadband noise / distortion in the chain.|Ни одно искажение не преобладает: широкополосный шум / искажения в тракте.
None of the captures shows a signal above the FSW noise floor although all PLLs are locked and JESD is OK.|Ни в одной записи нет сигнала выше шумов FSW, хотя все ФАПЧ захвачены и JESD в норме.
None of the captures shows a signal above the FSW noise floor.|Ни в одной записи нет сигнала выше шумов FSW.
Now {} dB: {} dB are lost to I/Q imbalance and carrier leakage, which the DAC38J82 QMC can correct.|Сейчас {} дБ: {} дБ теряется из-за разбаланса I/Q и просачивания несущей, что корректируется QMC DAC38J82.
Only the I rail carries data ({} expected, BPSK-like signal)|Данные только в канале I (ожидался {}, сигнал как BPSK)
Output power drifts {} dB during the run|Выходная мощность дрейфует на {} дБ за время проверки
Output power high by {} dB|Выходная мощность выше нормы на {} дБ
Output power low by {} dB|Выходная мощность ниже нормы на {} дБ
Over-temperature shutdown threshold is higher than the test limit.|Порог отключения по перегреву выше допуска проверки.
PA compression confirmed by the linearity test|Компрессия УМ подтверждена проверкой линейности
PA current does not rise with RF on|Ток УМ не растёт при включении ВЧ
PA gain compression {} dB|Компрессия усиления УМ {} дБ
PAPR loss does not change with attenuation|Потеря PAPR не зависит от ослабления
Peak-to-average {} dB vs {} dB for an ideal signal.|Пик-фактор {} дБ против {} дБ у идеального сигнала.
Peak-to-average {} dB vs {} dB for an ideal signal; ACLR {} dB.|Пик-фактор {} дБ против {} дБ у идеального сигнала; ACLR {} дБ.
Preambles are present but no marker followed within the capture.|Преамбулы есть, но маркер за ними в записи не найден.
Q path fault (BPSK good, OQPSK bad)|Неисправность канала Q (BPSK в норме, OQPSK нет)
Reference / LO not stable over time or temperature.|Опора / гетеродин нестабильны во времени или по температуре.
Reference frequency offset|Уход частоты опорного генератора
Reference tuning code out of range|Код подстройки опоры вне диапазона
SCID in the transport frames differs from the setting.|SCID в транспортных кадрах отличается от настройки.
Session limit Time_SNR = {} s|Ограничение сеанса Time_SNR = {} с
Sour_10MHz = internal although the external 10 MHz (TFG6806) is expected and valid: the source is set to internal (КОНФИГ / SET_Sour_10MHz).|Sour_10MHz = внутренний, хотя внешние 10 МГц (TFG6806) ожидаются и в норме: источник установлен внутренним (КОНФИГ / SET_Sour_10MHz).
Spacecraft ID {} (expected {})|Идентификатор КА {} (ожидался {})
Spectral regrowth in the adjacent channels: non-linearity or missing pulse shaping.|Рост спектра в соседних каналах: нелинейность или отсутствует формирующий фильтр.
Stat_10MHz = no signal (count outside 10 000 000 ± 1000 per second); the BOS runs on its internal oscillator.|Stat_10MHz = нет сигнала (счёт вне 10 000 000 ± 1000 в секунду); БОС работает от внутреннего генератора.
Stored 0x{}, calculated 0x{}: the configuration in flash may be corrupt - the BOS would then start with its default settings.|Записано 0x{}, вычислено 0x{}: конфигурация во flash, возможно, повреждена — тогда БОС запустится с настройками по умолчанию.
Supply current too high|Слишком большой ток питания
Supply current {} A (RF off) → {} A (RF on).|Ток питания {} А (ВЧ выкл.) → {} А (ВЧ вкл.).
Symbol clock error|Ошибка символьной частоты
Symbol rate error {} ppm while the carrier is on frequency → DAC / FPGA sample clock is wrong (not the common reference).|Ошибка символьной скорости {} ppm при точной несущей → неверна тактовая частота ЦАП / ПЛИС (а не общая опора).
Temperature changed {} °C. Thermal gain drift of the PA or a marginal connection.|Температура изменилась на {} °C. Тепловой дрейф усиления УМ или ненадёжное соединение.
The BOS ends every session after this time; the station re-starts the session before each capture.|БОС завершает каждый сеанс по истечении этого времени; станция перезапускает сеанс перед каждой записью.
The DAC reports a JESD interface error: samples reaching the DAC are invalid or missing.|ЦАП сообщает об ошибке интерфейса JESD: отсчёты на ЦАП неверны или отсутствуют.
The FSW bandwidth (0.8 × sample rate) cuts the rectangular SP-L pulses: even a perfect signal then measures only ~28 dB MER (2 samples/symbol). Decoding is not affected. Capture with ≥ 8 samples per symbol (УР: sample rate ≥ 16 × data rate).|Полоса FSW (0,8 × частота дискретизации) обрезает прямоугольные импульсы SP-L: даже идеальный сигнал даёт лишь ~28 дБ MER (2 отсчёта на символ). На декодирование не влияет. Записывайте с ≥ 8 отсчётами на символ (УР: частота дискретизации ≥ 16 × скорость).
The LO is unlocked, so the carrier level is not stable enough to judge the attenuator bits.|Гетеродин не захвачен, уровень несущей нестабилен — оценить разряды аттенюатора нельзя.
The marker was found but no КБД followed in the expected form.|Маркер найден, но КБД за ним в ожидаемом виде нет.
The peak reduction is not from the PA: clipping / scaling in the FPGA or the DAC full scale.|Снижение пиков вызвано не УМ: ограничение / масштабирование в ПЛИС или полная шкала ЦАП.
The test expects the internal oscillator ('External 10 MHz expected' is off).|Проверка ожидает внутренний генератор (параметр «Ожидаются внешние 10 МГц» выключен).
The test expects the {} reference ('External 10 MHz expected' setting). A wrong stored setting is not a hardware fault.|Проверка ожидает опору: {} (параметр «Ожидаются внешние 10 МГц»). Неверная сохранённая настройка — не аппаратная неисправность.
The virtual channel frame counter is not continuous: frames lost in the buffer.|Счётчик кадров виртуального канала прерывается: кадры теряются в буфере.
Thermal gain drift of the PA or a marginal connection.|Тепловой дрейф усиления УМ или ненадёжное соединение.
Ttlm_dop_FB = 0 (factory default): PLL / JESD / rail telemetry only arrives after SET_Period_TM. The station enables it for the test.|Ttlm_dop_FB = 0 (заводская настройка): телеметрия ФАПЧ / JESD / питания приходит только после SET_Period_TM. Станция включает её на время проверки.
Upper and lower half of the spectrum differ: frequency-dependent I/Q imbalance (I/Q timing skew, baseband filter mismatch) or a tilted RF response.|Верхняя и нижняя половины спектра различаются: частотно-зависимый разбаланс I/Q (рассогласование задержек I/Q, разброс фильтров НЧ) или наклон АЧХ ВЧ-тракта.
Wider: pulse shaping (RRC) off / wrong roll-off or strong distortion. Narrower: wrong symbol rate configured.|Шире: формирующий фильтр (RRC) выключен / неверный коэффициент скругления или сильные искажения. Уже: задана неверная символьная скорость.
With {} dB more attenuation the PAPR loss drops by {} dB: the PA (CHA3397) is driven into compression at the nominal setting.|При ослаблении больше на {} дБ потеря PAPR уменьшается на {} дБ: при номинальной настройке УМ (CHA3397) работает в компрессии.
Wrong modulation: {} expected, {} received|Неверная модуляция: ожидалась {}, принята {}
{} = {} °C|{} = {} °C
{} A (limit {} A).|{} А (допуск {} А).
{} Hz ({} ppm).|{} Гц ({} ppm).
{} V ({} %, limit ±{} %).|{} В ({} %, допуск ±{} %).
{} dBm vs nominal {} dBm (incl. {} dB cable loss).|{} дБм при номинале {} дБм (с учётом потерь кабеля {} дБ).
{} dBm vs nominal {} dBm.|{} дБм при номинале {} дБм.
{} idle frame(s) with unexpected bytes.|Пустых кадров с неожиданными байтами: {}.
{} of {} frame(s) with CRC errors, code-bit BER {}.|{} из {} кадров с ошибками CRC, BER кодовых битов {}.
{}: ACLR {} dB below {} dB|{}: ACLR {} дБ ниже {} дБ
{}: I/Q imbalance (gain {} dB, quadrature {}°)|{}: разбаланс I/Q (усиление {} дБ, квадратура {}°)
{}: MER would be {} dB after I/Q correction|{}: после коррекции I/Q MER был бы {} дБ
{}: MER {} dB below {} dB|{}: MER {} дБ ниже {} дБ
{}: PA compression ({} dB PAPR loss)|{}: компрессия УМ (потеря PAPR {} дБ)
{}: carrier (LO) leakage {} dBc|{}: просачивание несущей (гетеродина) {} дБн
{}: carrier frequency wanders {} Hz p-p (up to {} Hz/s)|{}: частота несущей блуждает на {} Гц размах (до {} Гц/с)
{}: occupied bandwidth {} MHz ({} % of nominal)|{}: занимаемая полоса {} МГц ({} % от номинала)
{}: only {} samples per symbol in the capture|{}: в записи лишь {} отсчёта на символ
{}: phase noise / jitter ({}° RMS)|{}: фазовый шум / джиттер ({}° СКЗ)
{}: spectrum asymmetry {} dB|{}: асимметрия спектра {} дБ
{}: spur {} dBc at {} MHz|{}: побочная составляющая {} дБн на {} МГц
ИОК_ДОП is disabled in the stored КОНФИГ|ИОК_ДОП отключён в сохранённой КОНФИГ
ИОК_КОНФИГ checksum does not match|Контрольная сумма ИОК_КОНФИГ не совпадает
УР data errors with a clean RF signal ({})|Ошибки данных УР при чистом ВЧ-сигнале ({})
УР: no frames found although the BPSK signal is clean|УР: кадры не найдены, хотя сигнал BPSK чистый
ШР: data errors after despreading / turbo decoding|ШР: ошибки данных после сжатия спектра / турбодекодирования
ШР: end-of-preamble marker 0xDA2B1E5E not found|ШР: маркер конца преамбулы 0xDA2B1E5E не найден
ШР: idle frame content differs from table 4.4|ШР: содержимое пустых кадров отличается от табл. 4.4
ШР: preamble not found by despreading|ШР: преамбула при сжатии спектра не найдена
ШР: spreading sequence of the {} does not match|ШР: не совпадает расширяющая ПСП {}
ШР: КБД not decodable after the marker|ШР: КБД после маркера не декодируются
Apply the QMC gain / phase corrections (see Corrections)|Введите поправки усиления / фазы QMC (см. «Поправки»)
Attenuator verified OK - look at the PA|Аттенюатор проверен и исправен — ищите в УМ
Check ADMV1013 enable and LO drive|Проверьте включение ADMV1013 и уровень гетеродина
Check CAN wiring / termination|Проверьте проводку CAN / терминаторы
Check DAC38J82 IOUTB± with a scope|Проверьте IOUTB± DAC38J82 осциллографом
Check DAC38J82 channel B output (IOUTB±) with a scope|Проверьте выход канала B DAC38J82 (IOUTB±) осциллографом
Check FPGA lane mapping for Q|Проверьте назначение линий ПЛИС для Q
Check LMX2594 VCO calibration / lock (pll_lmx (mod))|Проверьте калибровку ГУН / захват LMX2594 (pll_lmx (mod))
Check LMX2594 programming for {} GHz (×{} = {} GHz)|Проверьте программирование LMX2594 на {} ГГц (×{} = {} ГГц)
Check Mode / Mode_SNR telemetry: is the BOS really transmitting?|Проверьте телеметрию Mode / Mode_SNR: передаёт ли БОС на самом деле?
Check PA heat sinking|Проверьте теплоотвод УМ
Check PRS 1/2 and the КБД format|Проверьте ПСП 1/2 и формат КБД
Check PRS polynomial / initial state and the preamble value (ИОК_КОНФИГ)|Проверьте полином / начальное состояние ПСП и значение преамбулы (ИОК_КОНФИГ)
Check SYNC~ signal|Проверьте сигнал SYNC~
Check SYSREF / device clock alignment|Проверьте выравнивание SYSREF / тактов устройства
Check digital scaling / back-off in the FPGA|Проверьте цифровое масштабирование / запас в ПЛИС
Check heat sinking / thermal pad of the CHA3397|Проверьте теплоотвод / термопрокладку CHA3397
Check lane mapping and polarity (FPGA vs DAC38J82)|Проверьте назначение и полярность линий (ПЛИС и DAC38J82)
Check that the BOS is powered|Проверьте, что на БОС подано питание
Check that the attenuator is not at 31.5 dB|Проверьте, что аттенюатор не на 31,5 дБ
Check that the output is matched (VSWR)|Проверьте согласование выхода (КСВН)
Check the 10 MHz reference|Проверьте опору 10 МГц
Check the ADMV1013 multiplier filter / LO drive level|Проверьте фильтр умножителя ADMV1013 / уровень гетеродина
Check the ADRF5730 control line(s) for the {} dB step from the FPGA|Проверьте линии управления ADRF5730 ступени {} дБ от ПЛИС
Check the ADRF5730 soldering / replace|Проверьте пайку ADRF5730 / замените
Check the BOS 10 MHz input|Проверьте вход 10 МГц БОС
Check the CAN command / CONFIG of the modulation mode|Проверьте команду CAN / КОНФИГ режима модуляции
Check the CAN line / BOS power|Проверьте линию CAN / питание БОС
Check the CHA3397 drain voltage / gate bias|Проверьте напряжение стока / смещение затвора CHA3397
Check the DAC NCO (if used) and the FPGA frequency settings|Проверьте NCO ЦАП (если используется) и настройки частоты в ПЛИС
Check the DAC clock PLL (pll_lmx (DAC)) settings and the FPGA symbol rate configuration|Проверьте настройки ФАПЧ тактов ЦАП (pll_lmx (DAC)) и символьную скорость в ПЛИС
Check the DAC lane / channel delay|Проверьте задержку линий / каналов ЦАП
Check the DC coupling / bias network between DAC and ADMV1013|Проверьте связь по постоянному току / цепи смещения между ЦАП и ADMV1013
Check the FPGA RRC filter / roll-off and the symbol rate in CONFIG|Проверьте фильтр RRC / коэффициент скругления в ПЛИС и символьную скорость в КОНФИГ
Check the FPGA firmware version (Soft_MORE_Ver)|Проверьте версию прошивки ПЛИС (Soft_MORE_Ver)
Check the I/Q baseband filters and trace lengths|Проверьте фильтры НЧ I/Q и длины проводников
Check the IPPD / MLVDS data input and the КБД buffer|Проверьте вход данных ИППД / MLVDS и буфер КБД
Check the LMX2594 loop filter and VCO calibration|Проверьте фильтр петли и калибровку ГУН LMX2594
Check the LMX2594 reference divider / PFD|Проверьте делитель опоры / ЧФД LMX2594
Check the LO loop filter components|Проверьте элементы фильтра петли гетеродина
Check the PA bias / supply|Проверьте смещение / питание УМ
Check the PA drain current|Проверьте ток стока УМ
Check the PA drain current / bias|Проверьте ток стока / смещение УМ
Check the PA gate bias / drain switch and its enable from the FPGA|Проверьте смещение затвора / ключ стока УМ и его включение от ПЛИС
Check the PA supply current (drain current rises with RF on)|Проверьте ток питания УМ (ток стока растёт при включении ВЧ)
Check the PRS polynomial / register settings (ИОК_КОНФИГ Polyn / Registr) and the FPGA PRS generator|Проверьте полином / регистры ПСП (ИОК_КОНФИГ Polyn / Registr) и генератор ПСП в ПЛИС
Check the Q baseband filter / balun and the ADMV1013 Q inputs|Проверьте фильтр НЧ / симметрирующий трансформатор Q и входы Q ADMV1013
Check the Q baseband path to the ADMV1013|Проверьте тракт Q НЧ до ADMV1013
Check the RF cable and the FSW centre frequency|Проверьте ВЧ-кабель и центральную частоту FSW
Check the RS encoder setting (K7/8 mode) in the FPGA|Проверьте настройку кодера RS (режим K7/8) в ПЛИС
Check the SCID stored in the BOS non-volatile memory|Проверьте SCID в энергонезависимой памяти БОС
Check the SP-L coding and the КБД framing in the FPGA|Проверьте кодирование SP-L и кадрирование КБД в ПЛИС
Check the TFG6806 frequency (10 MHz exactly; its own reference / warm-up)|Проверьте частоту TFG6806 (ровно 10 МГц; его опора / прогрев)
Check the TFG6806: output on, 10 MHz, level, cable to the BOS|Проверьте TFG6806: выход включён, 10 МГц, уровень, кабель до БОС
Check the attenuator code sent by the FPGA (bit stuck at 0?)|Проверьте код аттенюатора от ПЛИС (разряд залип в 0?)
Check the bench supply voltage / current limit|Проверьте напряжение / ограничение тока источника питания
Check the cable loss setting|Проверьте заданные потери кабеля
Check the cable loss setting and the connectors|Проверьте заданные потери кабеля и разъёмы
Check the idle frame generator in the FPGA|Проверьте генератор пустых кадров в ПЛИС
Check the interpolation setting of the DAC38J82|Проверьте коэффициент интерполяции DAC38J82
Check the modulation command / CONFIG|Проверьте команду / КОНФИГ модуляции
Check the output filter|Проверьте выходной фильтр
Check the reference oscillator accuracy|Проверьте точность опорного генератора
Check the reference oscillator frequency / tuning|Проверьте частоту / подстройку опорного генератора
Check the reference oscillator warm-up / external 10 MHz|Проверьте прогрев опорного генератора / внешние 10 МГц
Check the turbo encoder / randomizer / PRS 1, 2 settings in the FPGA|Проверьте турбокодер / рандомизатор / ПСП 1, 2 в ПЛИС
Check the КБД buffer (≥ 37 КБД) and the IPPD interface|Проверьте буфер КБД (≥ 37 КБД) и интерфейс ИППД
Check the УР data rate (symbol rate = 2 × rate)|Проверьте скорость УР (символьная скорость = 2 × скорость)
Compare at a higher attenuation (PA non-linearity disappears, filter problems stay)|Сравните при большем ослаблении (нелинейность УМ исчезает, ошибки фильтра остаются)
Compare with the DC/DC switching frequency|Сравните с частотой преобразования DC/DC
Connect the BOS CAN line on the CAN tab|Подключите линию CAN БОС на вкладке CAN
If it does not null: ADMV1013 LO feed-through / damaged device|Если не компенсируется: просачивание гетеродина ADMV1013 / повреждённая микросхема
If it stays: watch Err_Ref_LO / Sour_10MHz in the telemetry during the capture|Если сохранится: следите за Err_Ref_LO / Sour_10MHz в телеметрии во время записи
If |gain| > 1 dB: check the Q baseband path (DAC IOUTB, filter, resistors)|Если модуль усиления > 1 дБ: проверьте тракт Q НЧ (IOUTB ЦАП, фильтр, резисторы)
Increase the attenuation by 3…6 dB and re-measure: compression must disappear|Увеличьте ослабление на 3…6 дБ и повторите: компрессия должна исчезнуть
Inspect DAC38J82 BGA soldering / high-speed lanes (X-ray)|Проверьте пайку BGA DAC38J82 / скоростные линии (рентген)
Lock the FSW to the same 10 MHz as the BOS (TFG6806 -> FSW REF IN) and repeat|Синхронизируйте FSW от тех же 10 МГц, что и БОС (TFG6806 -> REF IN FSW), и повторите
Make the capture longer / check the packet type|Увеличьте длительность записи / проверьте тип посылки
Measure the internal oscillator (Err_Ref_LO, counter)|Измерьте внутренний генератор (Err_Ref_LO, частотомер)
Measure the {} V regulator output and its load|Измерьте выход стабилизатора {} В и его нагрузку
Note the offset: if it moves with the carrier setting it is a synthesizer spur|Запишите отстройку: если она смещается вместе с несущей — это побочная синтезатора
Note the offset: if it moves with the frequency code N it is a synthesizer spur|Запишите отстройку: если она смещается с кодом частоты N — это побочная синтезатора
Null it with the DAC38J82 QMC offset registers (see Corrections)|Скомпенсируйте регистрами смещения QMC DAC38J82 (см. «Поправки»)
Protocol control: the session ends without a valid target designation (3 s) or after Time_SNR - check the keep-alive option and the acknowledgements in the log|Управление по протоколу: сеанс завершается без действующего ЦУ (3 с) или по Time_SNR — проверьте опцию поддержки ЦУ и квитанции в журнале
Re-seat the RF cable|Переподключите ВЧ-кабель
Re-send МД_КОНФИГ and save it (SAVE_CONFIG_NEW)|Перешлите МД_КОНФИГ и сохраните (SAVE_CONFIG_NEW)
Reduce the drive (Atten_EIRP)|Уменьшите раскачку (Atten_EIRP)
Reduce the drive (attenuator code)|Уменьшите раскачку (код аттенюатора)
Repeat the attenuator test once the LMX2594 locks|Повторите проверку аттенюатора после захвата LMX2594
Repeat with SET_Sour_10MHz = internal: if the error disappears, it is the external reference; if it stays, it is the LMX2594 frequency plan|Повторите с SET_Sour_10MHz = внутренний: если ошибка исчезнет — виновата внешняя опора, если останется — частотный план LMX2594
Run the 'Carrier (CW)' test: integrated phase noise without modulation|Запустите проверку «Несущая (CW)»: интегральный фазовый шум без модуляции
Run the attenuator test|Запустите проверку аттенюатора
See the Corrections tab|См. вкладку «Поправки»
Send SET_Sour_10MHz = external (0x02, in standby) and save the КОНФИГ if needed|Отправьте SET_Sour_10MHz = внешний (0x02, в ДЖР) и при необходимости сохраните КОНФИГ
Set the IOK DOP period (telemetry period command) and re-run.|Задайте период ИОК_ДОП (команда периода телеметрии) и повторите.
Set the source with SET_Sour_10MHz (0x02) and save the КОНФИГ|Задайте источник командой SET_Sour_10MHz (0x02) и сохраните КОНФИГ
Switch off and check for shorts / PA oscillation|Выключите и проверьте на КЗ / самовозбуждение УМ
Then check the LMX2594 loop and the reference oscillator|Затем проверьте петлю LMX2594 и опорный генератор
Trim / replace the oscillator, or run on the external 10 MHz|Подстройте / замените генератор или работайте от внешних 10 МГц
Use a calibration table, or check for compression at low attenuation|Используйте калибровочную таблицу или проверьте компрессию при малом ослаблении
Use the ШР test (it triggers on the RF onset) or the preamble-only mode|Используйте проверку ШР (запуск по появлению ВЧ) или режим только преамбул
10 MHz source in use (Sour_10MHz)|Используемый источник 10 МГц (Sour_10MHz)
ACLR (worst side)|ACLR (худшая сторона)
ACLR at {}|ACLR при {}
Analysed part|Анализируемый участок
BOS telemetry (IOK DOP)|Телеметрия БОС (ИОК_ДОП)
Carrier frequency wander (10 ms windows)|Блуждание частоты несущей (окна 10 мс)
Carrier measured|Измеренная несущая
Carrier present|Несущая есть
Code-bit BER before turbo decoding|BER кодовых битов до турбодекодирования
Compression recovered with more attenuation|Снижение компрессии при большем ослаблении
Control link (NOP)|Связь управления (NOP)
Current, RF off (standby)|Ток, ВЧ выкл. (ДЖР)
Current, RF on (CW)|Ток, ВЧ вкл. (CW)
Demodulation|Демодуляция
Despread SNR per preamble bit|ОСШ после сжатия на бит преамбулы
Despread SNR per КБД bit|ОСШ после сжатия на бит КБД
Despreading efficiency (preamble PRS)|Эффективность сжатия (ПСП преамбулы)
Despreading efficiency (КБД PRS)|Эффективность сжатия (ПСП КБД)
EVM RMS|EVM СКЗ
End-of-preamble marker 0xDA2B1E5E|Маркер конца преамбулы 0xDA2B1E5E
External 10 MHz (Stat_10MHz)|Внешние 10 МГц (Stat_10MHz)
FPGA rail {} V|Питание ПЛИС {} В
File centre frequency|Центральная частота файла
Fit residual RMS|СКО остатка аппроксимации
Frequency drift (peak-peak)|Дрейф частоты (размах)
Gain compression (expected −{} dB step)|Компрессия усиления (ожидаемый шаг −{} дБ)
I/Q gain imbalance|Разбаланс усиления I/Q
Idle frame content|Содержимое пустых кадров
Idle frame content (RS-decoded frames)|Содержимое пустых кадров (кадры после RS)
Integrated phase noise {}…{} kHz|Интегральный фазовый шум {}…{} кГц
MER after I/Q correction|MER после коррекции I/Q
Max deviation from nominal|Макс. отклонение от номинала
Occupied bandwidth 99 %|Занимаемая полоса 99 %
PA compression (PAPR loss)|Компрессия УМ (потеря PAPR)
PA current rise|Рост тока УМ
PAPR loss at {}|Потеря PAPR при {}
Phase error RMS (after I/Q corr.)|СКО фазовой ошибки (после корр. I/Q)
Power drift (peak-peak)|Дрейф мощности (размах)
Preamble|Преамбула
Preambles found (PRS 3)|Найдено преамбул (ПСП 3)
SP-L (Manchester) violations|Нарушения SP-L (манчестер)
Session limit (SET_Time_SNR)|Ограничение сеанса (SET_Time_SNR)
Signal present|Сигнал есть
Standby (MODE_D)|ДЖР (MODE_D)
Telemetry age|Возраст телеметрии
Temperature change|Изменение температуры
Worst MER|Худший MER
error|ошибка
skipped|пропущено
{} dB step|Ступень {} дБ
ИОК_КОНФИГ checksum|Контрольная сумма ИОК_КОНФИГ
ИОК_КОНФИГ received|ИОК_КОНФИГ принят
КБД decoded (turbo 1/2)|Декодировано КБД (турбо 1/2)
КБД decoding|Декодирование КБД
УР data rate (detected)|Скорость УР (определена)
ШР despreading|Сжатие спектра ШР
Attenuator calibration table|Калибровочная таблица аттенюатора
Carrier leakage {} dBc. The demodulator's 90° rotation ambiguity means the sign / rail may be swapped: apply half, re-measure, keep the direction that lowers it.|Просачивание несущей {} дБн. Из-за неоднозначности поворота на 90° в демодуляторе знак / канал могут быть перепутаны: введите половину, повторите измерение и оставьте направление, при котором оно уменьшается.
Change Atten_EIRP by {} dB to reach {} dBm (now {} dB → {} dB).|Измените Atten_EIRP на {} дБ, чтобы получить {} дБм (сейчас {} дБ → {} дБ).
From the {} capture. If the symbol rate error has the same ppm, trim the 10 MHz reference; otherwise correct the LMX2594 output by {} Hz (LO {} GHz × {}).|По записи {}. Если ошибка символьной скорости имеет те же ppm — подстройте опору 10 МГц; иначе скорректируйте выход LMX2594 на {} Гц (гетеродин {} ГГц × {}).
I {} LSB, Q {} LSB|I {} МЗР, Q {} МЗР
I/Q gain balance (DAC38J82 QMC gain A/B)|Баланс усиления I/Q (QMC gain A/B DAC38J82)
LO leakage nulling (DAC38J82 QMC offset A/B)|Компенсация просачивания гетеродина (QMC offset A/B DAC38J82)
Q path is {} dB vs I. Change qmc_gainb by ≈ {} LSB (scale {} LSB = ×1.0, check the datasheet), re-measure.|Канал Q {} дБ относительно I. Измените qmc_gainb на ≈ {} МЗР (масштаб {} МЗР = ×1,0, сверьте с документацией), повторите измерение.
Quadrature (DAC38J82 QMC phase)|Квадратура (QMC phase DAC38J82)
Quadrature error {}°. Change qmc_phase by ≈ {} LSB ({} LSB/rad assumed), re-measure.|Ошибка квадратуры {}°. Измените qmc_phase на ≈ {} МЗР (принято {} МЗР/рад), повторите измерение.
Use the measured bit weights for a code→dB table, or replace the ADRF5730 if a bit has no effect.|Используйте измеренные веса разрядов для таблицы код→дБ или замените ADRF5730, если разряд не действует.
{} Hz ({} ppm)|{} Гц ({} ppm)
{} dB on Q relative to I|{} дБ в канале Q относительно I
{} dB vs nominal|{} дБ относительно номинала
ERROR|ОШИБКА
File {}|Файл {}
I/Q file: {}|Файл I/Q: {}
file|анализ файла
n/a: constant envelope (gain compression: PA-linearity test)|н/п: постоянная огибающая (компрессия усиления — проверка линейности УМ)
n/a: FSW bandwidth cuts the spectral regrowth (see PA-linearity gain compression)|н/п: полоса FSW обрезает рост спектра (см. компрессию усиления в проверке линейности УМ)
n/a (rectangular pulses)|н/п (прямоугольные импульсы)
n/a (capture narrower than 3 channels)|н/п (запись уже 3 каналов)
{} · preambles|{} · преамбулы
{} · КБД|{} · КБД
PASS|НОРМА
FAIL|НЕ НОРМА
INFO|ИНФО
SKIP|ПРОПУЩЕНО
PASS*|НОРМА*
ERROR - {}|ОШИБКА - {}
SKIP - {}|ПРОПУЩЕНО - {}
FAIL - {}|НЕ НОРМА - {}
PASS - {}|НОРМА - {}
INFO - {}|ИНФО - {}
NO SIGNAL|НЕТ СИГНАЛА
OK|НОРМА
UNLOCKED|НЕ ЗАХВАЧЕН
found|найден
locked|захвачен
max {} Hz/s over {} s|макс. {} Гц/с за {} с
missing / invalid|нет / неверен
nearest channel n = {} = {} GHz|ближайший канал n = {} = {} ГГц
no BOS arrays received|массивы БОС не приняты
none found|не найдено
not in capture|нет в записи
not received|не принят
skipped: {} kbit/s < {} kbit/s (КБД = {} M chips)|пропущено: {} кбит/с < {} кбит/с (КБД = {} млн чипов)
synchromarker 0x1ACFFC1D not found|синхромаркер 0x1ACFFC1D не найден
valid|в норме
{} bits|{} бит
{} bits, < {} (95 %)|{} бит, < {} (95 %)
{} kbit/s, preamble {}|{} кбит/с, преамбула {}
{} … {} s of the recording|{} … {} с записи
{}, OBW {} MHz|{}, занимаемая полоса {} МГц
{}/{} ok|{}/{} в норме
КВ+ via CAN{}|КВ+ по CAN{}
{} fault(s), {} warning(s); look at {}|неисправностей: {}, замечаний: {}; проверьте: {}
{} fault(s), {} warning(s)|неисправностей: {}, замечаний: {}
Finished: {} — {}|Завершено: {} — {}
10 MHz reference not ready before {} ({} s): {}; required PulsNumb_10MHz 10 000 000 ± {} and |Err_Ref_LO| ≤ {}. Mode command not sent.|=|Опора 10 МГц не готова перед {} ({} с): {}; требуется PulsNumb_10MHz 10 000 000 ± {} и |Err_Ref_LO| ≤ {}. Команда режима не отправлена.
{}: waiting for the 10 MHz reference before {} (PulsNumb {}, Err_Ref_LO {}) …|{}: ожидание опоры 10 МГц перед {} (PulsNumb {}, Err_Ref_LO {}) …
TFG6806 not connected (TFG6806 tab) - it must give the BOS its 10 MHz before the BOS is powered|TFG6806 не подключён (вкладка TFG6806) - он должен подать 10 МГц до включения питания БОС
Reference before mode command|Опора перед командой режима
TFG6806 clock source (UTILity:CLOck:SOURce?)|Источник опоры TFG6806 (UTILity:CLOck:SOURce?)
TFG6806 CH{} frequency (10 MHz to the BOS)|Частота TFG6806 CH{} (10 МГц в БОС)
TFG6806 CH{} output|Выход TFG6806 CH{}
TFG6806 CH{} waveform|Форма сигнала TFG6806 CH{}
TFG6806 CH{} amplitude|Амплитуда TFG6806 CH{}
TFG6806 CH{} offset|Смещение TFG6806 CH{}
TFG6806 internal 10 MHz on the rear port (UTILity:INSide:CLOck?)|Внутренние 10 МГц TFG6806 на заднем разъёме (UTILity:INSide:CLOck?)
setting only: the TFG has no query for 'external clock present'; without a qualified clock it runs on its internal one (status bar 'Ext Ref On')|только настройка: у TFG нет запроса «внешняя опора есть»; без годного сигнала он работает от внутренней (строка состояния «Ext Ref On»)
BOS power on|Питание БОС включено
CAN{} ({}): command + КВ|CAN{} ({}): команда + КВ
CAN{} ({}): telemetry (REQ_TM ИОК_ОСН)|CAN{} ({}): телеметрия (REQ_TM ИОК_ОСН)
КВ+ on {}, via CAN{}|КВ+ по {}, через CAN{}
КВ+ on this line|КВ+ по этой линии
ИОК_ОСН on {}|ИОК_ОСН по {}
not received within 3 s|не получен за 3 с
acknowledgement on another line / wrong 'via CAN' bit|квитанция по другой линии / неверный признак «через CAN»
SET_CAN (transmission via CAN1 only)|SET_CAN (передача только по CAN1)
Further commands|Дальнейшие команды
via CAN1 only ({})|только через CAN1 ({})
no КВ|нет КВ
{}: {} V, limit {} A|{}: {} В, ограничение {} А
10 MHz pulses present (PulsNumb_10MHz)|Импульсы 10 МГц есть (PulsNumb_10MHz)
10 MHz pulses (PulsNumb_10MHz)|Импульсы 10 МГц (PulsNumb_10MHz)
External 10 MHz valid (Stat_10MHz)|Внешние 10 МГц исправны (Stat_10MHz)
ИОК_ОСН after power-up|ИОК_ОСН после включения
BOS not powered / booting, telemetry off, or wrong CAN line|БОС не включён / загружается, телеметрия выключена или неверная линия CAN
internal reference expected|ожидается внутренняя опора
10 MHz reference frequency off|Частота опоры 10 МГц вне нормы
PulsNumb_10MHz = {} pulses per second (required 10 000 000 ± {}, i.e. ±{} ppm): the 10 MHz given to the BOS is {} ppm off. Mode commands are not sent until it is inside.|PulsNumb_10MHz = {} импульсов в секунду (требуется 10 000 000 ± {}, т. е. ±{} ppm): опора 10 МГц для БОС смещена на {} ppm. Команды режима не отправляются, пока она вне нормы.
Check the TFG6806: CH output frequency exactly 10 MHz, its own reference (Ext Ref On in the status bar)|Проверьте TFG6806: частота выхода CH ровно 10 МГц, его собственная опора («Ext Ref On» в строке состояния)
Check the 10 MHz cable / level at the BOS|Проверьте кабель / уровень 10 МГц на входе БОС
Err_Ref_LO = {} (required |Err_Ref_LO| ≤ {} before a mode command): the BOS internal oscillator has not settled on the 10 MHz reference.|=|Err_Ref_LO = {} (перед командой режима требуется |Err_Ref_LO| ≤ {}): внутренний генератор БОС не подстроился под опору 10 МГц.
Wait longer after power-up (Reference wait, Unit control page)|Увеличьте ожидание после включения («Ожидание опоры», страница «Управление блоком»)
Analysing {} …{}|Анализ {} …{}
Analysing {} …|Анализ {} …
<b>Findings</b> — {} fault(s), {} warning(s)|<b>Выводы</b> — неисправностей: {}, замечаний: {}
<b>Findings</b> — {} fault(s), {} warning(s) — filtered: {}|<b>Выводы</b> — неисправностей: {}, замечаний: {} — фильтр: {}
Test {}/{}: {}|Проверка {}/{}: {}
{}: capturing {} …|{}: запись {} …
"""

# captured values that are words
VALUES = {
    "connectors, cable, FSW": "разъёмы, кабель, FSW",
    "external": "внешний", "internal": "внутренний", "correct": "в норме", "not conclusive": "неопределённа",
    "preamble": "преамбулы", "КБД": "КБД", "phase noise": "фазовый шум", "compression": "компрессия",
    "carrier leakage": "просачивание несущей", "I/Q imbalance": "разбаланс I/Q",
    "low signal-to-noise at the FSW": "низкое ОСШ на входе FSW", "nominal": "номинале",
"PASS": "НОРМА", "PASS*": "НОРМА*", "FAIL": "НЕ НОРМА",
    "ABORTED": "ПРЕРВАНО",
    "carrier (CW)": "несущая (CW)", "narrowband (УР, BPSK)": "узкополосный (УР, BPSK)",
    "wideband (ШР, OQPSK)": "широкополосный (ШР, OQPSK)", "no signal": "нет сигнала",
    "Carrier": "несущей", "OQPSK": "OQPSK", "BPSK": "BPSK", "mod_BPSK": "BPSK", "ber": "проверка BER",
    "1/4 × carrier (LO)": "1/4 × несущей (гетеродин)",
}

_ENTRIES = []
for _ln in T.strip().split("\n"):
    if "|" not in _ln:
        continue
    if "|=|" in _ln:                                     # texts that contain "|" themselves
        _en, _ru = _ln.split("|=|", 1)
    else:
        _en, _ru = _ln.split("|", 1) if _ln.count("|") == 1 else _ln.rsplit("|", 1)
    _rx = "^" + re.escape(_en).replace(r"\{\}", "(.*?)") + "$"
    _ENTRIES.append((re.compile(_rx, re.S), _ru, _en.count("{}")))
# specific before generic: templates with a longer fixed beginning, then more fixed text, are tried first
def _prio(en):
    lead = len(en.split("{}")[0])
    return (-lead, -len(en.replace("{}", "")))


_ENTRIES.sort(key=lambda e: _prio(e[0].pattern.replace("(.*?)", "{}").replace("\\", "")))
_EXACT = {e.pattern: r for e, r, n in _ENTRIES if n == 0}


def _value(v: str) -> str:
    if v in VALUES:
        return VALUES[v]
    try:
        import i18n
        t = i18n.tr(v, "ru")
        if t != v:
            return t
    except Exception:
        pass
    if ", " in v and len(v) < 200:                         # lists ("phase noise, compression")
        parts = re.split(r"(?<=\)), ", v) if "(" in v else v.split(", ")
        if len(parts) == 1:
            parts = None
        tp = [_value(p) for p in parts] if parts else None
        if parts and tp != parts:
            return ", ".join(tp)
    # block names "Power amp (UMS CHA3397)" -> translated name, part kept
    m = re.match(r"^([^(]*?) \((.*)\)$", v)
    if m:
        try:
            import i18n
            n = i18n.tr(m.group(1), "ru")
            if n != m.group(1):
                return f"{n} ({VALUES.get(m.group(2), m.group(2))})"
        except Exception:
            pass
    return v


def ru(text: str) -> str:
    """Russian for an English diagnosis / report text, or the text itself."""
    if not text:
        return text
    for rx, tpl, n in _ENTRIES:
        m = rx.match(text)
        if m:
            vals = [_value(g) for g in m.groups()]
            out = tpl
            for v in vals:
                out = out.replace("{}", v, 1)
            return out
    m = re.match(r"^([^()]*?) \(([^()]*)\)$", text)      # block names "Power amp (UMS CHA3397)"
    if m:
        try:
            import i18n
            n = i18n.TABLE.get(m.group(1), {}).get("ru")
            if n:
                return f"{n} ({VALUES.get(m.group(2), m.group(2))})"
        except Exception:
            pass
    if "), " in text and len(text) < 300:                  # list of block names
        parts = re.split(r"(?<=\)), ", text)
        tp = [ru(p) for p in parts]
        if tp != parts:
            return ", ".join(tp)
    return text


UNITS = {"dB": "дБ", "dBm": "дБм", "dBc": "дБн", "Hz": "Гц", "kHz": "кГц", "MHz": "МГц", "GHz": "ГГц", "s": "с",
         "V": "В", "A": "А", "kbit/s": "кбит/с", "°C": "°C", "° RMS": "° СКЗ", "Hz p-p": "Гц размах", "%": "%",
         "ppm": "ppm", "ksym/s": "кСимв/с", "Msym/s": "МСимв/с"}


# HTML report: fixed texts
REPORT = {
    "BOS test report": "Отчёт об испытании БОС",
    "BOS modulator test report": "Отчёт об испытании модулятора БОС",
    "Verdict": "Заключение",
    "Unit serial": "Заводской номер", "Operator": "Оператор", "Date": "Дата", "Duration": "Длительность",
    "Test plan": "План проверки", "Tests": "Проверки", "Carrier / symbol rates": "Несущая / символьные скорости",
    "Nominal power / att. code / cable loss": "Номинальная мощность / код атт. / потери кабеля", "FSW": "FSW",
    "Mode": "Режим", "Diagnosis": "Диагноз", "Measurements": "Измерения", "Test": "Проверка",
    "Parameter": "Параметр", "Value": "Значение", "Unit": "Ед.", "Limit": "Допуск", "Result": "Результат",
    "Suggested corrections": "Предлагаемые поправки", "Adjustment": "Регулировка", "How": "Как",
    "BOS telemetry at the end of the test": "Телеметрия БОС в конце проверки", "Signal": "Сигнал",
    "Attenuator": "Аттенюатор", "Most likely location": "Наиболее вероятное место", "What to do": "Что делать",
    "source": "источник", "power": "мощность", "telemetry": "телеметрия", "attenuator": "аттенюатор",
    "linearity": "линейность", "stability": "стабильность", "supply": "питание", "link": "связь", "PASS": "НОРМА", "FAIL": "НЕ НОРМА", "INFO": "ИНФО", "SKIP": "ПРОПУЩЕНО",
    "ERROR": "ОШИБКА", "WARN": "ЗАМЕЧАНИЕ", "PASS*": "НОРМА*", "ABORTED": "ПРЕРВАНО",
}
