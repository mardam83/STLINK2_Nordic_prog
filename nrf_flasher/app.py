"""UI per programmare nRF52832 / nRF52840 via ST-Link V2 o OTA BLE.

Quattro percorsi di programmazione, uno per tab:

* Cavo ST-Link      — flash "classico" Nordic/Adafruit (SoftDevice, MBR, UICR)
* Cavo MCUboot      — flash del firmware Zephyr firmato (merged / solo app)
* OTA Zephyr        — aggiornamento SMP/MCUmgr su modulo Zephyr
* OTA Adafruit      — aggiornamento DFU legacy Nordic su bootloader Adafruit

Chip e probe ST-Link sono comuni a tutte le operazioni via cavo e stanno nella
barra "Configurazione modulo" sopra i tab.
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import threading
from tkinter import filedialog, messagebox
import tkinter as tk
from typing import Callable, Coroutine, Sequence

import customtkinter as ctk

from nrf_flasher.flasher import (
    NrfFlasher,
    PostFlashAction,
    ProbeInfo,
    TargetChip,
    analyze_hex,
    analyze_mcuboot_hex,
)
from nrf_flasher.ota import OtaFlasher, scan_ble_devices
from nrf_flasher.ota_smp import SmpOtaFlasher, scan_smp_devices

APP_TITLE = "nRF52 Programmer & OTA"
APP_VERSION = "2.2.0"

NO_DEVICE = "(nessun dispositivo)"
NO_FILE = "Nessun file selezionato"


class ToolTip:
    def __init__(self, widget: tk.Widget | ctk.CTkBaseClass, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tw: tk.Toplevel | None = None
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)

    def enter(self, event: tk.Event | None = None) -> None:
        x, y, cx, cy = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        self.tw.attributes('-topmost', True)
        label = tk.Label(
            self.tw, text=self.text, justify='left',
            background="#2b2b2b", foreground="#e0e0e0", relief='solid', borderwidth=1,
            font=("Segoe UI", "9", "normal"), padx=4, pady=2
        )
        label.pack(ipadx=1)

    def leave(self, event: tk.Event | None = None) -> None:
        if self.tw:
            self.tw.destroy()
            self.tw = None


class OtaTab:
    """Un tab OTA completo e autosufficiente.

    I due protocolli (SMP di Zephyr e DFU legacy di Adafruit) hanno la stessa
    forma di interazione — scansiona, scegli lo .zip, invia — ma backend e
    dispositivi bersaglio diversi, quindi la UI è condivisa e le differenze
    arrivano da `scan_fn` e `flasher`.
    """

    def __init__(
        self,
        app: "NrfFlasherApp",
        parent: ctk.CTkFrame,
        *,
        description: str,
        zip_label: str,
        zip_hint: str,
        scan_fn: Callable[[float], Coroutine[None, None, Sequence]],
        flasher,
    ) -> None:
        self._app = app
        self._scan_fn = scan_fn
        self._flasher = flasher
        self._zip_path: Path | None = None
        self._devices: list = []

        parent.grid_columnconfigure(0, weight=1)

        form = ctk.CTkFrame(parent)
        form.grid(row=0, column=0, padx=0, pady=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            form,
            text=description,
            text_color="gray",
            wraplength=620,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 6), sticky="w")

        # --- Dispositivo BLE ---
        ctk.CTkLabel(form, text="Dispositivo BLE").grid(
            row=1, column=0, padx=12, pady=10, sticky="w"
        )
        ble_row = ctk.CTkFrame(form, fg_color="transparent")
        ble_row.grid(row=1, column=1, padx=12, pady=10, sticky="ew")
        ble_row.grid_columnconfigure(0, weight=1)

        self._ble_var = ctk.StringVar(value=NO_DEVICE)
        self._ble_menu = ctk.CTkOptionMenu(
            ble_row, variable=self._ble_var, values=[NO_DEVICE], state="disabled"
        )
        self._ble_menu.grid(row=0, column=0, sticky="ew")

        self._scan_btn = ctk.CTkButton(
            ble_row, text="Scansiona", width=100, command=self.start_scan
        )
        self._scan_btn.grid(row=0, column=1, padx=(8, 0))
        ToolTip(
            self._scan_btn,
            "Scansione BLE di 3 secondi.\n"
            "I dispositivi marcati [DFU] espongono il servizio giusto\n"
            "per questo protocollo e vengono preselezionati."
        )

        # --- Pacchetto ---
        ctk.CTkLabel(form, text=zip_label).grid(
            row=2, column=0, padx=12, pady=10, sticky="w"
        )
        zip_row = ctk.CTkFrame(form, fg_color="transparent")
        zip_row.grid(row=2, column=1, padx=12, pady=10, sticky="ew")
        zip_row.grid_columnconfigure(0, weight=1)

        self._zip_var = ctk.StringVar(value=NO_FILE)
        zip_entry = ctk.CTkEntry(zip_row, textvariable=self._zip_var, state="readonly")
        zip_entry.grid(row=0, column=0, sticky="ew")
        ToolTip(zip_entry, zip_hint)

        self._zip_browse_btn = ctk.CTkButton(
            zip_row, text="Sfoglia…", width=100, command=self.browse_zip
        )
        self._zip_browse_btn.grid(row=0, column=1, padx=(8, 0))

        self._zip_clear_btn = ctk.CTkButton(
            zip_row,
            text="✕",
            width=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "#DCE4EE"),
            command=self.clear_zip,
        )
        self._zip_clear_btn.grid(row=0, column=2, padx=(6, 0))

        # --- Progresso ---
        self._progress = ctk.CTkProgressBar(form)
        self._progress.grid(row=3, column=0, columnspan=2, padx=12, pady=(12, 12), sticky="ew")
        self._progress.set(0)

        # --- Azioni (dentro al tab, non sotto al tabview) ---
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=1, column=0, padx=0, pady=(14, 4), sticky="ew")

        self._flash_btn = ctk.CTkButton(
            actions,
            text="Avvia aggiornamento OTA",
            height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self.start_flash,
        )
        self._flash_btn.pack(side="left")

        self._status_var = ctk.StringVar(value="Pronto")
        ctk.CTkLabel(actions, textvariable=self._status_var).pack(side="left", padx=16)

    # --- scansione ---

    def start_scan(self) -> None:
        self._app.log("Avvio scansione BLE (3 secondi)…")
        self._scan_btn.configure(state="disabled")
        self._ble_menu.configure(state="disabled")
        self._ble_var.set("Scansione in corso…")

        def run_scan() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                devices = loop.run_until_complete(self._scan_fn(3.0))
                self._app.after(0, self._on_scan_done, devices)
            except Exception as e:  # noqa: BLE001 — la UI deve restare usabile
                self._app.after(0, lambda: self._app.log(f"Errore scansione: {e}"))
                self._app.after(0, self._on_scan_done, [])
            finally:
                loop.close()

        threading.Thread(target=run_scan, daemon=True).start()

    def _on_scan_done(self, devices: Sequence) -> None:
        self._scan_btn.configure(state="normal")
        self._devices = list(devices)

        if not devices:
            self._ble_var.set("(nessun dispositivo trovato)")
            self._ble_menu.configure(
                values=["(nessun dispositivo trovato)"], state="disabled"
            )
            self._app.log("Nessun dispositivo BLE trovato.")
            return

        values = []
        preselect = 0
        found_dfu = False
        for i, d in enumerate(devices):
            prefix = "[DFU] " if d.is_dfu else ""
            values.append(f"{prefix}{d.name} ({d.address})")
            if d.is_dfu and not found_dfu:
                preselect = i
                found_dfu = True

        self._ble_menu.configure(values=values, state="normal")
        self._ble_var.set(values[preselect])
        dfu_count = sum(1 for d in devices if d.is_dfu)
        self._app.log(
            f"Trovati {len(devices)} dispositivi ({dfu_count} compatibili con questo protocollo)."
        )

    # --- pacchetto ---

    def browse_zip(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Seleziona pacchetto OTA",
            filetypes=[("Archivi ZIP", "*.zip")],
        )
        if path_str:
            self._zip_path = Path(path_str)
            self._zip_var.set(str(self._zip_path))
            self._app.log(f"Selezionato pacchetto OTA: {self._zip_path}")

    def clear_zip(self) -> None:
        self._zip_path = None
        self._zip_var.set(NO_FILE)

    # --- flash ---

    def start_flash(self) -> None:
        if not self._zip_path:
            messagebox.showwarning("Attenzione", "Seleziona prima un pacchetto .zip")
            return

        sel_str = self._ble_var.get()
        if "(nessun" in sel_str or not self._devices:
            messagebox.showwarning(
                "Attenzione", "Scansiona e seleziona un dispositivo BLE."
            )
            return

        try:
            mac_address = sel_str.split("(")[-1].split(")")[0]
        except Exception:  # noqa: BLE001
            self._app.log("Impossibile determinare il MAC address.")
            return

        self._app.set_busy(True)
        self._status_var.set("In esecuzione…")
        self._progress.set(0)
        self._app.log("—" * 40)
        self._app.log(f"Avvio OTA verso {mac_address}…")

        self._flasher.flash_async(
            zip_path=self._zip_path,
            mac_address=mac_address,
            on_log=self._app.log,
            on_progress=self._update_progress,
            on_done=self._on_flash_done,
        )

    def _update_progress(self, progress: float) -> None:
        self._app.after(0, lambda: self._progress.set(progress))

    def _on_flash_done(self, success: bool, msg: str) -> None:
        def finish() -> None:
            self._app.log(msg)
            if success:
                self._status_var.set("Completato")
                self._progress.set(1.0)
            else:
                self._status_var.set("Errore")
            self._app.set_busy(False)
            if success:
                messagebox.showinfo("Completato", msg)
            else:
                messagebox.showerror("Errore OTA", msg)

        self._app.after(0, finish)

    # --- stato ---

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._scan_btn.configure(state=state)
        self._zip_browse_btn.configure(state=state)
        self._zip_clear_btn.configure(state=state)
        self._flash_btn.configure(state=state)
        self._ble_menu.configure(
            state="normal" if (not busy and self._devices) else "disabled"
        )
        if not busy and self._status_var.get() == "In esecuzione…":
            self._status_var.set("Pronto")


class NrfFlasherApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._flasher = NrfFlasher()
        self._legacy_ota_flasher = OtaFlasher()
        self._smp_ota_flasher = SmpOtaFlasher()
        self._hex_path: Path | None = None
        self._sd_path: Path | None = None
        self._mcuboot_hex_path: Path | None = None
        self._mcuboot_hex_kind: str = "unknown"
        self._probes: list[ProbeInfo] = []

        self._build_ui()
        self.after(300, self._refresh_probes)

    # ======================================================
    # COSTRUZIONE UI
    # ======================================================

    def _build_ui(self) -> None:
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("880x860")
        self.minsize(820, 760)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkLabel(
            self,
            text="Programmatore nRF52832 / nRF52840",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.grid(row=0, column=0, padx=20, pady=(18, 2), sticky="w")

        subtitle = ctk.CTkLabel(
            self,
            text="Via cavo con ST-Link V2 (pyOCD) oppure OTA Bluetooth LE — "
                 "SMP per Zephyr, DFU legacy per Adafruit",
            text_color="gray",
        )
        subtitle.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        self._build_config_bar()

        # --- TABVIEW ---
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=3, column=0, padx=20, pady=(4, 8), sticky="nsew")

        self.tab_stlink = self.tabview.add("Cavo ST-Link")
        self.tab_mcuboot = self.tabview.add("Cavo MCUboot")
        self.tab_ota_zephyr = self.tabview.add("OTA Zephyr")
        self.tab_ota_adafruit = self.tabview.add("OTA Adafruit")

        for tab in (
            self.tab_stlink,
            self.tab_mcuboot,
            self.tab_ota_zephyr,
            self.tab_ota_adafruit,
        ):
            tab.grid_columnconfigure(0, weight=1)

        self._build_tab_stlink()
        self._build_tab_mcuboot()

        self._ota_zephyr = OtaTab(
            self,
            self.tab_ota_zephyr,
            description=(
                "Aggiornamento SMP / MCUmgr per moduli con Zephyr e MCUboot. "
                "Richiede il pacchetto dfu_application.zip prodotto da sysbuild. "
                "L'immagine viene caricata nello slot secondario e marcata come "
                "\"test\": al riavvio MCUboot la valida con la firma."
            ),
            zip_label="Pacchetto Zephyr (.zip)",
            zip_hint=(
                "dfu_application.zip generato dalla build sysbuild.\n"
                "Il manifest contiene un array \"files\" con app_update.bin."
            ),
            scan_fn=scan_smp_devices,
            flasher=self._smp_ota_flasher,
        )

        self._ota_adafruit = OtaTab(
            self,
            self.tab_ota_adafruit,
            description=(
                "Aggiornamento DFU legacy Nordic per moduli col bootloader "
                "Adafruit (Arduino nRF52). Richiede il pacchetto .zip prodotto "
                "da nrfutil / Arduino IDE. Il modulo deve essere in modalità DFU "
                "(doppio reset, oppure comando dall'app) e appare come DfuTarg."
            ),
            zip_label="Pacchetto nrfutil (.zip)",
            zip_hint=(
                "ZIP generato da nrfutil o dall'export Arduino.\n"
                "Il manifest contiene manifest.application con i file .dat e .bin."
            ),
            scan_fn=scan_ble_devices,
            flasher=self._legacy_ota_flasher,
        )

        self._build_log()

    def _build_config_bar(self) -> None:
        """Chip e probe: comuni a tutte le operazioni via cavo."""
        bar = ctk.CTkFrame(self)
        bar.grid(row=2, column=0, padx=20, pady=(0, 4), sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        bar.grid_columnconfigure(3, weight=2)

        ctk.CTkLabel(
            bar,
            text="Configurazione modulo",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 0), sticky="w")

        ctk.CTkLabel(
            bar,
            text="Valgono per entrambi i tab \"Cavo\". I tab OTA non li usano.",
            text_color="gray",
        ).grid(row=1, column=0, columnspan=4, padx=12, pady=(0, 8), sticky="w")

        ctk.CTkLabel(bar, text="Microcontrollore").grid(
            row=2, column=0, padx=12, pady=(0, 12), sticky="w"
        )
        self._target_var = ctk.StringVar(value=TargetChip.NRF52840.label)
        self._target_menu = ctk.CTkOptionMenu(
            bar,
            variable=self._target_var,
            values=[TargetChip.NRF52832.label, TargetChip.NRF52840.label],
        )
        self._target_menu.grid(row=2, column=1, padx=(0, 24), pady=(0, 12), sticky="ew")

        ctk.CTkLabel(bar, text="ST-Link").grid(
            row=2, column=2, padx=(0, 12), pady=(0, 12), sticky="w"
        )
        probe_row = ctk.CTkFrame(bar, fg_color="transparent")
        probe_row.grid(row=2, column=3, padx=(0, 12), pady=(0, 12), sticky="ew")
        probe_row.grid_columnconfigure(0, weight=1)

        self._probe_var = ctk.StringVar(value="(nessun probe)")
        self._probe_menu = ctk.CTkOptionMenu(
            probe_row, variable=self._probe_var, values=["(nessun probe)"]
        )
        self._probe_menu.grid(row=0, column=0, sticky="ew")

        self._refresh_btn = ctk.CTkButton(
            probe_row, text="Aggiorna", width=100, command=self._refresh_probes
        )
        self._refresh_btn.grid(row=0, column=1, padx=(8, 0))

    def _build_tab_stlink(self) -> None:
        form = ctk.CTkFrame(self.tab_stlink)
        form.grid(row=0, column=0, padx=0, pady=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            form,
            text="Flash \"classico\" Nordic/Adafruit: applicazione, SoftDevice "
                 "opzionale, trampolino MBR e registri post-programmazione.",
            text_color="gray",
            wraplength=620,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 6), sticky="w")

        ctk.CTkLabel(form, text="Firmware HEX").grid(
            row=1, column=0, padx=12, pady=10, sticky="w"
        )
        hex_row = ctk.CTkFrame(form, fg_color="transparent")
        hex_row.grid(row=1, column=1, padx=12, pady=10, sticky="ew")
        hex_row.grid_columnconfigure(0, weight=1)

        self._hex_var = ctk.StringVar(value=NO_FILE)
        self._hex_label = ctk.CTkEntry(
            hex_row, textvariable=self._hex_var, state="readonly"
        )
        self._hex_label.grid(row=0, column=0, sticky="ew")

        self._browse_btn = ctk.CTkButton(
            hex_row, text="Sfoglia…", width=100, command=self._browse_hex
        )
        self._browse_btn.grid(row=0, column=1, padx=(8, 0))

        self._hex_clear_btn = ctk.CTkButton(
            hex_row,
            text="✕",
            width=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "#DCE4EE"),
            command=self._clear_hex,
        )
        self._hex_clear_btn.grid(row=0, column=2, padx=(6, 0))

        ctk.CTkLabel(form, text="SoftDevice (opz.)").grid(
            row=2, column=0, padx=12, pady=10, sticky="w"
        )
        sd_row = ctk.CTkFrame(form, fg_color="transparent")
        sd_row.grid(row=2, column=1, padx=12, pady=10, sticky="ew")
        sd_row.grid_columnconfigure(0, weight=1)

        self._sd_var = ctk.StringVar(value="Nessuno (solo firmware)")
        self._sd_label = ctk.CTkEntry(
            sd_row, textvariable=self._sd_var, state="readonly"
        )
        self._sd_label.grid(row=0, column=0, sticky="ew")

        self._sd_browse_btn = ctk.CTkButton(
            sd_row, text="Sfoglia…", width=100, command=self._browse_sd
        )
        self._sd_browse_btn.grid(row=0, column=1, padx=(8, 0))

        self._sd_clear_btn = ctk.CTkButton(
            sd_row,
            text="✕",
            width=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "#DCE4EE"),
            command=self._clear_sd,
        )
        self._sd_clear_btn.grid(row=0, column=2, padx=(6, 0))

        # Opzioni: su griglia invece che tutte in fila, altrimenti la terza
        # checkbox finisce fuori dalla finestra
        options = ctk.CTkFrame(form, fg_color="transparent")
        options.grid(row=3, column=0, columnspan=2, padx=12, pady=(4, 8), sticky="w")

        self._erase_var = ctk.BooleanVar(value=False)
        erase_check = ctk.CTkCheckBox(
            options,
            text="Cancella tutta la flash prima di programmare",
            variable=self._erase_var,
        )
        erase_check.grid(row=0, column=0, padx=(0, 24), pady=4, sticky="w")
        ToolTip(
            erase_check,
            "ATTENZIONE: Esegue un 'mass erase' del chip.\n"
            "Questo cancellerà anche il Bootloader, il SoftDevice e l'UICR.\n"
            "Usa questa opzione solo se vuoi ripartire da un chip completamente vuoto."
        )

        self._reset_var = ctk.BooleanVar(value=True)
        reset_check = ctk.CTkCheckBox(
            options,
            text="Reset dopo la programmazione",
            variable=self._reset_var,
        )
        reset_check.grid(row=0, column=1, padx=(0, 16), pady=4, sticky="w")
        ToolTip(
            reset_check,
            "Esegue un reset hardware (pin o SYSRESETREQ) al termine del flash.\n"
            "Necessario affinché il nuovo firmware inizi l'esecuzione."
        )

        self._trampoline_var = ctk.BooleanVar(value=True)
        self._trampoline_check = ctk.CTkCheckBox(
            options,
            text="Aggiungi trampolino MBR a 0x0 se l'app parte da 0x1000",
            variable=self._trampoline_var,
        )
        self._trampoline_check.grid(
            row=1, column=0, columnspan=2, padx=(0, 16), pady=4, sticky="w"
        )
        ToolTip(
            self._trampoline_check,
            "Se stai flashando un'app linkata a 0x1000 (es. OpenThread) senza SoftDevice/MBR,\n"
            "questa opzione inietta un piccolo 'trampolino' a 0x0 per avviarla.\n"
            "Se c'è un SoftDevice selezionato, questa opzione viene ignorata."
        )

        # --- Opzioni post-programmazione ---
        post_frame = ctk.CTkFrame(form, fg_color="transparent")
        post_frame.grid(row=4, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w")

        ctk.CTkLabel(
            post_frame, text="Dopo la programmazione:", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, padx=(0, 12), pady=4, sticky="w")

        self._post_action_var = ctk.StringVar(value=PostFlashAction.ERASE_UICR.value)
        self._post_action_radios: list[ctk.CTkRadioButton] = []

        action_tooltips = {
            PostFlashAction.NONE: (
                "Non modifica alcun registro post-programmazione.\n"
                "Utile se flashi un SoftDevice o un Bootloader da solo."
            ),
            PostFlashAction.ERASE_UICR: (
                "Cancella l'UICR in modo che il SoftDevice non trovi il bootloader.\n"
                "L'applicazione si avvierà ignorando i controlli CRC.\n"
                "ATTENZIONE: questo disabiliterà gli aggiornamenti OTA via BLE."
            ),
            PostFlashAction.WRITE_BL_SETTINGS: (
                "Simula il processo di nrfutil: calcola il CRC-16 dell'app\n"
                "e scrive la pagina Bootloader Settings (0x7F000).\n"
                "L'Adafruit Bootloader validerà l'app e la avvierà normalmente.\n"
                "CONSIGLIATO: Mantiene attivi gli aggiornamenti OTA via BLE."
            ),
        }

        for i, action in enumerate(PostFlashAction):
            rb = ctk.CTkRadioButton(
                post_frame,
                text=action.label,
                variable=self._post_action_var,
                value=action.value,
            )
            rb.grid(row=1, column=i, padx=(0, 16), pady=4, sticky="w")
            ToolTip(rb, action_tooltips[action])
            self._post_action_radios.append(rb)

        # --- Azioni: dentro al tab (prima erano gridate sulla finestra e
        # restavano visibili anche negli altri tab) ---
        actions = ctk.CTkFrame(self.tab_stlink, fg_color="transparent")
        actions.grid(row=1, column=0, padx=0, pady=(14, 4), sticky="ew")

        self._flash_btn = ctk.CTkButton(
            actions,
            text="Programma",
            height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_flash,
        )
        self._flash_btn.pack(side="left")

        self._erase_btn = ctk.CTkButton(
            actions,
            text="Cancella solo",
            height=40,
            fg_color="transparent",
            border_width=2,
            text_color=("gray10", "#DCE4EE"),
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_erase,
        )
        self._erase_btn.pack(side="left", padx=(10, 0))

        self._status_var = ctk.StringVar(value="Pronto")
        ctk.CTkLabel(actions, textvariable=self._status_var).pack(side="left", padx=16)

    def _build_tab_mcuboot(self) -> None:
        form = ctk.CTkFrame(self.tab_mcuboot)
        form.grid(row=0, column=0, padx=0, pady=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            form,
            text="Flash via cavo del firmware Zephyr con bootloader MCUboot. "
                 "MCUboot valida l'immagine con la propria firma: non servono "
                 "trampolino MBR né scritture UICR.",
            text_color="gray",
            wraplength=620,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 6), sticky="w")

        ctk.CTkLabel(form, text="File HEX").grid(
            row=1, column=0, padx=12, pady=10, sticky="w"
        )
        mcuboot_hex_row = ctk.CTkFrame(form, fg_color="transparent")
        mcuboot_hex_row.grid(row=1, column=1, padx=12, pady=10, sticky="ew")
        mcuboot_hex_row.grid_columnconfigure(0, weight=1)

        self._mcuboot_hex_var = ctk.StringVar(value=NO_FILE)
        self._mcuboot_hex_label = ctk.CTkEntry(
            mcuboot_hex_row, textvariable=self._mcuboot_hex_var, state="readonly"
        )
        self._mcuboot_hex_label.grid(row=0, column=0, sticky="ew")

        self._mcuboot_browse_btn = ctk.CTkButton(
            mcuboot_hex_row, text="Sfoglia…", width=100, command=self._browse_mcuboot_hex
        )
        self._mcuboot_browse_btn.grid(row=0, column=1, padx=(8, 0))

        self._mcuboot_clear_btn = ctk.CTkButton(
            mcuboot_hex_row,
            text="✕",
            width=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "#DCE4EE"),
            command=self._clear_mcuboot_hex,
        )
        self._mcuboot_clear_btn.grid(row=0, column=2, padx=(6, 0))

        mcuboot_mode_frame = ctk.CTkFrame(form, fg_color="transparent")
        mcuboot_mode_frame.grid(
            row=2, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w"
        )

        ctk.CTkLabel(
            mcuboot_mode_frame, text="Modalità:", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, padx=(0, 12), pady=4, sticky="w")

        self._mcuboot_mode_var = ctk.StringVar(value="merged")
        self._mcuboot_mode_radios: list[ctk.CTkRadioButton] = []

        rb_merged = ctk.CTkRadioButton(
            mcuboot_mode_frame,
            text="Chip completo (merged.hex)",
            variable=self._mcuboot_mode_var,
            value="merged",
        )
        rb_merged.grid(row=1, column=0, padx=(0, 16), pady=4, sticky="w")
        ToolTip(
            rb_merged,
            "Mass erase + scrittura di MCUboot e applicazione firmata.\n"
            "Da usare al primo flash o per migrare un modulo dal bootloader\n"
            "Adafruit. ATTENZIONE: cancella tutto, incluso il vecchio\n"
            "bootloader, il SoftDevice, l'UICR e le soglie salvate."
        )
        self._mcuboot_mode_radios.append(rb_merged)

        rb_app = ctk.CTkRadioButton(
            mcuboot_mode_frame,
            text="Solo applicazione (zephyr.signed.hex)",
            variable=self._mcuboot_mode_var,
            value="app",
        )
        rb_app.grid(row=1, column=1, padx=(0, 16), pady=4, sticky="w")
        ToolTip(
            rb_app,
            "Scrive solo l'applicazione firmata nello slot primario (0xC000).\n"
            "Conserva MCUboot e la partizione settings (soglie classificatore).\n"
            "Richiede che sul modulo ci sia già MCUboot."
        )
        self._mcuboot_mode_radios.append(rb_app)

        mcuboot_actions = ctk.CTkFrame(self.tab_mcuboot, fg_color="transparent")
        mcuboot_actions.grid(row=1, column=0, padx=0, pady=(14, 4), sticky="ew")

        self._mcuboot_flash_btn = ctk.CTkButton(
            mcuboot_actions,
            text="Programma",
            height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_mcuboot_flash,
        )
        self._mcuboot_flash_btn.pack(side="left")

        # Stessa variabile del tab ST-Link: una sola operazione via cavo può
        # essere in corso, quindi lo stato è unico e visibile da entrambi
        ctk.CTkLabel(mcuboot_actions, textvariable=self._status_var).pack(
            side="left", padx=16
        )

    def _build_log(self) -> None:
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=4, column=0, padx=20, pady=(8, 20), sticky="nsew")
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_frame, text="Log", anchor="w").grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w"
        )
        self._log_box = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self._log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

    # ======================================================
    # STATO COMUNE
    # ======================================================

    def log(self, message: str) -> None:
        def append() -> None:
            self._log_box.configure(state="normal")
            self._log_box.insert("end", message + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")

        self.after(0, append)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"

        # Barra di configurazione
        self._refresh_btn.configure(state=state)
        self._target_menu.configure(state=state)
        self._probe_menu.configure(state=state)

        # Tab ST-Link
        self._flash_btn.configure(state=state)
        self._erase_btn.configure(state=state)
        self._browse_btn.configure(state=state)
        self._hex_clear_btn.configure(state=state)
        self._sd_browse_btn.configure(state=state)
        self._sd_clear_btn.configure(state=state)
        for rb in self._post_action_radios:
            rb.configure(state=state)

        # Tab MCUboot
        self._mcuboot_browse_btn.configure(state=state)
        self._mcuboot_clear_btn.configure(state=state)
        self._mcuboot_flash_btn.configure(state=state)
        for rb in self._mcuboot_mode_radios:
            rb.configure(state=state)

        # Tab OTA
        self._ota_zephyr.set_busy(busy)
        self._ota_adafruit.set_busy(busy)

        if not busy:
            self._update_trampoline_state()

    def _update_trampoline_state(self) -> None:
        # Con un SoftDevice selezionato l'MBR è già incluso: il trampolino
        # non serve e viene disabilitato per chiarezza.
        if self._sd_path is not None:
            self._trampoline_check.configure(state="disabled")
        else:
            self._trampoline_check.configure(state="normal")

    def _on_flash_done(self, success: bool, message: str) -> None:
        def finish() -> None:
            self._status_var.set("Completato" if success else "Errore")
            self.set_busy(False)
            self.log(message)
            if success:
                messagebox.showinfo("Completato", message)
            else:
                messagebox.showerror("Errore", message)

        self.after(0, finish)

    # ======================================================
    # PROBE E TARGET
    # ======================================================

    def _refresh_probes(self) -> None:
        self.log("Ricerca probe ST-Link…")
        try:
            self._probes = self._flasher.list_probes()
        except Exception as exc:  # noqa: BLE001
            self._probes = []
            self.log(f"Errore ricerca probe: {exc}")
            messagebox.showerror("Errore probe", str(exc))
            return

        if not self._probes:
            self._probe_menu.configure(values=["(nessun probe)"])
            self._probe_var.set("(nessun probe)")
            self.log("Nessun ST-Link rilevato.")
            return

        labels = [p.display_name for p in self._probes]
        self._probe_menu.configure(values=labels)
        self._probe_var.set(labels[0])
        self.log(f"Trovati {len(self._probes)} probe.")

    def _selected_target(self) -> TargetChip:
        label = self._target_var.get()
        if label == TargetChip.NRF52832.label:
            return TargetChip.NRF52832
        return TargetChip.NRF52840

    def _selected_probe_uid(self) -> str | None:
        if not self._probes:
            return None
        selected = self._probe_var.get()
        for probe in self._probes:
            if probe.display_name == selected:
                return probe.unique_id
        return self._probes[0].unique_id

    def _require_probe(self) -> bool:
        if self._probes:
            return True
        messagebox.showwarning(
            "ST-Link non trovato",
            "Collega un ST-Link V2 e premi Aggiorna nella barra "
            "\"Configurazione modulo\".",
        )
        return False

    # ======================================================
    # TAB ST-LINK
    # ======================================================

    def _log_hex_analysis(self, path: Path, role: str) -> None:
        try:
            info = analyze_hex(path)
        except Exception as exc:  # noqa: BLE001 — analisi solo informativa
            self.log(f"Impossibile analizzare {path.name}: {exc}")
            return
        self.log(f"{role}: {info.description}")

    def _browse_hex(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleziona firmware HEX",
            filetypes=[("Intel HEX", "*.hex"), ("Tutti i file", "*.*")],
        )
        if path:
            self._hex_path = Path(path)
            self._hex_var.set(str(self._hex_path))
            self.log(f"Selezionato firmware: {self._hex_path.name}")
            self._log_hex_analysis(self._hex_path, "Analisi firmware")

    def _clear_hex(self) -> None:
        self._hex_path = None
        self._hex_var.set(NO_FILE)

    def _browse_sd(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleziona SoftDevice HEX",
            filetypes=[("Intel HEX", "*.hex"), ("Tutti i file", "*.*")],
        )
        if path:
            self._sd_path = Path(path)
            self._sd_var.set(str(self._sd_path))
            self.log(f"Selezionato SoftDevice: {self._sd_path.name}")
            self._log_hex_analysis(self._sd_path, "Analisi SoftDevice")
            self._update_trampoline_state()

    def _clear_sd(self) -> None:
        self._sd_path = None
        self._sd_var.set("Nessuno (solo firmware)")
        self._update_trampoline_state()

    def _start_flash(self) -> None:
        if self._flasher.busy:
            return

        if self._hex_path is None and self._sd_path is None:
            messagebox.showwarning(
                "File mancante", "Seleziona un firmware HEX e/o un SoftDevice."
            )
            return

        if not self._require_probe():
            return

        parts = []
        if self._sd_path is not None:
            parts.append(f"SoftDevice {self._sd_path.name}")
        if self._hex_path is not None:
            parts.append(f"firmware {self._hex_path.name}")
        if not messagebox.askyesno(
            "Conferma",
            f"Programmare {' + '.join(parts)} su {self._target_var.get()}?",
        ):
            return

        self.set_busy(True)
        self._status_var.set("Programmazione in corso…")
        self.log("—" * 40)
        self.log("Avvio programmazione…")

        self._flasher.flash_async(
            hex_path=self._hex_path,
            target=self._selected_target(),
            probe_uid=self._selected_probe_uid(),
            erase_all=self._erase_var.get(),
            reset_after=self._reset_var.get(),
            on_log=self.log,
            on_done=self._on_flash_done,
            add_mbr_trampoline=self._trampoline_var.get(),
            softdevice_path=self._sd_path,
            post_flash_action=PostFlashAction(self._post_action_var.get()),
        )

    def _start_erase(self) -> None:
        if self._flasher.busy:
            return

        if not self._require_probe():
            return

        if not messagebox.askyesno(
            "Conferma Cancellazione",
            f"Cancellare completamente la memoria di {self._target_var.get()}?\n"
            "Tutti i dati verranno persi.",
            icon="warning",
        ):
            return

        self.set_busy(True)
        self._status_var.set("Cancellazione in corso…")
        self.log("—" * 40)
        self.log("Avvio cancellazione completa…")

        self._flasher.flash_async(
            hex_path=None,
            target=self._selected_target(),
            probe_uid=self._selected_probe_uid(),
            erase_all=True,
            reset_after=self._reset_var.get(),
            on_log=self.log,
            on_done=self._on_flash_done,
        )

    # ======================================================
    # TAB MCUBOOT
    # ======================================================

    def _browse_mcuboot_hex(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Seleziona merged.hex o zephyr.signed.hex",
            filetypes=[("Intel HEX", "*.hex"), ("Tutti i file", "*.*")],
        )
        if not path_str:
            return
        self._mcuboot_hex_path = Path(path_str)
        self._mcuboot_hex_var.set(str(self._mcuboot_hex_path))
        self.log(f"Selezionato firmware MCUboot: {self._mcuboot_hex_path.name}")
        try:
            info = analyze_mcuboot_hex(self._mcuboot_hex_path)
        except Exception as exc:  # noqa: BLE001 — analisi solo informativa
            self._mcuboot_hex_kind = "unknown"
            self.log(f"Impossibile analizzare {self._mcuboot_hex_path.name}: {exc}")
            return
        self._mcuboot_hex_kind = info.kind
        self.log(f"Analisi firmware: {info.description}")
        # preseleziona la modalità coerente col file scelto
        if info.kind in ("merged", "app"):
            self._mcuboot_mode_var.set(info.kind)

    def _clear_mcuboot_hex(self) -> None:
        self._mcuboot_hex_path = None
        self._mcuboot_hex_kind = "unknown"
        self._mcuboot_hex_var.set(NO_FILE)

    def _start_mcuboot_flash(self) -> None:
        if self._flasher.busy:
            return

        if self._mcuboot_hex_path is None:
            messagebox.showwarning(
                "File mancante",
                "Seleziona un file HEX (merged.hex o zephyr.signed.hex).",
            )
            return

        if not self._require_probe():
            return

        mode = self._mcuboot_mode_var.get()

        # coerenza file/modalità: un merged.hex flashato senza mass erase o una
        # app firmata flashata con mass erase lasciano il modulo non avviabile
        if self._mcuboot_hex_kind != "unknown" and self._mcuboot_hex_kind != mode:
            expected = (
                "Chip completo" if self._mcuboot_hex_kind == "merged"
                else "Solo applicazione"
            )
            if not messagebox.askyesno(
                "Modalità incoerente",
                f"Il file selezionato sembra richiedere la modalità "
                f"\"{expected}\", ma è selezionata l'altra.\n"
                "Continuare comunque?",
                icon="warning",
            ):
                return
        if self._mcuboot_hex_kind == "unknown":
            if not messagebox.askyesno(
                "File non riconosciuto",
                "Il file non sembra una build sysbuild del progetto Zephyr "
                "(nessun header immagine MCUboot a 0xC000).\n"
                "Continuare comunque?",
                icon="warning",
            ):
                return

        if mode == "merged":
            confirm_msg = (
                f"Programmare {self._mcuboot_hex_path.name} su "
                f"{self._target_var.get()} con CANCELLAZIONE COMPLETA?\n\n"
                "Verranno cancellati il vecchio bootloader (Adafruit), il "
                "SoftDevice, l'UICR e le soglie salvate."
            )
        else:
            confirm_msg = (
                f"Aggiornare la sola applicazione con "
                f"{self._mcuboot_hex_path.name} su {self._target_var.get()}?\n\n"
                "MCUboot e la partizione settings restano intatti."
            )
        if not messagebox.askyesno(
            "Conferma", confirm_msg, icon="warning" if mode == "merged" else "question"
        ):
            return

        self.set_busy(True)
        self._status_var.set("Programmazione in corso…")
        self.log("—" * 40)
        self.log(
            f"Avvio programmazione MCUboot "
            f"({'chip completo' if mode == 'merged' else 'solo applicazione'})…"
        )

        # Niente trampolino MBR né azioni post-flash legacy: MCUboot valida le
        # immagini con la propria firma, non servono UICR/CRC del mondo Adafruit.
        self._flasher.flash_async(
            hex_path=self._mcuboot_hex_path,
            target=self._selected_target(),
            probe_uid=self._selected_probe_uid(),
            erase_all=(mode == "merged"),
            reset_after=True,
            on_log=self.log,
            on_done=self._on_flash_done,
            add_mbr_trampoline=False,
            softdevice_path=None,
            post_flash_action=PostFlashAction.NONE,
        )


def main() -> None:
    app = NrfFlasherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
