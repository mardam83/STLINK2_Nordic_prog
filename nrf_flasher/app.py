"""UI per programmare nRF52832 / nRF52840 con ST-Link V2."""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from nrf_flasher.flasher import NrfFlasher, ProbeInfo, TargetChip

APP_TITLE = "nRF52 ST-Link Flasher"
APP_VERSION = "1.0.0"


class NrfFlasherApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._flasher = NrfFlasher()
        self._hex_path: Path | None = None
        self._probes: list[ProbeInfo] = []

        self._build_ui()
        self.after(300, self._refresh_probes)

    def _build_ui(self) -> None:
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("720x560")
        self.minsize(640, 480)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkLabel(
            self,
            text="Programmatore nRF52832 / nRF52840",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")

        subtitle = ctk.CTkLabel(
            self,
            text="ST-Link V2 · caricamento file Intel HEX",
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

        ctk.CTkLabel(form, text="File HEX").grid(
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

        options = ctk.CTkFrame(form, fg_color="transparent")
        options.grid(row=3, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w")

        self._erase_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options,
            text="Cancella tutta la flash prima di programmare",
            variable=self._erase_var,
        ).pack(side="left", padx=(0, 16))

        self._reset_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            options,
            text="Reset dopo la programmazione",
            variable=self._reset_var,
        ).pack(side="left", padx=(0, 16))

        self._trampoline_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            options,
            text="Aggiungi trampolino MBR a 0x0 se l'app parte da 0x1000",
            variable=self._trampoline_var,
        ).pack(side="left")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, padx=20, pady=4, sticky="ew")

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
        log_frame.grid(row=4, column=0, padx=20, pady=(8, 20), sticky="nsew")
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
        self._refresh_btn.configure(state=state)
        self._target_menu.configure(state=state)
        self._probe_menu.configure(state=state)
        if not busy:
            self._status_var.set("Pronto")

    def _browse_hex(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleziona file HEX",
            filetypes=[
                ("Intel HEX", "*.hex"),
                ("Tutti i file", "*.*"),
            ],
        )
        if path:
            self._hex_path = Path(path)
            self._hex_var.set(str(self._hex_path))
            self._log(f"Selezionato: {self._hex_path.name}")

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

        if self._hex_path is None:
            messagebox.showwarning("File mancante", "Seleziona un file HEX.")
            return

        if not self._probes:
            messagebox.showwarning(
                "ST-Link non trovato",
                "Collega un ST-Link V2 e premi Aggiorna.",
            )
            return

        if not messagebox.askyesno(
            "Conferma",
            f"Programmare {self._hex_path.name} su {self._target_var.get()}?",
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
