# nRF52 ST-Link Flasher

Applicazione desktop per programmare **nRF52832** e **nRF52840** tramite **ST-Link V2**, caricando file Intel HEX.

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
5. Sfoglia e seleziona il file `.hex`
6. Premi **Programma**

### Opzioni

- **Cancella tutta la flash** — esegue un mass erase prima della scrittura
- **Reset dopo la programmazione** — riavvia il firmware appena caricato (consigliato)
- **Aggiungi trampolino MBR a 0x0** — se l'HEX è linkato a `0x1000` e lascia `0x0`
  vuoto (tipico delle app nRF5 SDK come `ot-rcp`, che girano sotto il Nordic MBR),
  scrive a `0x0` un piccolo trampolino che imposta `VTOR=0x1000` e salta all'app.
  Senza, il chip andrebbe in lockup al reset. Si attiva solo quando serve; lascialo
  attivo se non hai un MBR/SoftDevice/bootloader da programmare separatamente.

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
