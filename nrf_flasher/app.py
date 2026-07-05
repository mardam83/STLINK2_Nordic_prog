"""UI per programmare nRF52832 / nRF52840 con ST-Link V2."""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk

import customtkinter as ctk

from nrf_flasher.flasher import NrfFlasher, PostFlashAction, ProbeInfo, TargetChip, analyze_hex

APP_TITLE = "nRF52 ST-Link Flasher"
APP_VERSION = "1.2.0"


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


class NrfFlasherApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._flasher = NrfFlasher()
        self._hex_path: Path | None = None
        self._sd_path: Path | None = None
        self._probes: list[ProbeInfo] = []

        self._build_ui()
        self.after(300, self._refresh_probes)

    def _build_ui(self) -> None:
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("720x700")
        self.minsize(640, 600)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        header = ctk.CTkLabel(
            self,
            text="Programmatore nRF52832 / nRF52840",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")

        subtitle = ctk.CTkLabel(
            self,
            text="ST-Link V2 · firmware + SoftDevice (Intel HEX, anche build Arduino)",
            text_color="gray",
        )
        subtitle.grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        form = ctk.CTkFrame(self)
        form.grid(row=2, column=0, padx=20, pady=8, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Microcontrollore").grid(
            row=0, column=0, padx=12, pady=10, sticky="w"
        )
        self._target_var = ctk.StringVar(value=TargetChip.NRF52840.label)
        self._target_menu = ctk.CTkOptionMenu(
            form,
            variable=self._target_var,
            values=[TargetChip.NRF52832.label, TargetChip.NRF52840.label],
        )
        self._target_menu.grid(row=0, column=1, padx=12, pady=10, sticky="ew")

        ctk.CTkLabel(form, text="ST-Link").grid(
            row=1, column=0, padx=12, pady=10, sticky="w"
        )
        probe_row = ctk.CTkFrame(form, fg_color="transparent")
        probe_row.grid(row=1, column=1, padx=12, pady=10, sticky="ew")
        probe_row.grid_columnconfigure(0, weight=1)

        self._probe_var = ctk.StringVar(value="(nessun probe)")
        self._probe_menu = ctk.CTkOptionMenu(
            probe_row,
            variable=self._probe_var,
            values=["(nessun probe)"],
            width=360,
        )
        self._probe_menu.grid(row=0, column=0, sticky="ew")

        self._refresh_btn = ctk.CTkButton(
            probe_row,
            text="Aggiorna",
            width=90,
            command=self._refresh_probes,
        )
        self._refresh_btn.grid(row=0, column=1, padx=(8, 0))

        ctk.CTkLabel(form, text="Firmware HEX").grid(
            row=2, column=0, padx=12, pady=10, sticky="w"
        )
        hex_row = ctk.CTkFrame(form, fg_color="transparent")
        hex_row.grid(row=2, column=1, padx=12, pady=10, sticky="ew")
        hex_row.grid_columnconfigure(0, weight=1)

        self._hex_var = ctk.StringVar(value="Nessun file selezionato")
        self._hex_label = ctk.CTkEntry(
            hex_row,
            textvariable=self._hex_var,
            state="readonly",
        )
        self._hex_label.grid(row=0, column=0, sticky="ew")

        self._browse_btn = ctk.CTkButton(
            hex_row,
            text="Sfoglia…",
            width=90,
            command=self._browse_hex,
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
            row=3, column=0, padx=12, pady=10, sticky="w"
        )
        sd_row = ctk.CTkFrame(form, fg_color="transparent")
        sd_row.grid(row=3, column=1, padx=12, pady=10, sticky="ew")
        sd_row.grid_columnconfigure(0, weight=1)

        self._sd_var = ctk.StringVar(value="Nessuno (solo firmware)")
        self._sd_label = ctk.CTkEntry(
            sd_row,
            textvariable=self._sd_var,
            state="readonly",
        )
        self._sd_label.grid(row=0, column=0, sticky="ew")

        self._sd_browse_btn = ctk.CTkButton(
            sd_row,
            text="Sfoglia…",
            width=90,
            command=self._browse_sd,
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

        options = ctk.CTkFrame(form, fg_color="transparent")
        options.grid(row=4, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w")

        self._erase_var = ctk.BooleanVar(value=False)
        erase_check = ctk.CTkCheckBox(
            options,
            text="Cancella tutta la flash prima di programmare",
            variable=self._erase_var,
        )
        erase_check.pack(side="left", padx=(0, 16))
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
        reset_check.pack(side="left", padx=(0, 16))
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
        self._trampoline_check.pack(side="left")
        ToolTip(
            self._trampoline_check,
            "Se stai flashando un'app linkata a 0x1000 (es. OpenThread) senza SoftDevice/MBR,\n"
            "questa opzione inietta un piccolo 'trampolino' a 0x0 per avviarla.\n"
            "Se c'è un SoftDevice selezionato, questa opzione viene ignorata."
        )

        # --- Riga opzioni post-programmazione (radio buttons) ---
        post_frame = ctk.CTkFrame(form, fg_color="transparent")
        post_frame.grid(row=5, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w")

        ctk.CTkLabel(post_frame, text="Dopo la programmazione:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(0, 12)
        )

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

        for action in PostFlashAction:
            rb = ctk.CTkRadioButton(
                post_frame,
                text=action.label,
                variable=self._post_action_var,
                value=action.value,
            )
            rb.pack(side="left", padx=(0, 12))
            ToolTip(rb, action_tooltips[action])
            self._post_action_radios.append(rb)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, padx=20, pady=4, sticky="ew")

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
        ctk.CTkLabel(actions, textvariable=self._status_var).pack(
            side="left", padx=16
        )

        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=5, column=0, padx=20, pady=(8, 20), sticky="nsew")
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_frame, text="Log", anchor="w").grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w"
        )
        self._log_box = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self._log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

    def _log(self, message: str) -> None:
        def append() -> None:
            self._log_box.configure(state="normal")
            self._log_box.insert("end", message + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")

        self.after(0, append)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._flash_btn.configure(state=state)
        self._erase_btn.configure(state=state)
        self._browse_btn.configure(state=state)
        self._hex_clear_btn.configure(state=state)
        self._sd_browse_btn.configure(state=state)
        self._sd_clear_btn.configure(state=state)
        self._refresh_btn.configure(state=state)
        self._target_menu.configure(state=state)
        self._probe_menu.configure(state=state)
        if not busy:
            self._status_var.set("Pronto")
            self._update_trampoline_state()
        for rb in self._post_action_radios:
            rb.configure(state=state)

    def _update_trampoline_state(self) -> None:
        # Con un SoftDevice selezionato l'MBR è già incluso: il trampolino
        # non serve e viene disabilitato per chiarezza.
        if self._sd_path is not None:
            self._trampoline_check.configure(state="disabled")
        else:
            self._trampoline_check.configure(state="normal")

    def _log_hex_analysis(self, path: Path, role: str) -> None:
        try:
            info = analyze_hex(path)
        except Exception as exc:  # noqa: BLE001 — analisi solo informativa
            self._log(f"Impossibile analizzare {path.name}: {exc}")
            return
        self._log(f"{role}: {info.description}")

    def _browse_hex(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleziona firmware HEX",
            filetypes=[
                ("Intel HEX", "*.hex"),
                ("Tutti i file", "*.*"),
            ],
        )
        if path:
            self._hex_path = Path(path)
            self._hex_var.set(str(self._hex_path))
            self._log(f"Selezionato firmware: {self._hex_path.name}")
            self._log_hex_analysis(self._hex_path, "Analisi firmware")

    def _clear_hex(self) -> None:
        self._hex_path = None
        self._hex_var.set("Nessun file selezionato")

    def _browse_sd(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleziona SoftDevice HEX",
            filetypes=[
                ("Intel HEX", "*.hex"),
                ("Tutti i file", "*.*"),
            ],
        )
        if path:
            self._sd_path = Path(path)
            self._sd_var.set(str(self._sd_path))
            self._log(f"Selezionato SoftDevice: {self._sd_path.name}")
            self._log_hex_analysis(self._sd_path, "Analisi SoftDevice")
            self._update_trampoline_state()

    def _clear_sd(self) -> None:
        self._sd_path = None
        self._sd_var.set("Nessuno (solo firmware)")
        self._update_trampoline_state()

    def _refresh_probes(self) -> None:
        self._log("Ricerca probe ST-Link…")
        try:
            self._probes = self._flasher.list_probes()
        except Exception as exc:  # noqa: BLE001
            self._probes = []
            self._log(f"Errore ricerca probe: {exc}")
            messagebox.showerror("Errore probe", str(exc))
            return

        if not self._probes:
            self._probe_menu.configure(values=["(nessun probe)"])
            self._probe_var.set("(nessun probe)")
            self._log("Nessun ST-Link rilevato.")
            return

        labels = [p.display_name for p in self._probes]
        self._probe_menu.configure(values=labels)
        self._probe_var.set(labels[0])
        self._log(f"Trovati {len(self._probes)} probe.")

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

    def _start_flash(self) -> None:
        if self._flasher.busy:
            return

        if self._hex_path is None and self._sd_path is None:
            messagebox.showwarning(
                "File mancante",
                "Seleziona un firmware HEX e/o un SoftDevice.",
            )
            return

        if not self._probes:
            messagebox.showwarning(
                "ST-Link non trovato",
                "Collega un ST-Link V2 e premi Aggiorna.",
            )
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

        self._set_busy(True)
        self._status_var.set("Programmazione in corso…")
        self._log("—" * 40)
        self._log("Avvio programmazione…")

        self._flasher.flash_async(
            hex_path=self._hex_path,
            target=self._selected_target(),
            probe_uid=self._selected_probe_uid(),
            erase_all=self._erase_var.get(),
            reset_after=self._reset_var.get(),
            on_log=self._log,
            on_done=self._on_flash_done,
            add_mbr_trampoline=self._trampoline_var.get(),
            softdevice_path=self._sd_path,
            post_flash_action=PostFlashAction(self._post_action_var.get()),
        )

    def _start_erase(self) -> None:
        if self._flasher.busy:
            return

        if not self._probes:
            messagebox.showwarning(
                "ST-Link non trovato",
                "Collega un ST-Link V2 e premi Aggiorna.",
            )
            return

        if not messagebox.askyesno(
            "Conferma Cancellazione",
            f"Cancellare completamente la memoria di {self._target_var.get()}?\nTutti i dati verranno persi.",
            icon="warning"
        ):
            return

        self._set_busy(True)
        self._status_var.set("Cancellazione in corso…")
        self._log("—" * 40)
        self._log("Avvio cancellazione completa…")

        self._flasher.flash_async(
            hex_path=None,
            target=self._selected_target(),
            probe_uid=self._selected_probe_uid(),
            erase_all=True,
            reset_after=self._reset_var.get(),
            on_log=self._log,
            on_done=self._on_flash_done,
        )

    def _on_flash_done(self, success: bool, message: str) -> None:
        def finish() -> None:
            self._set_busy(False)
            self._log(message)
            if success:
                messagebox.showinfo("Completato", message)
            else:
                messagebox.showerror("Errore", message)

        self.after(0, finish)


def main() -> None:
    app = NrfFlasherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
