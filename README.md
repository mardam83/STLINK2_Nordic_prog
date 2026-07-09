# nRF52 ST-Link Flasher

Applicazione desktop per programmare **nRF52832** e **nRF52840** tramite **ST-Link V2**, caricando file Intel HEX. Supporta il caricamento del **SoftDevice** Nordic insieme al firmware applicativo, inclusi i firmware compilati con **Arduino**.

## Requisiti

- Windows 10/11
- Python 3.10 o superiore
- ST-Link V2 (originale ST o clone compatibile)
- Driver ST-Link installati ([STM32CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html) o driver standalone ST)

## Collegamento hardware

| ST-Link | nRF52 |
|---------|-------|
| SWDIO   | SWDIO |
| SWCLK   | SWCLK |
| GND     | GND   |
| 3.3V    | VDD   *(opzionale se alimentato separatamente)* |

> **Nota:** non collegare NRST se non necessario. Assicurati che il target sia a 3.3 V.

## Avvio rapido

Doppio click su **`run.bat`**. Al primo avvio crea l'ambiente virtuale e installa le dipendenze.

Oppure manualmente:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

## Utilizzo

1. Collega ST-Link e scheda nRF52
2. Avvia l'applicazione
3. Seleziona il microcontrollore (52832 o 52840)
4. Premi **Aggiorna** per rilevare lo ST-Link
5. Sfoglia e seleziona il firmware `.hex` (e, se serve, il SoftDevice `.hex`)
6. Premi **Programma**

Alla selezione di un file il log mostra l'analisi del layout: applicazione standalone,
applicazione per MBR, applicazione che richiede un SoftDevice, oppure SoftDevice con
nome/versione/dimensione letti dall'info struct Nordic a `0x3000`.

### SoftDevice

Il campo **SoftDevice (opz.)** accetta l'HEX ufficiale Nordic (es.
`s132_nrf52_7.2.0_softdevice.hex`). Si può caricare:

- **solo il firmware** — comportamento classico
- **solo il SoftDevice** — lascia vuoto il campo firmware
- **SoftDevice + firmware insieme** — i due HEX vengono uniti e scritti in un unico
  passaggio, con verifica automatica che il firmware sia linkato dove finisce il
  SoftDevice (`APP_CODE_BASE`). Sovrapposizioni o file incompatibili vengono
  bloccati prima di toccare la flash.

> Quando si carica o si cambia SoftDevice è consigliato attivare
> **Cancella tutta la flash**.

### Firmware Arduino

I core Arduino per nRF52 producono un `.hex` (in Arduino IDE: *Sketch → Esporta
sketch compilato*, oppure nella cartella di build). A seconda del core:

| Core / configurazione | Inizio app | Cosa selezionare |
|---|---|---|
| arduino-nRF5 (sandeepmistry), SoftDevice "None" | `0x0` | solo firmware |
| arduino-nRF5 con S132 v2.x | `0x1C000` | firmware + `s132_nrf52_2.x` |
| Adafruit nRF52832 (S132 v6/v7) | `0x26000` | firmware + `s132_nrf52_6/7` |
| Adafruit nRF52840 (S140 v6/v7) | `0x26000`/`0x27000` | firmware + `s140_nrf52_6/7` |

### Firmware Zephyr / MCUboot (tab "MCUboot (Zephyr)")

Terza scheda dedicata al progetto **TendaVibrationZephyr** (nRF Connect SDK con
bootloader MCUboot). Due modalità, preselezionate automaticamente in base al
file (riconoscimento dell'header immagine MCUboot a `0xC000`):

- **Chip completo (`merged.hex`)** — mass erase + MCUboot + applicazione
  firmata. Da usare al primo flash o per migrare un modulo dal bootloader
  Adafruit. Cancella tutto: vecchio bootloader, SoftDevice, UICR, soglie.
- **Solo applicazione (`zephyr.signed.hex`)** — scrive l'app nello slot
  primario conservando MCUboot e la partizione settings.

Probe ST-Link e microcontrollore si selezionano nel tab "Cavo ST-Link". Le
azioni post-flash legacy (UICR/CRC) non servono: MCUboot valida le immagini
con la propria firma. Gli aggiornamenti OTA dei moduli MCUboot usano il
protocollo **SMP** (app nRF Connect Device Manager o `smpclient`), non la
scheda "OTA BLE" (DFU legacy Adafruit).

## Caratteristiche
- Interfaccia a tre schede (Tabview): **Cavo ST-Link**, **OTA BLE** e **MCUboot (Zephyr)**.
- Programmazione di file Intel HEX tramite sonda ST-Link V2 (via pyOCD).
- (Nuovo) **Aggiornamento Firmware OTA (Over-The-Air)** senza fili usando la connessione Bluetooth del PC e il pacchetto `.zip` di Arduino.
- Scansione Bluetooth integrata per trovare il dispositivo senza hardcodare i MAC address.
- Estrazione automatica della porzione dell'applicazione dall'HEX.
- Generazione CRC-16 CCITT per simulare i pacchetti `nrfutil` e mantenere compatibilità OTA del bootloader Adafruit.
- Iniezione "trampolino" MBR a `0x0` per le app compilate per l'indirizzo `0x1000` (es. varianti OpenThread).
- Operazioni di mass erase e riavvio software/hardware automatizzate.

## Requisiti

- Windows / Linux / macOS
- Python 3.10+
- Installare le dipendenze: `pip install -r requirements.txt` (include `pyocd`, `customtkinter`, `intelhex`, e `bleak`). 

## Opzioni

- **Cancella tutta la flash** — esegue un mass erase prima della scrittura
- **Reset dopo la programmazione** — riavvia il firmware appena caricato (consigliato)
- **Aggiungi trampolino MBR a 0x0** — se l'HEX è linkato a `0x1000` e lascia `0x0`
  vuoto (tipico delle app nRF SDK come `ot-rcp`, che girano sotto il Nordic MBR),
  scrive a `0x0` un piccolo trampolino che imposta `VTOR=0x1000` e salta all'app.
  Senza, il chip andrebbe in lockup al reset. Si attiva solo quando serve; lascialo
  attivo se non hai un MBR/SoftDevice/bootloader da programmare separatamente.
  Se selezioni un SoftDevice l'opzione viene disabilitata: l'MBR vero è già incluso
  nell'HEX del SoftDevice.

> **Nota APPROTECT:** se il chip è bloccato (APPROTECT attivo), la connessione lo
> sblocca automaticamente con un mass erase via CTRL-AP. È normale e necessario per
> poter riprogrammare nRF52 protetti.

## Risoluzione problemi

| Problema | Soluzione |
|----------|-----------|
| Nessun ST-Link rilevato | Verifica USB, driver ST, cavo SWD |
| `No probe found` | Chiudi STM32CubeProgrammer / OpenOCD se aperti |
| Errore di scrittura | Controlla alimentazione e collegamenti SWDIO/SWCLK |
| Target sbagliato | Seleziona il chip corretto (52832 vs 52840) |

## Tecnologie

- [pyOCD](https://github.com/pyocd/pyOCD) — programmazione via ST-Link
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — interfaccia grafica
