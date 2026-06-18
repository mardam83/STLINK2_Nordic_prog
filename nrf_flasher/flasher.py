"""Backend di programmazione nRF52 via ST-Link (pyOCD)."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from intelhex import IntelHex

LogCallback = Callable[[str], None]
DoneCallback = Callable[[bool, str], None]

# Indirizzo della vector table di un'applicazione nRF52 che gira sotto il Nordic
# MBR (MBR_SIZE = 4 KB). Gli HEX del nRF5 SDK (es. ot-rcp) sono linkati qui e
# lasciano 0x0–0xFFF al MBR.
APP_VECTOR_TABLE_ADDR = 0x1000

# Stub Thumb-2 (22 byte) verificato col disassembly. Replica il boot-forwarding
# del Nordic MBR quando NON c'è SoftDevice/bootloader: imposta SCB->VTOR all'app
# a 0x1000, ricarica MSP e salta al reset handler dell'app. È generico perché
# legge SP e reset vector da 0x1000/0x1004 a runtime, quindi vale per qualsiasi
# applicazione (nRF52832 o nRF52840).
#   movw r0, #0xED08 ; movt r0, #0xE000   -> r0 = &SCB->VTOR (0xE000ED08)
#   movw r1, #0x1000                       -> r1 = 0x1000 (vector table app)
#   str  r1, [r0]                          -> VTOR = 0x1000
#   ldr  r2, [r1]    ; mov sp, r2          -> MSP = *(0x1000)
#   ldr  r3, [r1, #4]; bx  r3              -> salta a *(0x1004)
_MBR_TRAMPOLINE_STUB = bytes.fromhex("4ef60850cef2000041f2000101600a6895464b681847")
_MBR_TRAMPOLINE_STUB_OFFSET = 0x8  # dopo SP (0x0) e reset vector (0x4)


def build_mbr_trampoline(app_hex: "IntelHex") -> "IntelHex | None":
    """Costruisce la pagina 0x0 col trampolino MBR, se l'HEX ne ha bisogno.

    Ritorna una ``IntelHex`` contenente la sola pagina 0x0 quando l'applicazione
    ha layout "vector table a 0x1000 con 0x0 vuoto" (tipico nRF5 SDK + MBR).
    Ritorna ``None`` se l'HEX mappa già qualcosa sotto 0x1000 o se a 0x1000 non
    c'è una vector table plausibile: in quei casi non si tocca nulla.
    """
    from intelhex import IntelHex

    # 0x0–0xFFF deve essere assente nell'HEX (altrimenti c'è già MBR/SoftDevice).
    for start, _end in app_hex.segments():
        if start < APP_VECTOR_TABLE_ADDR:
            return None

    # A 0x1000 deve esserci una vector table plausibile: SP in RAM, reset in flash.
    try:
        sp = int.from_bytes(bytes(app_hex.tobinarray(start=APP_VECTOR_TABLE_ADDR, size=4)), "little")
        reset = int.from_bytes(bytes(app_hex.tobinarray(start=APP_VECTOR_TABLE_ADDR + 4, size=4)), "little")
    except Exception:  # noqa: BLE001 — HEX senza dati a 0x1000
        return None

    sp_in_ram = 0x20000000 <= sp <= 0x20040008
    reset_in_flash = bool(reset & 1) and APP_VECTOR_TABLE_ADDR <= (reset & ~1) < 0x00100000
    if not (sp_in_ram and reset_in_flash):
        return None

    page = IntelHex()
    page.puts(0x0, sp.to_bytes(4, "little"))  # SP iniziale (placeholder, lo stub lo ricarica)
    page.puts(0x4, (_MBR_TRAMPOLINE_STUB_OFFSET | 1).to_bytes(4, "little"))  # reset -> stub (thumb)
    page.puts(_MBR_TRAMPOLINE_STUB_OFFSET, _MBR_TRAMPOLINE_STUB)
    return page


class TargetChip(str, Enum):
    NRF52832 = "nrf52832"
    NRF52840 = "nrf52840"

    @property
    def label(self) -> str:
        return {
            TargetChip.NRF52832: "nRF52832",
            TargetChip.NRF52840: "nRF52840",
        }[self]


@dataclass
class ProbeInfo:
    unique_id: str
    description: str
    vendor: str
    product: str

    @property
    def display_name(self) -> str:
        return f"{self.description} ({self.unique_id[:16]}…)"


class NrfFlasher:
    """Gestisce rilevamento probe ST-Link e flash di file Intel HEX."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def list_probes(self) -> list[ProbeInfo]:
        from pyocd.core.helpers import ConnectHelper

        probes = ConnectHelper.get_all_connected_probes(blocking=False)
        result: list[ProbeInfo] = []
        for probe in probes:
            desc = probe.description or "Probe sconosciuto"
            uid = probe.unique_id or "n/a"
            vendor = probe.vendor_name or "?"
            product = probe.product_name or "?"
            result.append(
                ProbeInfo(
                    unique_id=uid,
                    description=desc,
                    vendor=vendor,
                    product=product,
                )
            )
        return result

    def flash_async(
        self,
        *,
        hex_path: Path | None,
        target: TargetChip,
        probe_uid: str | None,
        erase_all: bool,
        reset_after: bool,
        on_log: LogCallback,
        on_done: DoneCallback,
        add_mbr_trampoline: bool = True,
    ) -> None:
        if self.busy:
            on_done(False, "Operazione già in corso.")
            return

        if hex_path is not None:
            if not hex_path.is_file():
                on_done(False, f"File non trovato: {hex_path}")
                return

            if hex_path.suffix.lower() not in {".hex", ".ihex"}:
                on_done(False, "Seleziona un file Intel HEX (.hex).")
                return

        with self._lock:
            self._busy = True

        thread = threading.Thread(
            target=self._flash_worker,
            args=(hex_path, target, probe_uid, erase_all, reset_after, on_log, on_done, add_mbr_trampoline),
            daemon=True,
        )
        thread.start()

    def _flash_worker(
        self,
        hex_path: Path | None,
        target: TargetChip,
        probe_uid: str | None,
        erase_all: bool,
        reset_after: bool,
        on_log: LogCallback,
        on_done: DoneCallback,
        add_mbr_trampoline: bool,
    ) -> None:
        try:
            self._run_flash(hex_path, target, probe_uid, erase_all, reset_after, on_log, add_mbr_trampoline)
            msg = "Programmazione completata con successo." if hex_path else "Cancellazione completata con successo."
            on_done(True, msg)
        except Exception as exc:  # noqa: BLE001 — mostrato all'utente nel log
            on_log(f"ERRORE: {exc}")
            on_done(False, str(exc))
        finally:
            with self._lock:
                self._busy = False

    def _run_flash(
        self,
        hex_path: Path | None,
        target: TargetChip,
        probe_uid: str | None,
        erase_all: bool,
        reset_after: bool,
        on_log: LogCallback,
        add_mbr_trampoline: bool = True,
    ) -> None:
        import logging

        from pyocd.core.helpers import ConnectHelper
        from pyocd.flash.file_programmer import FileProgrammer
        from pyocd.flash.eraser import FlashEraser

        class _UiLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                on_log(self.format(record))

        root_logger = logging.getLogger("pyocd")
        handler = _UiLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        try:
            on_log(f"Target: {target.label}")
            if hex_path:
                on_log(f"File: {hex_path}")

            session_kwargs: dict = {
                "target_override": target.value,
                # Sblocca automaticamente un chip con APPROTECT attivo eseguendo
                # un mass erase via CTRL-AP alla connessione. Sulle revisioni con
                # APPROTECT "hardware" pyOCD scrive anche UICR.APPROTECT = 0x5A
                # così il chip resta accessibile dopo il power-cycle.
                "auto_unlock": True,
            }
            if probe_uid:
                session_kwargs["unique_id"] = probe_uid

            on_log("Connessione al probe ST-Link…")
            session = ConnectHelper.session_with_chosen_probe(
                blocking=False,
                return_first=probe_uid is None,
                **session_kwargs,
            )
            if session is None:
                raise RuntimeError(
                    "Nessun probe ST-Link trovato. Verifica cavo USB e driver."
                )

            with session:
                board = session.board
                on_log(f"Connesso: {board.target.part_number}")

                core_target = session.board.target
                try:
                    if core_target.is_locked():
                        on_log("APPROTECT attivo: il chip verrà sbloccato via mass erase.")
                except Exception:  # noqa: BLE001 — non tutti i target espongono is_locked()
                    pass

                if erase_all:
                    # Mode.MASS usa l'ERASEALL via CTRL-AP: cancella l'intera flash
                    # (incluso UICR) e sblocca un chip protetto. Mode.CHIP, invece,
                    # cancella regione per regione tramite NVMC e fallisce su un
                    # chip con APPROTECT attivo.
                    on_log("Cancellazione completa (mass erase via CTRL-AP)…")
                    FlashEraser(session, FlashEraser.Mode.MASS).erase()

                if hex_path:
                    program_path = hex_path
                    temp_hex: Path | None = None

                    if add_mbr_trampoline:
                        from intelhex import IntelHex

                        app_hex = IntelHex(str(hex_path))
                        trampoline = build_mbr_trampoline(app_hex)
                        if trampoline is not None:
                            import tempfile

                            on_log(
                                "Layout MBR rilevato (vector table a 0x1000, 0x0 vuoto): "
                                "aggiungo il trampolino MBR a 0x0."
                            )
                            app_hex.merge(trampoline, overlap="error")
                            fd, tmp = tempfile.mkstemp(suffix=".hex", prefix="nrf_mbr_")
                            os.close(fd)
                            temp_hex = Path(tmp)
                            app_hex.write_hex_file(str(temp_hex))
                            program_path = temp_hex

                    on_log("Scrittura firmware…")
                    try:
                        FileProgrammer(session).program(str(program_path))
                    finally:
                        if temp_hex is not None:
                            temp_hex.unlink(missing_ok=True)

                if reset_after:
                    session.board.target.reset()
                    on_log("Reset del target eseguito.")
                else:
                    on_log("Operazione completata (senza reset).")
        finally:
            root_logger.removeHandler(handler)
