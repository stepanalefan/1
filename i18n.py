# -*- coding: utf-8 -*-
"""
i18n.py
=======
User-interface languages of KTR TEST: the languages of the G8 countries -
English (USA, UK, Canada), French (France, Canada), German, Italian, Japanese, Russian.

The program is written in English. `Translator` walks the widget tree and replaces texts it finds
in TABLE (labels, buttons, check boxes, group titles, tab titles, table headers, placeholders,
combo-box items that carry item data, short table cells). The English original is kept per widget,
so switching back works, and texts that the program changes later (status, "▶ Run" / "■ Stop" …)
are translated again by a periodic pass. Technical identifiers (telemetry field names, SCPI,
model names, units, CAN IDs) are not in the table and stay as they are.
The choice is stored in ui_language.txt next to the program (Help tab -> Language).
"""

from __future__ import annotations

import os
import re
import sys

from PyQt5 import QtCore, QtWidgets

LANGS = [("en", "English"), ("fr", "Français"), ("de", "Deutsch"), ("it", "Italiano"), ("ja", "日本語"),
         ("ru", "Русский")]
_ORDER = ("fr", "de", "it", "ja", "ru")

# English -> "French|German|Italian|Japanese|Russian"
_RAW = r"""
BOS Test Station|Station de test BOS|BOS-Teststation|Stazione di test BOS|BOS試験ステーション|Испытательная станция БОС
Main|Principal|Übersicht|Principale|メイン|Главная
Spectrum Analyzer|Analyseur de spectre|Spektrumanalysator|Analizzatore di spettro|スペクトラムアナライザ|Анализатор спектра
CAN|CAN|CAN|CAN|CAN|CAN
Telemetry|Télémesure|Telemetrie|Telemetria|テレメトリ|Телеметрия
Logs|Journaux|Protokolle|Log|ログ|Журналы
Help|Aide|Hilfe|Guida|ヘルプ|Справка
Unit|Unité|Gerät|Unità|装置|Блок
operator|opérateur|Bediener|operatore|オペレータ|оператор
unit serial number|numéro de série|Seriennummer|numero di serie|製造番号|заводской номер
Plan|Plan|Plan|Piano|計画|План
Plans ▾|Plans ▾|Pläne ▾|Piani ▾|計画 ▾|Планы ▾
▶ Run test plan|▶ Lancer le plan|▶ Testplan starten|▶ Avvia piano di test|▶ 試験開始|▶ Запустить план
■ Abort|■ Interrompre|■ Abbrechen|■ Interrompi|■ 中止|■ Прервать
◎ Live diagnose|◎ Diagnostic en direct|◎ Live-Diagnose|◎ Diagnosi live|◎ ライブ診断|◎ Диагностика онлайн
📂 Analyze I/Q file…|📂 Analyser un fichier I/Q…|📂 I/Q-Datei analysieren…|📂 Analizza file I/Q…|📂 I/Qファイル解析…|📂 Анализ файла I/Q…
NOT TESTED|NON TESTÉ|NICHT GEPRÜFT|NON TESTATO|未試験|НЕ ИСПЫТАН
RUNNING|EN COURS|LÄUFT|IN CORSO|実行中|ИДЁТ
PASS (warnings)|CONFORME (avert.)|BESTANDEN (Warn.)|SUPERATO (avvisi)|合格(警告)|НОРМА (замеч.)
ABORTED|INTERROMPU|ABGEBROCHEN|INTERROTTO|中止|ПРЕРВАН
Ready. Connect the FSW (Spectrum Analyzer tab) and the BOS CAN line (CAN tab).|Prêt. Connectez le FSW (onglet Analyseur de spectre) et la ligne CAN du BOS (onglet CAN).|Bereit. FSW (Reiter Spektrumanalysator) und BOS-CAN-Leitung (Reiter CAN) verbinden.|Pronto. Collegare l'FSW (scheda Analizzatore di spettro) e la linea CAN del BOS (scheda CAN).|準備完了。FSW(スペクトラムアナライザタブ)とBOSのCANライン(CANタブ)を接続してください。|Готово. Подключите FSW (вкладка «Анализатор спектра») и линию CAN БОС (вкладка CAN).
Unit / signal|Unité / signal|Gerät / Signal|Unità / segnale|装置/信号|Блок / сигнал
Limits|Limites|Grenzwerte|Limiti|規格値|Допуски
Unit control|Commande de l'unité|Gerätesteuerung|Controllo unità|装置制御|Управление блоком
Simulator|Simulateur|Simulator|Simulatore|シミュレータ|Имитатор
Diagnosis|Diagnostic|Diagnose|Diagnosi|診断|Диагноз
Measurements|Mesures|Messwerte|Misure|測定値|Измерения
Signal|Signal|Signal|Segnale|信号|Сигнал
Attenuator|Atténuateur|Dämpfungsglied|Attenuatore|減衰器|Аттенюатор
Frames|Trames|Rahmen|Trame|フレーム|Кадры
BOS telemetry|Télémesure BOS|BOS-Telemetrie|Telemetria BOS|BOSテレメトリ|Телеметрия БОС
Corrections|Corrections|Korrekturen|Correzioni|補正|Поправки
History|Historique|Verlauf|Cronologia|履歴|История
Findings|Constats|Befunde|Rilievi|所見|Выводы
Open last report|Ouvrir le dernier rapport|Letzten Bericht öffnen|Apri ultimo rapporto|最新レポートを開く|Открыть последний отчёт
Open report|Ouvrir le rapport|Bericht öffnen|Apri rapporto|レポートを開く|Открыть отчёт
Reports folder|Dossier des rapports|Berichtsordner|Cartella rapporti|レポートフォルダ|Папка отчётов
Reload|Recharger|Neu laden|Ricarica|再読込|Обновить
Test|Test|Prüfung|Test|試験|Проверка
Parameter|Paramètre|Parameter|Parametro|パラメータ|Параметр
Value|Valeur|Wert|Valore|値|Значение
Limit|Limite|Grenzwert|Limite|規格|Допуск
Result|Résultat|Ergebnis|Risultato|結果|Результат
Capture|Acquisition|Aufnahme|Acquisizione|取得データ|Запись
Adjustment|Réglage|Einstellung|Regolazione|調整項目|Регулировка
How|Comment|Vorgehen|Come|方法|Как
Copy to clipboard|Copier dans le presse-papiers|In die Zwischenablage|Copia negli appunti|クリップボードにコピー|Копировать в буфер
Nominal|Nominal|Nennwert|Nominale|公称|Номинал
Measured|Mesuré|Gemessen|Misurato|測定|Измерено
Error|Erreur|Fehler|Errore|誤差|Ошибка
State|État|Zustand|Stato|状態|Состояние
Carrier frequency|Fréquence porteuse|Trägerfrequenz|Frequenza portante|搬送波周波数|Частота несущей
Symbol rate BPSK (УР)|Débit symbole BPSK (УР)|Symbolrate BPSK (УР)|Symbol rate BPSK (УР)|シンボルレート BPSK (УР)|Символьная скорость BPSK (УР)
Symbol rate OQPSK (ШР)|Débit symbole OQPSK (ШР)|Symbolrate OQPSK (ШР)|Symbol rate OQPSK (ШР)|シンボルレート OQPSK (ШР)|Символьная скорость OQPSK (ШР)
OQPSK RRC roll-off|Facteur de retombée RRC OQPSK|OQPSK-RRC-Roll-off|Roll-off RRC OQPSK|OQPSK RRCロールオフ|Коэффициент скругления RRC OQPSK
BPSK rectangular pulses (SP-L)|Impulsions rectangulaires BPSK (SP-L)|BPSK-Rechteckimpulse (SP-L)|Impulsi rettangolari BPSK (SP-L)|BPSK矩形パルス (SP-L)|Прямоугольные импульсы BPSK (SP-L)
OQPSK RRC filtered|OQPSK filtré RRC|OQPSK RRC-gefiltert|OQPSK filtrato RRC|OQPSK RRCフィルタ|OQPSK с фильтром RRC
Samples / symbol BPSK|Échantillons / symbole BPSK|Abtastwerte / Symbol BPSK|Campioni / simbolo BPSK|サンプル/シンボル BPSK|Отсчётов на символ BPSK
OQPSK capture sample rate|Fréquence d'échantillonnage OQPSK|OQPSK-Abtastrate|Frequenza di campionamento OQPSK|OQPSKサンプルレート|Частота дискретизации OQPSK
FSW bandwidth from options|Bande FSW d'après les options|FSW-Bandbreite aus Optionen|Banda FSW dalle opzioni|オプションからFSW帯域|Полоса FSW по опциям
FSW I/Q bandwidth|Bande I/Q du FSW|FSW-I/Q-Bandbreite|Banda I/Q dell'FSW|FSW I/Q帯域幅|Полоса I/Q FSW
Symbols per capture|Symboles par acquisition|Symbole pro Aufnahme|Simboli per acquisizione|取得あたりシンボル数|Символов в записи
Decode УР data (BPSK)|Décoder les données УР (BPSK)|УР-Daten dekodieren (BPSK)|Decodifica dati УР (BPSK)|УРデータ復号 (BPSK)|Декодировать данные УР (BPSK)
ШР: decode КБД from rate|ШР : décoder КБД à partir de|ШР: КБД dekodieren ab|ШР: decodifica КБД da|ШР: КБД復号の下限レート|ШР: декодировать КБД со скорости
Save every capture (.iq.tar)|Enregistrer chaque acquisition (.iq.tar)|Jede Aufnahme speichern (.iq.tar)|Salva ogni acquisizione (.iq.tar)|全取得データを保存 (.iq.tar)|Сохранять каждую запись (.iq.tar)
Spacecraft ID (SCID)|Identifiant du satellite (SCID)|Satelliten-ID (SCID)|ID veicolo spaziale (SCID)|宇宙機ID (SCID)|Идентификатор КА (SCID)
BER test: bits|Test TEB : bits|BER-Test: Bits|Test BER: bit|BER試験:ビット数|Проверка BER: бит
ADMV1013 LO multiplier|Multiplicateur OL ADMV1013|ADMV1013-LO-Vervielfacher|Moltiplicatore LO ADMV1013|ADMV1013 LO逓倍数|Умножитель гетеродина ADMV1013
Nominal output power|Puissance de sortie nominale|Nenn-Ausgangsleistung|Potenza di uscita nominale|公称出力|Номинальная выходная мощность
Nominal Atten_EIRP|Atten_EIRP nominal|Nenn-Atten_EIRP|Atten_EIRP nominale|公称 Atten_EIRP|Номинальный Atten_EIRP
Cable / fixture loss|Pertes câble / montage|Kabel-/Adapterdämpfung|Perdita cavo / attrezzatura|ケーブル/治具損失|Потери кабеля / оснастки
External 10 MHz expected|10 MHz externe attendu|Externe 10 MHz erwartet|10 MHz esterno previsto|外部10 MHz使用|Ожидается внешние 10 МГц
FSW auto level per capture|Niveau auto FSW à chaque acquisition|FSW-Autopegel je Aufnahme|Livello auto FSW per acquisizione|取得ごとにFSW自動レベル|Автоуровень FSW для каждой записи
FSW ref level (no auto)|Niveau de réf. FSW (sans auto)|FSW-Referenzpegel (ohne Auto)|Livello rif. FSW (senza auto)|FSW基準レベル(自動なし)|Опорный уровень FSW (без авто)
CW capture bandwidth|Bande d'acquisition CW|CW-Aufnahmebandbreite|Banda di acquisizione CW|CW取得帯域|Полоса записи CW
Phase noise from|Bruit de phase de|Phasenrauschen von|Rumore di fase da|位相雑音 下限|Фазовый шум от
Phase noise to|Bruit de phase à|Phasenrauschen bis|Rumore di fase a|位相雑音 上限|Фазовый шум до
Stability: measurements|Stabilité : mesures|Stabilität: Messungen|Stabilità: misure|安定度:測定回数|Стабильность: измерений
Stability: interval|Stabilité : intervalle|Stabilität: Intervall|Stabilità: intervallo|安定度:間隔|Стабильность: интервал
Linearity: extra attenuation|Linéarité : atténuation supplémentaire|Linearität: zusätzliche Dämpfung|Linearità: attenuazione aggiuntiva|直線性:追加減衰|Линейность: доп. ослабление
QMC gain LSB per ×1.0|QMC gain LSB pour ×1,0|QMC-Verstärkung LSB je ×1,0|QMC guadagno LSB per ×1,0|QMC利得 LSB/×1.0|QMC усиление, МЗР на ×1,0
QMC phase LSB per rad|QMC phase LSB par rad|QMC-Phase LSB je rad|QMC fase LSB per rad|QMC位相 LSB/rad|QMC фаза, МЗР на рад
QMC offset LSB per RMS|QMC offset LSB par RMS|QMC-Offset LSB je RMS|QMC offset LSB per RMS|QMCオフセット LSB/RMS|QMC смещение, МЗР на СКЗ
BOS supply output|Sortie d'alimentation du BOS|BOS-Versorgungsausgang|Uscita di alimentazione BOS|BOS電源出力|Выход питания БОС
MER min|MER min|MER min.|MER min|MER 最小|MER мин.
EVM max|EVM max|EVM max.|EVM max|EVM 最大|EVM макс.
Output power tolerance ±|Tolérance de puissance ±|Leistungstoleranz ±|Tolleranza di potenza ±|出力許容差 ±|Допуск мощности ±
Carrier leakage max|Fuite de porteuse max|Trägerdurchgriff max.|Dispersione portante max|キャリアリーク 最大|Просачивание несущей макс.
I/Q gain imbalance max|Déséquilibre de gain I/Q max|I/Q-Verstärkungsungleichheit max.|Squilibrio guadagno I/Q max|I/Q利得不平衡 最大|Разбаланс усиления I/Q макс.
Quadrature error max|Erreur de quadrature max|Quadraturfehler max.|Errore di quadratura max|直交誤差 最大|Ошибка квадратуры макс.
Phase error RMS max|Erreur de phase RMS max|Phasenfehler RMS max.|Errore di fase RMS max|位相誤差RMS 最大|СКО фазовой ошибки макс.
PA compression max (PAPR loss)|Compression PA max (perte PAPR)|PA-Kompression max. (PAPR-Verlust)|Compressione PA max (perdita PAPR)|PA圧縮 最大(PAPR低下)|Компрессия УМ макс. (потеря PAPR)
ACLR min|ACLR min|ACLR min.|ACLR min|ACLR 最小|ACLR мин.
Spectrum asymmetry max|Asymétrie du spectre max|Spektrumasymmetrie max.|Asimmetria spettro max|スペクトル非対称 最大|Асимметрия спектра макс.
Spur max|Raie parasite max|Nebenlinie max.|Spuria max|スプリアス 最大|Побочная составляющая макс.
CW integrated phase noise max|Bruit de phase intégré CW max|CW integr. Phasenrauschen max.|Rumore di fase integrato CW max|CW積分位相雑音 最大|Интегр. фазовый шум CW макс.
Attenuator bit error max|Erreur de bit d'atténuateur max|Dämpfungsbit-Fehler max.|Errore bit attenuatore max|減衰器ビット誤差 最大|Ошибка разряда аттенюатора макс.
LO-related lines max|Raies liées à l'OL max|LO-bezogene Linien max.|Righe legate al LO max|LO関連スプリアス 最大|Составляющие гетеродина макс.
PA current rise min (RF on)|Hausse du courant PA min (RF actif)|PA-Stromanstieg min. (HF an)|Aumento corrente PA min (RF on)|PA電流増加 最小(RFオン)|Рост тока УМ мин. (ВЧ вкл.)
Supply current max, session (TZ, 14 V)|Courant max en session (CdC, 14 V)|Strom max., Sitzung (TZ, 14 V)|Corrente max, sessione (TZ, 14 V)|電流最大・セッション(TZ, 14 V)|Ток макс., сеанс (ТЗ, 14 В)
Supply current max, standby (TZ, 14 V)|Courant max en veille (CdC, 14 V)|Strom max., Bereitschaft (TZ, 14 V)|Corrente max, standby (TZ, 14 V)|電流最大・待機(TZ, 14 V)|Ток макс., дежурный (ТЗ, 14 В)
FPGA rail tolerance ±|Tolérance des rails FPGA ±|FPGA-Spannungstoleranz ±|Tolleranza alimentazioni FPGA ±|FPGA電源許容差 ±|Допуск питания ПЛИС ±
Temperature max|Température max|Temperatur max.|Temperatura max|温度 最大|Температура макс.
Power drift max|Dérive de puissance max|Leistungsdrift max.|Deriva di potenza max|電力ドリフト 最大|Дрейф мощности макс.
Frequency drift max|Dérive de fréquence max|Frequenzdrift max.|Deriva di frequenza max|周波数ドリフト 最大|Дрейф частоты макс.
Telemetry stale after|Télémesure périmée après|Telemetrie veraltet nach|Telemetria obsoleta dopo|テレメトリ無効までの時間|Телеметрия устарела через
Restore default limits|Rétablir les limites par défaut|Standard-Grenzwerte wiederherstellen|Ripristina limiti predefiniti|規格値を既定に戻す|Восстановить допуски по умолчанию
Control method|Méthode de commande|Steuerungsart|Metodo di controllo|制御方式|Способ управления
BOS protocol (ПИЛВ, direct CAN commands)|Protocole BOS (ПИЛВ, commandes CAN directes)|BOS-Protokoll (ПИЛВ, direkte CAN-Befehle)|Protocollo BOS (ПИЛВ, comandi CAN diretti)|BOSプロトコル(ПИЛВ, CAN直接コマンド)|Протокол БОС (ПИЛВ, прямые команды CAN)
Commands from the CAN library|Commandes de la bibliothèque CAN|Befehle aus der CAN-Bibliothek|Comandi dalla libreria CAN|CANライブラリのコマンド|Команды из библиотеки CAN
Ask the operator|Demander à l'opérateur|Bediener fragen|Chiedi all'operatore|オペレータに確認|Запросить оператора
BOS protocol|Protocole BOS|BOS-Protokoll|Protocollo BOS|BOSプロトコル|Протокол БОС
Sender address|Adresse d'émetteur|Absenderadresse|Indirizzo mittente|送信元アドレス|Адрес отправителя
CAN line|Ligne CAN|CAN-Leitung|Linea CAN|CANライン|Линия CAN
Frequency code N|Code de fréquence N|Frequenzcode N|Codice di frequenza N|周波数コード N|Код частоты N
BPSK signal from|Signal BPSK par|BPSK-Signal aus|Segnale BPSK da|BPSK信号の送信コマンド|Сигнал BPSK по команде
OQPSK signal from|Signal OQPSK par|OQPSK-Signal aus|Segnale OQPSK da|OQPSK信号の送信コマンド|Сигнал OQPSK по команде
УР (BPSK) rate|Débit УР (BPSK)|УР-Rate (BPSK)|Velocità УР (BPSK)|УР (BPSK) 速度|Скорость УР (BPSK)
ШР (OQPSK) rate|Débit ШР (OQPSK)|ШР-Rate (OQPSK)|Velocità ШР (OQPSK)|ШР (OQPSK) 速度|Скорость ШР (OQPSK)
Session limit|Limite de session|Sitzungsgrenze|Limite di sessione|セッション制限|Ограничение сеанса
Send target designation every 0.5 s (0°, 0°)|Envoyer la désignation d'objectif toutes les 0,5 s (0°, 0°)|Zielzuweisung alle 0,5 s senden (0°, 0°)|Invia designazione ogni 0,5 s (0°, 0°)|0.5秒ごとに目標指示を送信 (0°, 0°)|Отправлять ЦУ каждые 0,5 с (0°, 0°)
Enable ИОК_ДОП / ОСН / СОСТОЯНИЕ and read ИОК_КОНФИГ|Activer ИОК_ДОП / ОСН / СОСТОЯНИЕ et lire ИОК_КОНФИГ|ИОК_ДОП / ОСН / СОСТОЯНИЕ aktivieren und ИОК_КОНФИГ lesen|Attiva ИОК_ДОП / ОСН / СОСТОЯНИЕ e leggi ИОК_КОНФИГ|ИОК_ДОП/ОСН/СОСТОЯНИЕを有効化しИОК_КОНФИГを読む|Включить ИОК_ДОП / ОСН / СОСТОЯНИЕ и прочитать ИОК_КОНФИГ
Test link|Tester la liaison|Verbindung prüfen|Prova collegamento|リンク試験|Проверить связь
RF off (standby)|RF coupé (veille)|HF aus (Bereitschaft)|RF off (standby)|RFオフ(待機)|ВЧ выкл. (ДЖР)
Modes (commands from the CAN library)|Modes (commandes de la bibliothèque CAN)|Betriebsarten (Befehle aus der CAN-Bibliothek)|Modi (comandi dalla libreria CAN)|モード(CANライブラリのコマンド)|Режимы (команды из библиотеки CAN)
Reload CAN library|Recharger la bibliothèque CAN|CAN-Bibliothek neu laden|Ricarica libreria CAN|CANライブラリ再読込|Перечитать библиотеку CAN
(ask operator)|(demander à l'opérateur)|(Bediener fragen)|(chiedi all'operatore)|(オペレータに確認)|(запросить оператора)
RF off|RF coupé|HF aus|RF off|RFオフ|ВЧ выкл.
Carrier (CW)|Porteuse (CW)|Träger (CW)|Portante (CW)|搬送波 (CW)|Несущая (CW)
Attenuator control (library / operator method)|Commande de l'atténuateur (bibliothèque / opérateur)|Dämpfungssteuerung (Bibliothek / Bediener)|Controllo attenuatore (libreria / operatore)|減衰器制御(ライブラリ/オペレータ)|Управление аттенюатором (библиотека / оператор)
CAN command template|Modèle de commande CAN|CAN-Befehlsvorlage|Modello di comando CAN|CANコマンド雛形|Шаблон команды CAN
Not adjustable (skip)|Non réglable (ignorer)|Nicht einstellbar (überspringen)|Non regolabile (salta)|調整不可(スキップ)|Не регулируется (пропустить)
Control|Commande|Steuerung|Controllo|制御|Управление
Data|Données|Daten|Dati|データ|Данные
Timing|Temporisation|Zeitverhalten|Temporizzazione|タイミング|Временные параметры
Settle time|Temps de stabilisation|Einschwingzeit|Tempo di assestamento|安定待ち時間|Время установления
Clear all faults|Effacer tous les défauts|Alle Fehler löschen|Cancella tutti i guasti|全故障をクリア|Сбросить все неисправности
Random fault (blind test)|Défaut aléatoire (test à l'aveugle)|Zufallsfehler (Blindtest)|Guasto casuale (test alla cieca)|ランダム故障(ブラインド試験)|Случайная неисправность (слепой тест)
Hide idle frames|Masquer les trames de remplissage|Leerrahmen ausblenden|Nascondi trame vuote|アイドルフレームを隠す|Скрыть пустые кадры
Save frames (.bin)|Enregistrer les trames (.bin)|Rahmen speichern (.bin)|Salva trame (.bin)|フレーム保存 (.bin)|Сохранить кадры (.bin)
Save list (.csv)|Enregistrer la liste (.csv)|Liste speichern (.csv)|Salva elenco (.csv)|一覧保存 (.csv)|Сохранить список (.csv)
Save raw bit stream (.bin)|Enregistrer le flux binaire brut (.bin)|Rohen Bitstrom speichern (.bin)|Salva flusso di bit grezzo (.bin)|生ビット列保存 (.bin)|Сохранить исходный поток бит (.bin)
Format|Format|Format|Formato|形式|Формат
Highlight changing columns|Surligner les colonnes variables|Veränderliche Spalten hervorheben|Evidenzia colonne variabili|変化する列を強調|Выделять изменяющиеся столбцы
Source|Source|Quelle|Sorgente|ソース|Источник
Type|Type|Typ|Tipo|種別|Тип
Content|Contenu|Inhalt|Contenuto|内容|Содержимое
Bit err.|Err. bits|Bitfehler|Err. bit|ビット誤り|Ош. бит
RS corr.|Corr. RS|RS-Korr.|Corr. RS|RS訂正|Испр. RS
No decoded frames yet - run a BPSK / ШР test or analyze an I/Q file.|Aucune trame décodée - lancez un test BPSK / ШР ou analysez un fichier I/Q.|Noch keine dekodierten Rahmen - BPSK-/ШР-Test starten oder I/Q-Datei analysieren.|Nessuna trama decodificata - eseguire un test BPSK / ШР o analizzare un file I/Q.|復号済みフレームはありません — BPSK/ШР試験を実行するかI/Qファイルを解析してください。|Декодированных кадров нет — запустите проверку BPSK / ШР или проанализируйте файл I/Q.
no BOS telemetry yet|pas encore de télémesure BOS|noch keine BOS-Telemetrie|nessuna telemetria BOS|BOSテレメトリ未受信|телеметрии БОС пока нет
Full test|Test complet|Vollständige Prüfung|Test completo|全項目試験|Полная проверка
Quick check|Contrôle rapide|Schnellprüfung|Controllo rapido|簡易試験|Быстрая проверка
Modulation only|Modulation seule|Nur Modulation|Solo modulazione|変調のみ|Только модуляция
Data decoding|Décodage des données|Datendekodierung|Decodifica dati|データ復号|Декодирование данных
Burn-in (stability)|Rodage (stabilité)|Einbrennen (Stabilität)|Rodaggio (stabilità)|エージング(安定度)|Прогон (стабильность)
Pre-check (telemetry)|Pré-contrôle (télémesure)|Vorprüfung (Telemetrie)|Pre-controllo (telemetria)|事前確認(テレメトリ)|Предварительная проверка (телеметрия)
Supply current|Courant d'alimentation|Versorgungsstrom|Corrente di alimentazione|電源電流|Ток питания
BPSK modulation|Modulation BPSK|BPSK-Modulation|Modulazione BPSK|BPSK変調|Модуляция BPSK
OQPSK modulation|Modulation OQPSK|OQPSK-Modulation|Modulazione OQPSK|OQPSK変調|Модуляция OQPSK
PA linearity|Linéarité du PA|PA-Linearität|Linearità PA|PA直線性|Линейность УМ
LO harmonics|Harmoniques de l'OL|LO-Oberwellen|Armoniche LO|LO高調波|Гармоники гетеродина
Stability / drift|Stabilité / dérive|Stabilität / Drift|Stabilità / deriva|安定度/ドリフト|Стабильность / дрейф
Data / BER (УР)|Données / TEB (УР)|Daten / BER (УР)|Dati / BER (УР)|データ/BER (УР)|Данные / BER (УР)
ШР packet (despread + decode)|Paquet ШР (désétalement + décodage)|ШР-Paket (Entspreizen + Dekodieren)|Pacchetto ШР (despreading + decodifica)|ШРパケット(逆拡散+復号)|Пакет ШР (сжатие + декодирование)
Control link & BOS setup|Liaison de commande et réglage BOS|Steuerverbindung & BOS-Einstellung|Collegamento di controllo e impostazione BOS|制御リンクとBOS設定|Связь управления и настройка БОС
Output power|Puissance de sortie|Ausgangsleistung|Potenza di uscita|出力電力|Выходная мощность
Detected modulation|Modulation détectée|Erkannte Modulation|Modulazione rilevata|検出された変調|Обнаруженная модуляция
Carrier frequency error|Erreur de fréquence porteuse|Trägerfrequenzfehler|Errore di frequenza portante|搬送波周波数誤差|Ошибка частоты несущей
Symbol rate error|Erreur de débit symbole|Symbolratenfehler|Errore di symbol rate|シンボルレート誤差|Ошибка символьной скорости
Carrier leakage (I/Q offset)|Fuite de porteuse (offset I/Q)|Trägerdurchgriff (I/Q-Offset)|Dispersione portante (offset I/Q)|キャリアリーク(I/Qオフセット)|Просачивание несущей (смещение I/Q)
Magnitude error RMS|Erreur d'amplitude RMS|Betragsfehler RMS|Errore di ampiezza RMS|振幅誤差RMS|СКО ошибки амплитуды
Spectrum asymmetry|Asymétrie du spectre|Spektrumasymmetrie|Asimmetria dello spettro|スペクトル非対称|Асимметрия спектра
Largest spur|Plus forte raie parasite|Größte Nebenlinie|Spuria maggiore|最大スプリアス|Наибольшая побочная составляющая
Frames (КБД) decoded|Trames (КБД) décodées|Dekodierte Rahmen (КБД)|Trame (КБД) decodificate|復号フレーム (КБД)|Декодировано кадров (КБД)
RS uncorrectable frames|Trames non corrigibles RS|Nicht korrigierbare Rahmen (RS)|Trame non correggibili RS|RS訂正不能フレーム|Кадров, не исправленных RS
RS corrected bytes|Octets corrigés RS|RS-korrigierte Bytes|Byte corretti RS|RS訂正バイト|Байт, исправленных RS
Transport frame CRC errors|Erreurs CRC des trames|CRC-Fehler der Transportrahmen|Errori CRC delle trame|トランスポートフレームCRC誤り|Ошибки CRC транспортных кадров
Channel BER (raw code bits)|TEB du canal (bits codés bruts)|Kanal-BER (rohe Codebits)|BER di canale (bit codificati grezzi)|チャネルBER(生符号ビット)|BER канала (исходные кодовые биты)
Spacecraft ID|Identifiant du satellite|Satelliten-ID|ID veicolo spaziale|宇宙機ID|Идентификатор КА
Frame counter gaps|Sauts du compteur de trames|Lücken im Rahmenzähler|Salti del contatore di trame|フレームカウンタ欠落|Пропуски счётчика кадров
Signal type|Type de signal|Signaltyp|Tipo di segnale|信号種別|Тип сигнала
Main|Principal|Übersicht|Principale|メイン|Главная
Devices|Appareils|Geräte|Dispositivi|機器|Приборы
Power supply outputs|Sorties d'alimentation|Netzteilausgänge|Uscite di alimentazione|電源出力|Выходы источников питания
Power watchdog|Surveillance d'alimentation|Leistungsüberwachung|Watchdog di alimentazione|電源ウォッチドッグ|Сторож питания
Switch power off on error|Couper l'alimentation en cas d'erreur|Bei Fehler Versorgung abschalten|Spegni l'alimentazione in caso di errore|エラー時に電源オフ|Отключать питание при ошибке
Conditions…|Conditions…|Bedingungen…|Condizioni…|条件…|Условия…
Reset|Réinitialiser|Zurücksetzen|Reimposta|リセット|Сброс
BOS unit|Unité BOS|BOS-Gerät|Unità BOS|BOS装置|Блок БОС
Test sequence|Séquence de test|Prüfablauf|Sequenza di test|試験シーケンス|Последовательность проверки
New|Nouveau|Neu|Nuovo|新規|Новая
Rename|Renommer|Umbenennen|Rinomina|名前変更|Переименовать
Delete|Supprimer|Löschen|Elimina|削除|Удалить
Copy|Copier|Kopieren|Copia|コピー|Копировать
Edit|Modifier|Bearbeiten|Modifica|編集|Изменить
Loop|Boucle|Schleife|Ciclo|繰返し|Цикл
▶ Run sequence|▶ Lancer la séquence|▶ Ablauf starten|▶ Avvia sequenza|▶ シーケンス開始|▶ Запустить последовательность
■ Stop|■ Arrêt|■ Stopp|■ Stop|■ 停止|■ Стоп
Telemetry watch|Surveillance de télémesure|Telemetrie-Beobachtung|Monitoraggio telemetria|テレメトリ監視|Наблюдение телеметрии
+ Add|+ Ajouter|+ Hinzufügen|+ Aggiungi|+ 追加|+ Добавить
Remove|Retirer|Entfernen|Rimuovi|削除|Убрать
Log to CSV|Journaliser en CSV|In CSV protokollieren|Registra in CSV|CSVに記録|Записывать в CSV
only changes|seulement les changements|nur Änderungen|solo modifiche|変化時のみ|только изменения
Designation|Désignation|Bezeichnung|Designazione|名称|Обозначение
Meaning|Signification|Bedeutung|Significato|意味|Смысл
Updated|Mis à jour|Aktualisiert|Aggiornato|更新|Обновлено
+ Pause|+ Pause|+ Pause|+ Pausa|+ 待機|+ Пауза
+ Repeat|+ Répéter|+ Wiederholen|+ Ripeti|+ 繰返し|+ Повтор
+ BOS test|+ Test BOS|+ BOS-Prüfung|+ Test BOS|+ BOS試験|+ Проверка БОС
+ FSW set|+ Réglage FSW|+ FSW-Einstellung|+ Impostazione FSW|+ FSW設定|+ Настройка FSW
FSW connection (LAN)|Connexion FSW (LAN)|FSW-Verbindung (LAN)|Connessione FSW (LAN)|FSW接続 (LAN)|Подключение FSW (LAN)
Connect|Connecter|Verbinden|Connetti|接続|Подключить
Disconnect|Déconnecter|Trennen|Disconnetti|切断|Отключить
Connection|Connexion|Verbindung|Connessione|接続|Подключение
Protocol|Protocole|Protokoll|Protocollo|プロトコル|Протокол
Demo (no instrument)|Démo (sans instrument)|Demo (ohne Gerät)|Demo (senza strumento)|デモ(機器なし)|Демо (без прибора)
Read|Lire|Lesen|Leggi|読込|Прочитать
Apply ▶ FSW|Appliquer ▶ FSW|Übernehmen ▶ FSW|Applica ▶ FSW|適用 ▶ FSW|Применить ▶ FSW
Read ◀ FSW|Lire ◀ FSW|Lesen ◀ FSW|Leggi ◀ FSW|読込 ◀ FSW|Прочитать ◀ FSW
Auto Level|Niveau auto|Autopegel|Livello auto|自動レベル|Автоуровень
Restore VSA layout|Rétablir la disposition VSA|VSA-Anordnung wiederherstellen|Ripristina layout VSA|VSAレイアウト復元|Восстановить вид VSA
Frequency / level (FSW)|Fréquence / niveau (FSW)|Frequenz / Pegel (FSW)|Frequenza / livello (FSW)|周波数/レベル (FSW)|Частота / уровень (FSW)
Signal Description|Description du signal|Signalbeschreibung|Descrizione del segnale|信号定義|Описание сигнала
Modulation|Modulation|Modulation|Modulazione|変調|Модуляция
Symbol rate|Débit symbole|Symbolrate|Symbol rate|シンボルレート|Символьная скорость
Filter|Filtre|Filter|Filtro|フィルタ|Фильтр
Capture / Result|Acquisition / résultat|Aufnahme / Ergebnis|Acquisizione / risultato|取得/結果|Запись / результат
Capture length|Longueur d'acquisition|Aufnahmelänge|Lunghezza acquisizione|取得長|Длина записи
Result length|Longueur du résultat|Ergebnislänge|Lunghezza risultato|結果長|Длина результата
Oversampling|Suréchantillonnage|Überabtastung|Sovracampionamento|オーバーサンプリング|Передискретизация
Statistic count|Nombre de statistiques|Statistikanzahl|Numero statistico|統計回数|Число усреднений
Equalizer|Égaliseur|Entzerrer|Equalizzatore|イコライザ|Эквалайзер
Burst search|Recherche de salve|Burst-Suche|Ricerca burst|バースト検索|Поиск пакета
Pattern search|Recherche de motif|Mustersuche|Ricerca pattern|パターン検索|Поиск шаблона
Trigger source|Source de déclenchement|Triggerquelle|Sorgente di trigger|トリガ源|Источник запуска
Trigger|Déclenchement|Trigger|Trigger|トリガ|Запуск
Ref level|Niveau de réf.|Referenzpegel|Livello di rif.|基準レベル|Опорный уровень
Attenuation|Atténuation|Dämpfung|Attenuazione|減衰|Ослабление
Center|Centre|Mitte|Centro|中心|Центр
Span|Plage|Span|Span|スパン|Полоса обзора
Detector|Détecteur|Detektor|Rivelatore|検波|Детектор
Trace mode|Mode de trace|Kurvenmodus|Modalità traccia|トレースモード|Режим трассы
Sweep time|Temps de balayage|Sweepzeit|Tempo di sweep|掃引時間|Время развёртки
Sweep points|Points de balayage|Sweeppunkte|Punti di sweep|掃引ポイント|Точек развёртки
Continuous|Continu|Kontinuierlich|Continuo|連続|Непрерывно
Single|Unique|Einzeln|Singolo|単発|Однократно
Single sweeps (synced)|Balayages uniques (synchronisés)|Einzelsweeps (synchron)|Sweep singoli (sincronizzati)|単発掃引(同期)|Однократные развёртки (синхр.)
▶ Run|▶ Lancer|▶ Start|▶ Avvia|▶ 実行|▶ Пуск
Reset stats|Réinit. statistiques|Statistik zurücksetzen|Azzera statistiche|統計リセット|Сбросить статистику
Log CSV|Journal CSV|CSV-Protokoll|Log CSV|CSV記録|Журнал CSV
Update FSW screen during remote|Mettre à jour l'écran FSW en commande à distance|FSW-Bildschirm bei Fernsteuerung aktualisieren|Aggiorna schermo FSW in remoto|リモート中もFSW画面を更新|Обновлять экран FSW при ДУ
Power|Puissance|Leistung|Potenza|電力|Мощность
Spectrum|Spectre|Spektrum|Spettro|スペクトル|Спектр
I/Q (software demod)|I/Q (démod. logicielle)|I/Q (Software-Demod.)|I/Q (demod. software)|I/Q(ソフト復調)|I/Q (программная демодуляция)
Simulated hardware (demo)|Matériel simulé (démo)|Simulierte Hardware (Demo)|Hardware simulato (demo)|模擬ハードウェア(デモ)|Имитация оборудования (демо)
Address|Adresse|Adresse|Indirizzo|アドレス|Адрес
Logging|Journalisation|Protokollierung|Registrazione|記録|Журналирование
Write daily HMP4040 log|Journal HMP4040 quotidien|Tägliches HMP4040-Protokoll schreiben|Scrivi log HMP4040 giornaliero|HMP4040日次ログを記録|Писать суточный журнал HMP4040
Write daily N6701C log|Journal N6701C quotidien|Tägliches N6701C-Protokoll schreiben|Scrivi log N6701C giornaliero|N6701C日次ログを記録|Писать суточный журнал N6701C
HMP log every|Journal HMP toutes les|HMP-Protokoll alle|Log HMP ogni|HMPログ間隔|Журнал HMP каждые
Log every|Journaliser toutes les|Protokollieren alle|Registra ogni|記録間隔|Записывать каждые
Folder…|Dossier…|Ordner…|Cartella…|フォルダ…|Папка…
Power cycling|Cycles marche/arrêt|Ein-/Aus-Zyklen|Cicli di accensione|電源サイクル|Циклы включения
▶ Start cycling|▶ Démarrer les cycles|▶ Zyklen starten|▶ Avvia cicli|▶ サイクル開始|▶ Запустить циклы
■ Stop cycling|■ Arrêter les cycles|■ Zyklen stoppen|■ Ferma cicli|■ サイクル停止|■ Остановить циклы
MASTER OUTPUT: OFF|SORTIE GÉNÉRALE : ARRÊT|HAUPTAUSGANG: AUS|USCITA GENERALE: OFF|マスター出力:オフ|ОБЩИЙ ВЫХОД: ВЫКЛ
MASTER OUTPUT: ON|SORTIE GÉNÉRALE : MARCHE|HAUPTAUSGANG: EIN|USCITA GENERALE: ON|マスター出力:オン|ОБЩИЙ ВЫХОД: ВКЛ
Time left: --:--:--|Temps restant : --:--:--|Restzeit: --:--:--|Tempo rimanente: --:--:--|残り時間: --:--:--|Осталось: --:--:--
Idle|Repos|Leerlauf|Inattivo|待機|Ожидание
Plot|Graphique|Diagramm|Grafico|グラフ|График
Follow|Suivre|Folgen|Segui|追従|Следовать
Clear plot|Effacer le graphique|Diagramm löschen|Cancella grafico|グラフ消去|Очистить график
Current|Courant|Strom|Corrente|電流|Ток
Voltage|Tension|Spannung|Tensione|電圧|Напряжение
Voltage, V|Tension, V|Spannung, V|Tensione, V|電圧, V|Напряжение, В
Current, A|Courant, A|Strom, A|Corrente, A|電流, A|Ток, А
Power, W|Puissance, W|Leistung, W|Potenza, W|電力, W|Мощность, Вт
ALL OUTPUTS ON|TOUTES SORTIES MARCHE|ALLE AUSGÄNGE EIN|TUTTE LE USCITE ON|全出力オン|ВСЕ ВЫХОДЫ ВКЛ
ALL OUTPUTS OFF|TOUTES SORTIES ARRÊT|ALLE AUSGÄNGE AUS|TUTTE LE USCITE OFF|全出力オフ|ВСЕ ВЫХОДЫ ВЫКЛ
Clear protection|Effacer la protection|Schutz zurücksetzen|Azzera protezione|保護解除|Сбросить защиту
Read all|Tout lire|Alle lesen|Leggi tutto|全読込|Прочитать всё
Set|Régler|Setzen|Imposta|設定|Установить
Output on|Sortie active|Ausgang ein|Uscita on|出力オン|Выход вкл.
Waveform|Forme d'onde|Kurvenform|Forma d'onda|波形|Форма сигнала
Frequency|Fréquence|Frequenz|Frequenza|周波数|Частота
Amplitude|Amplitude|Amplitude|Ampiezza|振幅|Амплитуда
Offset|Décalage|Offset|Offset|オフセット|Смещение
Phase|Phase|Phase|Fase|位相|Фаза
Sweep|Balayage|Sweep|Sweep|掃引|Развёртка
Burst|Salve|Burst|Burst|バースト|Пакет
Double pulse|Double impulsion|Doppelimpuls|Doppio impulso|ダブルパルス|Двойной импульс
Sine|Sinus|Sinus|Sinusoide|正弦波|Синус
Square|Carré|Rechteck|Quadra|方形波|Меандр
Ramp|Rampe|Rampe|Rampa|ランプ|Пила
Pulse|Impulsion|Puls|Impulso|パルス|Импульс
Noise|Bruit|Rauschen|Rumore|雑音|Шум
Align phases|Aligner les phases|Phasen angleichen|Allinea fasi|位相整列|Выровнять фазы
Save state|Enregistrer l'état|Zustand speichern|Salva stato|状態保存|Сохранить состояние
Recall state|Rappeler l'état|Zustand laden|Richiama stato|状態呼出|Загрузить состояние
SCPI console|Console SCPI|SCPI-Konsole|Console SCPI|SCPIコンソール|Консоль SCPI
Send|Envoyer|Senden|Invia|送信|Отправить
Transmit|Émission|Senden|Trasmissione|送信|Передача
Trace|Trace|Mitschnitt|Traccia|トレース|Трасса
Monitor (by ID)|Moniteur (par ID)|Monitor (nach ID)|Monitor (per ID)|モニタ(ID別)|Монитор (по ID)
● Record|● Enregistrer|● Aufzeichnen|● Registra|● 記録|● Запись
Open log…|Ouvrir un journal…|Protokoll öffnen…|Apri log…|ログを開く…|Открыть журнал…
Save…|Enregistrer…|Speichern…|Salva…|保存…|Сохранить…
Clear|Effacer|Löschen|Cancella|消去|Очистить
Autoscroll|Défilement auto|Automatisch scrollen|Scorrimento automatico|自動スクロール|Автопрокрутка
Listen-only|Écoute seule|Nur Mithören|Solo ascolto|受信専用|Только прослушивание
Letter hotkeys (press the key to send)|Raccourcis lettres (appuyer pour envoyer)|Buchstaben-Tasten (Taste drücken = senden)|Tasti rapidi (premere per inviare)|文字ホットキー(押すと送信)|Горячие клавиши (нажатие = отправка)
Commands|Commandes|Befehle|Comandi|コマンド|Команды
Script|Script|Skript|Script|スクリプト|Скрипт
Loop script|Script en boucle|Skript wiederholen|Script in ciclo|スクリプト繰返し|Скрипт в цикле
Bus statistics|Statistiques du bus|Busstatistik|Statistiche bus|バス統計|Статистика шины
Bus load|Charge du bus|Buslast|Carico bus|バス負荷|Загрузка шины
Rx frames|Trames reçues|Empfangene Rahmen|Trame ricevute|受信フレーム|Принято кадров
Tx frames|Trames émises|Gesendete Rahmen|Trame trasmesse|送信フレーム|Передано кадров
Errors|Erreurs|Fehler|Errori|エラー|Ошибки
Interface|Interface|Schnittstelle|Interfaccia|インタフェース|Интерфейс
Channel / port|Canal / port|Kanal / Port|Canale / porta|チャネル/ポート|Канал / порт
Bitrate|Débit|Bitrate|Bitrate|ビットレート|Скорость
Enabled|Activé|Aktiv|Abilitato|有効|Вкл.
Line|Ligne|Leitung|Linea|ライン|Линия
Show lines:|Afficher les lignes :|Leitungen anzeigen:|Mostra linee:|表示ライン:|Показывать линии:
Name|Nom|Name|Nome|名称|Имя
Description|Description|Beschreibung|Descrizione|説明|Описание
Key|Touche|Taste|Tasto|キー|Клавиша
Load|Charger|Laden|Carica|読込|Загрузить
Add CSV files…|Ajouter des fichiers CSV…|CSV-Dateien hinzufügen…|Aggiungi file CSV…|CSVファイル追加…|Добавить файлы CSV…
Today's logs|Journaux du jour|Heutige Protokolle|Log di oggi|今日のログ|Журналы за сегодня
Hide all|Tout masquer|Alle ausblenden|Nascondi tutto|すべて隠す|Скрыть все
Absolute time|Temps absolu|Absolute Zeit|Tempo assoluto|絶対時刻|Абсолютное время
Relative time|Temps relatif|Relative Zeit|Tempo relativo|相対時刻|Относительное время
Auto scale|Échelle auto|Autoskalierung|Scala automatica|自動スケール|Автомасштаб
Steps|Paliers|Stufen|Gradini|ステップ|Ступени
Points|Points|Punkte|Punti|点|Точки
FAQ|FAQ|FAQ|FAQ|FAQ|Вопросы и ответы
Readme|Lisez-moi|Liesmich|Leggimi|説明書|Описание
About|À propos|Über|Informazioni|情報|О программе
Language|Langue|Sprache|Lingua|言語|Язык
on|marche|ein|on|オン|вкл
off|arrêt|aus|off|オフ|выкл
ON|MARCHE|EIN|ON|オン|ВКЛ
OFF|ARRÊT|AUS|OFF|オフ|ВЫКЛ
none|aucun|keine|nessuno|なし|нет
not connected|non connecté|nicht verbunden|non connesso|未接続|не подключено
● disconnected|● déconnecté|● getrennt|● disconnesso|● 切断|● отключено
● off|● arrêt|● aus|● off|● オフ|● выкл
Internal|Interne|Intern|Interno|内部|Внутренний
External|Externe|Extern|Esterno|外部|Внешний
Manual|Manuel|Manuell|Manuale|手動|Вручную
Auto|Auto|Auto|Auto|自動|Авто
Normal|Normal|Normal|Normale|通常|Обычный
Inverted|Inversé|Invertiert|Invertito|反転|Инверсный
Rising|Montant|Steigend|Salita|立上り|Фронт
Falling|Descendant|Fallend|Discesa|立下り|Спад
Positive|Positif|Positiv|Positivo|正|Положительный
Negative|Négatif|Negativ|Negativo|負|Отрицательный
Linear|Linéaire|Linear|Lineare|リニア|Линейная
Logarithmic|Logarithmique|Logarithmisch|Logaritmica|対数|Логарифмическая
Status|État|Status|Stato|状態|Статус
Duration|Durée|Dauer|Durata|期間|Длительность
Interval|Intervalle|Intervall|Intervallo|間隔|Интервал
Pause|Pause|Pause|Pausa|一時停止|Пауза
Mode|Mode|Modus|Modalità|モード|Режим
Level|Niveau|Pegel|Livello|レベル|Уровень
Device|Appareil|Gerät|Dispositivo|機器|Прибор
Instrument|Instrument|Gerät|Strumento|測定器|Прибор
IP address|Adresse IP|IP-Adresse|Indirizzo IP|IPアドレス|IP-адрес
Port|Port|Port|Porta|ポート|Порт
Timeout|Délai|Zeitlimit|Timeout|タイムアウト|Тайм-аут
Low PA gain|Gain PA faible|Geringe PA-Verstärkung|Guadagno PA basso|PA利得低下|Низкое усиление УМ
No RF output|Pas de sortie RF|Kein HF-Ausgang|Nessuna uscita RF|RF出力なし|Нет ВЧ на выходе
Over-temperature|Surchauffe|Übertemperatur|Sovratemperatura|過熱|Перегрев
LO leakage|Fuite de l'OL|LO-Durchgriff|Dispersione LO|LO漏れ|Просачивание гетеродина
Excess phase noise|Bruit de phase excessif|Übermäßiges Phasenrauschen|Rumore di fase eccessivo|過大な位相雑音|Повышенный фазовый шум
Quadrature error|Erreur de quadrature|Quadraturfehler|Errore di quadratura|直交誤差|Ошибка квадратуры
Q path gain error|Erreur de gain voie Q|Verstärkungsfehler Q-Pfad|Errore di guadagno ramo Q|Q経路利得誤差|Ошибка усиления канала Q
Q rail dead (DAC ch B)|Voie Q morte (DAC voie B)|Q-Zweig tot (DAC Kanal B)|Ramo Q guasto (DAC canale B)|Q系統断(DAC chB)|Нет канала Q (ЦАП, канал B)
JESD204B link error|Erreur de liaison JESD204B|JESD204B-Verbindungsfehler|Errore collegamento JESD204B|JESD204Bリンク異常|Ошибка канала JESD204B
LMX2594 unlocked|LMX2594 déverrouillé|LMX2594 nicht eingerastet|LMX2594 sganciato|LMX2594 アンロック|LMX2594 не захвачен
LMX2594 frequency error|Erreur de fréquence LMX2594|LMX2594-Frequenzfehler|Errore di frequenza LMX2594|LMX2594 周波数誤差|Ошибка частоты LMX2594
DAC / FPGA clock error|Erreur d'horloge DAC / FPGA|DAC-/FPGA-Taktfehler|Errore di clock DAC / FPGA|DAC/FPGAクロック誤差|Ошибка тактирования ЦАП / ПЛИС
PA compression (backoff)|Compression PA (recul)|PA-Kompression (Backoff)|Compressione PA (backoff)|PA圧縮(バックオフ)|Компрессия УМ (запас)
Synthesizer spur|Raie du synthétiseur|Synthesizer-Nebenlinie|Spuria del sintetizzatore|シンセサイザスプリアス|Побочная синтезатора
1.8 V rail low|Rail 1,8 V bas|1,8-V-Spannung zu niedrig|Alimentazione 1,8 V bassa|1.8 V電源低下|Низкое питание 1,8 В
Internal 10 MHz oscillator offset|Écart de l'oscillateur interne 10 MHz|Abweichung interner 10-MHz-Oszillator|Scostamento oscillatore interno 10 MHz|内部10 MHz発振器偏差|Уход внутреннего генератора 10 МГц
External 10 MHz (TFG6806) offset|Écart du 10 MHz externe (TFG6806)|Abweichung externe 10 MHz (TFG6806)|Scostamento 10 MHz esterno (TFG6806)|外部10 MHz(TFG6806)偏差|Уход внешних 10 МГц (TFG6806)
External 10 MHz missing|10 MHz externe absent|Externe 10 MHz fehlen|10 MHz esterno assente|外部10 MHzなし|Нет внешних 10 МГц
Attenuator bit stuck (Atten_EIRP)|Bit d'atténuateur bloqué (Atten_EIRP)|Dämpfungsbit hängt (Atten_EIRP)|Bit attenuatore bloccato (Atten_EIRP)|減衰器ビット固着 (Atten_EIRP)|Залипание разряда аттенюатора (Atten_EIRP)
Wrong modulation (QPSK sent)|Mauvaise modulation (QPSK émis)|Falsche Modulation (QPSK gesendet)|Modulazione errata (inviato QPSK)|誤った変調(QPSK送信)|Неверная модуляция (передаётся QPSK)
Data path bit errors (FPGA / buffer)|Erreurs de bits sur le chemin de données (FPGA / tampon)|Bitfehler im Datenpfad (FPGA / Puffer)|Errori di bit nel percorso dati (FPGA / buffer)|データ経路ビット誤り(FPGA/バッファ)|Ошибки в тракте данных (ПЛИС / буфер)
Reed-Solomon check symbols missing|Symboles de contrôle Reed-Solomon absents|Reed-Solomon-Prüfsymbole fehlen|Simboli di controllo Reed-Solomon assenti|リードソロモン検査シンボルなし|Нет проверочных символов Рида–Соломона
ШР spreading sequence wrong (FPGA)|Séquence d'étalement ШР erronée (FPGA)|Falsche ШР-Spreizfolge (FPGA)|Sequenza di spreading ШР errata (FPGA)|ШР拡散系列誤り(FPGA)|Неверная расширяющая ПСП ШР (ПЛИС)
Analyse selection|Analyser la sélection|Auswahl analysieren|Analizza selezione|選択範囲を解析|Анализировать выделенное
Auto (signal only)|Auto (signal seul)|Auto (nur Signal)|Auto (solo segnale)|自動(信号部のみ)|Авто (только сигнал)
Whole recording|Enregistrement entier|Gesamte Aufnahme|Registrazione intera|記録全体|Вся запись
Zoom to selection|Zoom sur la sélection|Auf Auswahl zoomen|Zoom sulla selezione|選択範囲へズーム|Увеличить выделенное
Show all|Tout afficher|Alles zeigen|Mostra tutto|全体表示|Показать всё
Start|Début|Start|Inizio|開始|Начало
Stop|Fin|Ende|Fine|終了|Конец
Sample rate|Fréquence d'échantillonnage|Abtastrate|Frequenza di campionamento|サンプルレート|Частота дискретизации
Centre frequency|Fréquence centrale|Mittenfrequenz|Frequenza centrale|中心周波数|Центральная частота
Samples|Échantillons|Abtastwerte|Campioni|サンプル数|Отсчётов
УР rate|Débit УР|УР-Rate|Velocità УР|УР速度|Скорость УР
ШР rate|Débit ШР|ШР-Rate|Velocità ШР|ШР速度|Скорость ШР
Auto (from the spectrum)|Auto (d'après le spectre)|Auto (aus dem Spektrum)|Auto (dallo spettro)|自動(スペクトルから)|Авто (по спектру)
УР - BPSK|УР - BPSK|УР - BPSK|УР - BPSK|УР - BPSK|УР — BPSK
ШР - OQPSK|ШР - OQPSK|ШР - OQPSK|ШР - OQPSK|ШР - OQPSK|ШР — OQPSK
Raw data type|Type des données brutes|Rohdatentyp|Tipo dati grezzi|生データ型|Тип исходных данных
Sample order|Ordre des échantillons|Abtastreihenfolge|Ordine dei campioni|サンプル順序|Порядок отсчётов
I,Q pairs|Paires I,Q|I,Q-Paare|Coppie I,Q|I,Qペア|Пары I,Q
I block, then Q block|Bloc I puis bloc Q|I-Block, dann Q-Block|Blocco I, poi blocco Q|Iブロック後にQブロック|Блок I, затем блок Q
auto (best MER)|auto (meilleur MER)|auto (bestes MER)|auto (MER migliore)|自動(最良MER)|авто (лучший MER)
auto (search)|auto (recherche)|auto (Suche)|auto (ricerca)|自動(探索)|авто (поиск)
Reference|Référence|Referenz|Riferimento|基準|Опорный
Clock PLLs|PLL d'horloge|Takt-PLLs|PLL di clock|クロックPLL|ФАПЧ тактов
DAC|CNA|DAC|DAC|DAC|ЦАП
Upconverter|Convertisseur haut|Aufwärtsmischer|Convertitore in salita|アップコンバータ|Преобразователь вверх
Power amp|Ampli de puissance|Leistungsverstärker|Amplificatore di potenza|電力増幅器|Усилитель мощности
Output / setup|Sortie / montage|Ausgang / Aufbau|Uscita / setup|出力/測定系|Выход / стенд
LO synth|Synthé OL|LO-Synthesizer|Sintetizzatore LO|LOシンセサイザ|Синтезатор гетеродина
Power rails|Alimentations|Versorgungsspannungen|Alimentazioni|電源系|Питание
FPGA|FPGA|FPGA|FPGA|FPGA|ПЛИС
suspicion: green < 10 % · yellow · orange · red > 65 %   (click a block to filter the findings)|suspicion : vert < 10 % · jaune · orange · rouge > 65 %   (cliquer un bloc pour filtrer les constats)|Verdacht: grün < 10 % · gelb · orange · rot > 65 %   (Block anklicken = Befunde filtern)|sospetto: verde < 10 % · giallo · arancio · rosso > 65 %   (clic su un blocco per filtrare)|疑い度: 緑 < 10 % · 黄 · 橙 · 赤 > 65 %   (ブロックをクリックで所見を絞込み)|подозрение: зелёный < 10 % · жёлтый · оранжевый · красный > 65 %   (щелчок по блоку — фильтр выводов)
filter (serial, verdict, plan …)|filtre (n° de série, verdict, plan …)|Filter (Seriennr., Ergebnis, Plan …)|filtro (seriale, esito, piano …)|フィルタ(製番, 判定, 計画 …)|фильтр (номер, вердикт, план …)
"""

TABLE: dict[str, dict[str, str]] = {}
for _ln in _RAW.strip().split("\n"):
    _p = _ln.split("|")
    if len(_p) >= 6:
        # the English key itself may contain '|' (e.g. "|code|"): the last 5 fields are the translations
        _key = "|".join(_p[:-5])
        TABLE[_key] = dict(zip(_ORDER, _p[-5:]))


CURRENT = "en"                                            # for painted texts (block diagram)


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


LANG_FILE = os.path.join(_app_dir(), "ui_language.txt")


def load_language() -> str:
    try:
        with open(LANG_FILE, encoding="utf-8") as fh:
            v = fh.read().strip()
        return v if v in dict(LANGS) else "en"
    except OSError:
        return "en"


def save_language(code: str):
    try:
        with open(LANG_FILE, "w", encoding="utf-8") as fh:
            fh.write(code)
    except OSError:
        pass


_TAG = re.compile(r"^(<b>)(.*)(</b>)$", re.S)


def tr(text: str, lang: str) -> str:
    if lang == "en" or not text:
        return text
    d = TABLE.get(text)
    if d:
        return d.get(lang, text)
    m = _TAG.match(text)                                  # "<b>…</b>"
    if m and m.group(2) in TABLE:
        return m.group(1) + TABLE[m.group(2)].get(lang, m.group(2)) + m.group(3)
    s = text.strip()
    if s != text and s in TABLE:
        return text.replace(s, TABLE[s].get(lang, s))
    return text


class Translator(QtCore.QObject):
    """Translates the widget tree of `root` live; keeps the English originals per widget."""
    changed = QtCore.pyqtSignal(str)

    def __init__(self, root: QtWidgets.QWidget, lang: str = "en"):
        super().__init__(root)
        global CURRENT
        self.root, self.lang = root, lang
        CURRENT = lang
        self._src: dict = {}                              # (id(obj), slot) -> (english, shown)
        self.timer = QtCore.QTimer(self, interval=1500, timeout=self.apply)
        self.timer.start()

    def set_language(self, lang: str):
        global CURRENT
        self.lang = lang
        CURRENT = lang
        for w in self.root.findChildren(QtWidgets.QWidget):    # repaint painted texts (block diagram)
            w.update()
        save_language(lang)
        self.apply()
        self.changed.emit(lang)

    def _t(self, obj, slot, cur: str, setter):
        key = (id(obj), slot)
        src, shown = self._src.get(key, (None, None))
        if src is None or cur != shown:                   # new text set by the program -> new source
            src = cur
        new = tr(src, self.lang)
        if new != cur:
            setter(new)
        self._src[key] = (src, new)

    def apply(self):
        if self.lang == "en" and not self._src:
            return
        for w in [self.root] + self.root.findChildren(QtWidgets.QWidget):
            try:
                if isinstance(w, (QtWidgets.QLabel, QtWidgets.QAbstractButton)) and not isinstance(
                        w, QtWidgets.QToolButton) or isinstance(w, QtWidgets.QToolButton) and w.text():
                    self._t(w, "text", w.text(), w.setText)
                if isinstance(w, QtWidgets.QGroupBox):
                    self._t(w, "title", w.title(), w.setTitle)
                if isinstance(w, QtWidgets.QTabWidget):
                    for i in range(w.count()):
                        self._t(w, f"tab{i}", w.tabText(i), lambda s, i=i, w=w: w.setTabText(i, s))
                if isinstance(w, QtWidgets.QLineEdit) and w.placeholderText():
                    self._t(w, "ph", w.placeholderText(), w.setPlaceholderText)
                if isinstance(w, QtWidgets.QComboBox) and not w.isEditable():
                    # only combos whose items carry data: the program reads currentData(), not the text
                    if all(w.itemData(i) is not None for i in range(w.count())):
                        for i in range(w.count()):
                            self._t(w, f"it{i}", w.itemText(i), lambda s, i=i, w=w: w.setItemText(i, s))
                if isinstance(w, QtWidgets.QTableWidget):
                    for i in range(w.columnCount()):
                        it = w.horizontalHeaderItem(i)
                        if it:
                            self._t(it, "h", it.text(), it.setText)
                    if w.rowCount() * w.columnCount() <= 3000 and w.columnCount() <= 12:
                        for r in range(w.rowCount()):
                            for c in range(min(2, w.columnCount())):       # test / parameter names
                                it = w.item(r, c)
                                if it:
                                    self._t(it, "c", it.text(), it.setText)
            except RuntimeError:                           # widget deleted meanwhile
                continue
        if len(self._src) > 200000:
            self._src.clear()
