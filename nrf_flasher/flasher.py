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

# La regione flash del codice finisce sotto 0x10000000: UICR (0x10001000) e
# FICR non contano per l'analisi del layout dell'applicazione.
_FLASH_REGION_END = 0x10000000

# Info struct del SoftDevice: MBR_SIZE (0x1000) + SOFTDEVICE_INFO_STRUCT_OFFSET
# (0x2000) = 0x3000. Layout (nrf_sdm.h):
#   0x3000  u8   info_size
#   0x3004  u32  magic_number (0x51B1E5DB)
#   0x3008  u32  softdevice_size (fine regione flash del SD = APP_CODE_BASE)
#   0x300C  u16  firmware_id
#   0x3010  u32  softdevice_id (es. 132 per S132) — solo se info_size >= 0x18
#   0x3014  u32  softdevice_version (MMMmmmppp)   — solo se info_size >= 0x18
_SD_INFO_STRUCT_ADDR = 0x3000
_SD_MAGIC = 0x51B1E5DB

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

# ---------------------------------------------------------------------------
# Bootloader Settings Page (SDK 11 legacy, usato dal bootloader Adafruit).
# Layout della struct bootloader_settings_t (packed, 12 byte significativi):
#   offset 0x00  u32  bank_0       — BANK_VALID_APP = 0x01 se l'app è valida
#   offset 0x04  u32  bank_0_crc   — CRC-16/CCITT dell'immagine app (in u32)
#   offset 0x08  u32  bank_0_size  — dimensione dell'app in byte
# Il resto della pagina da 4 KB è 0xFF (flash erased).
# ---------------------------------------------------------------------------
_BL_SETTINGS_ADDR = {
    "nrf52832": 0x7F000,   # ultima pagina su 512 KB flash
    "nrf52840": 0xFF000,   # ultima pagina su 1 MB flash
}
_BANK_VALID_APP = 0x01


def _read_u32(ihex: "IntelHex", addr: int) -> int:
    return int.from_bytes(bytes(ihex.tobinarray(start=addr, size=4)), "little")


@dataclass
class SoftDeviceInfo:
    """Dati letti dall'info struct del SoftDevice a 0x3000."""

    size: int  # fine della regione flash del SD (= APP_CODE_BASE dell'app)
    fwid: int
    sd_id: int  # es. 132 per S132, 140 per S140; 0 se non disponibile
    version: str  # "7.2.0", oppure "" se non disponibile

    @property
    def name(self) -> str:
        return f"S{self.sd_id}" if self.sd_id else f"FWID 0x{self.fwid:04X}"

    @property
    def label(self) -> str:
        ver = f" v{self.version}" if self.version else ""
        return f"{self.name}{ver} (occupa 0x0–0x{self.size:X})"


def read_softdevice_info(ihex: "IntelHex") -> SoftDeviceInfo | None:
    """Legge l'info struct del SoftDevice; ``None`` se l'HEX non ne contiene uno."""
    try:
        magic = _read_u32(ihex, _SD_INFO_STRUCT_ADDR + 0x4)
    except Exception:  # noqa: BLE001
        return None
    if magic != _SD_MAGIC:
        return None

    info_size = ihex.tobinarray(start=_SD_INFO_STRUCT_ADDR, size=1)[0]
    size = _read_u32(ihex, _SD_INFO_STRUCT_ADDR + 0x8)
    fwid = _read_u32(ihex, _SD_INFO_STRUCT_ADDR + 0xC) & 0xFFFF

    sd_id = 0
    version = ""
    if info_size >= 0x18:  # i SoftDevice recenti espongono anche id e versione
        sd_id = _read_u32(ihex, _SD_INFO_STRUCT_ADDR + 0x10)
        raw_ver = _read_u32(ihex, _SD_INFO_STRUCT_ADDR + 0x14)
        version = f"{raw_ver // 1000000}.{(raw_ver // 1000) % 1000}.{raw_ver % 1000}"

    if not (0x1000 < size < _FLASH_REGION_END):
        return None
    return SoftDeviceInfo(size=size, fwid=fwid, sd_id=sd_id, version=version)


@dataclass
class HexInfo:
    """Risultato dell'analisi di un file HEX, per feedback nella UI."""

    min_addr: int  # inizio dei dati in flash (UICR escluso)
    max_addr: int
    sd_info: SoftDeviceInfo | None
    has_mbr_region: bool  # dati presenti sotto 0x1000
    description: str


def analyze_hex(path: Path) -> HexInfo:
    """Analizza un HEX e produce una descrizione leggibile del layout."""
    from intelhex import IntelHex

    ihex = IntelHex(str(path))
    flash_segs = [(s, e) for s, e in ihex.segments() if s < _FLASH_REGION_END]
    if not flash_segs:
        raise ValueError("il file non contiene dati in flash")

    min_addr = min(s for s, _ in flash_segs)
    max_addr = max(e for _, e in flash_segs)
    has_mbr_region = min_addr < APP_VECTOR_TABLE_ADDR
    sd_info = read_softdevice_info(ihex) if has_mbr_region else None

    if sd_info is not None:
        desc = f"MBR + SoftDevice {sd_info.label}"
        if max_addr > sd_info.size:
            desc += f", con applicazione inclusa a 0x{sd_info.size:X}"
    elif min_addr == 0:
        desc = "applicazione standalone (vector table a 0x0)"
    elif min_addr == APP_VECTOR_TABLE_ADDR:
        desc = "applicazione per MBR (vector table a 0x1000)"
    else:
        desc = (
            f"applicazione linkata a 0x{min_addr:X}: richiede un SoftDevice "
            f"che termini a 0x{min_addr:X} (tipico firmware Arduino/SDK con SoftDevice)"
        )
    return HexInfo(
        min_addr=min_addr,
        max_addr=max_addr,
        sd_info=sd_info,
        has_mbr_region=has_mbr_region,
        description=desc,
    )


# ---------------------------------------------------------------------------
# MCUboot (progetto Zephyr TendaVibrationZephyr).
# Layout fisso di pm_static.yml: MCUboot a 0x0, slot primario a 0xC000.
# Un'immagine firmata inizia con l'header MCUboot: magic IH_MAGIC a offset 0
# dell'immagine, cioè a 0xC000 in flash.
# ---------------------------------------------------------------------------
_MCUBOOT_SLOT0_ADDR = 0xC000
_MCUBOOT_IH_MAGIC = 0x96F3B83D


@dataclass
class McubootHexInfo:
    """Risultato dell'analisi di un HEX per il flusso MCUboot."""

    kind: str  # "merged" | "app" | "unknown"
    description: str


def analyze_mcuboot_hex(path: Path) -> McubootHexInfo:
    """Classifica un HEX del progetto Zephyr (merged vs app firmata).

    - ``merged``: dati a 0x0 (bootloader) + header immagine a 0xC000
      (``merged.hex`` di sysbuild) -> richiede programmazione chip completo.
    - ``app``: nessun dato sotto 0xC000 ma header immagine a 0xC000
      (``zephyr.signed.hex``) -> aggiornamento solo applicazione.
    - ``unknown``: tutto il resto (probabilmente non è una build sysbuild
      del progetto Zephyr con il layout di pm_static.yml).
    """
    from intelhex import IntelHex

    ihex = IntelHex(str(path))
    flash_segs = [(s, e) for s, e in ihex.segments() if s < _FLASH_REGION_END]
    if not flash_segs:
        raise ValueError("il file non contiene dati in flash")

    min_addr = min(s for s, _ in flash_segs)
    max_addr = max(e for _, e in flash_segs)
    has_bootloader = min_addr < APP_VECTOR_TABLE_ADDR

    try:
        slot0_magic = _read_u32(ihex, _MCUBOOT_SLOT0_ADDR)
    except Exception:  # noqa: BLE001 — nessun dato a 0xC000
        slot0_magic = 0
    has_image_header = slot0_magic == _MCUBOOT_IH_MAGIC

    if has_bootloader and has_image_header:
        return McubootHexInfo(
            kind="merged",
            description=(
                f"immagine completa MCUboot + app firmata (merged.hex, "
                f"0x{min_addr:X}–0x{max_addr:X}): usare 'Chip completo'"
            ),
        )
    if has_image_header and min_addr >= _MCUBOOT_SLOT0_ADDR:
        return McubootHexInfo(
            kind="app",
            description=(
                f"applicazione firmata MCUboot (slot primario 0x{min_addr:X}–"
                f"0x{max_addr:X}): ok per 'Solo applicazione'"
            ),
        )
    if has_bootloader:
        return McubootHexInfo(
            kind="unknown",
            description=(
                f"dati a 0x{min_addr:X} ma nessun header immagine MCUboot a "
                f"0x{_MCUBOOT_SLOT0_ADDR:X}: non sembra una build sysbuild del "
                "progetto Zephyr (layout pm_static.yml)"
            ),
        )
    return McubootHexInfo(
        kind="unknown",
        description=(
            f"nessun header immagine MCUboot a 0x{_MCUBOOT_SLOT0_ADDR:X} "
            f"(dati 0x{min_addr:X}–0x{max_addr:X}): verificare di aver "
            "selezionato merged.hex o zephyr.signed.hex"
        ),
    )


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


def _prepare_image(
    hex_path: Path | None,
    softdevice_path: Path | None,
    add_mbr_trampoline: bool,
    on_log: LogCallback,
) -> tuple[Path, Path | None]:
    """Prepara l'immagine da programmare (merge SoftDevice + app, trampolino).

    Ritorna ``(path_da_programmare, path_temporaneo_da_eliminare)``.
    """
    import tempfile

    from intelhex import IntelHex

    def write_temp(image: "IntelHex") -> tuple[Path, Path]:
        fd, tmp = tempfile.mkstemp(suffix=".hex", prefix="nrf_merged_")
        os.close(fd)
        temp = Path(tmp)
        image.write_hex_file(str(temp))
        return temp, temp

    if softdevice_path is None:
        assert hex_path is not None
        if not add_mbr_trampoline:
            return hex_path, None
        app_hex = IntelHex(str(hex_path))
        trampoline = build_mbr_trampoline(app_hex)
        if trampoline is None:
            return hex_path, None
        on_log(
            "Layout MBR rilevato (vector table a 0x1000, 0x0 vuoto): "
            "aggiungo il trampolino MBR a 0x0."
        )
        app_hex.merge(trampoline, overlap="error")
        return write_temp(app_hex)

    # --- SoftDevice selezionato ---
    image = IntelHex(str(softdevice_path))
    sd_info = read_softdevice_info(image)
    if sd_info is None:
        raise RuntimeError(
            f"Il file '{softdevice_path.name}' non sembra un SoftDevice Nordic: "
            "info struct non trovata a 0x3000. Verifica di aver selezionato "
            "l'HEX del SoftDevice (es. s132_nrf52_7.2.0_softdevice.hex)."
        )
    on_log(f"SoftDevice: {sd_info.label}, FWID 0x{sd_info.fwid:04X}")

    if hex_path is None:
        return write_temp(image)

    app_hex = IntelHex(str(hex_path))
    flash_segs = [(s, e) for s, e in app_hex.segments() if s < _FLASH_REGION_END]
    if not flash_segs:
        raise RuntimeError("L'HEX applicazione non contiene dati in flash.")
    app_min = min(s for s, _ in flash_segs)

    if app_min < APP_VECTOR_TABLE_ADDR:
        raise RuntimeError(
            "L'HEX applicazione contiene già dati sotto 0x1000 (probabilmente "
            "include MBR/SoftDevice): programmalo da solo, senza selezionare "
            "un SoftDevice separato."
        )
    if app_min < sd_info.size:
        raise RuntimeError(
            f"L'applicazione inizia a 0x{app_min:X} ma il SoftDevice occupa la "
            f"flash fino a 0x{sd_info.size:X}: si sovrappongono. Serve un "
            f"firmware compilato per questo SoftDevice (inizio applicazione "
            f"a 0x{sd_info.size:X})."
        )
    if app_min > sd_info.size:
        on_log(
            f"ATTENZIONE: l'applicazione inizia a 0x{app_min:X} ma il SoftDevice "
            f"termina a 0x{sd_info.size:X}. Senza un bootloader il SoftDevice "
            f"salta a 0x{sd_info.size:X} e l'applicazione potrebbe non partire. "
            "Verifica che la versione del SoftDevice corrisponda a quella usata "
            "in compilazione."
        )

    if add_mbr_trampoline:
        on_log("SoftDevice selezionato: trampolino MBR non necessario (l'MBR è incluso nel SoftDevice).")

    on_log("Unione SoftDevice + applicazione…")
    try:
        image.merge(app_hex, overlap="error")
    except Exception as exc:  # noqa: BLE001 — sovrapposizioni inattese
        raise RuntimeError(
            f"Sovrapposizione tra SoftDevice e applicazione durante l'unione: {exc}"
        ) from exc
    return write_temp(image)


class PostFlashAction(str, Enum):
    """Azione da eseguire dopo la programmazione del firmware."""

    NONE = "none"              # Non fare nulla
    ERASE_UICR = "uicr"       # Cancella UICR (bypass bootloader)
    WRITE_BL_SETTINGS = "crc"  # Genera e scrivi Bootloader Settings (mantieni OTA)

    @property
    def label(self) -> str:
        return {
            PostFlashAction.NONE: "Nessuna azione",
            PostFlashAction.ERASE_UICR: "Cancella UICR (bypass bootloader)",
            PostFlashAction.WRITE_BL_SETTINGS: "Genera CRC (mantieni bootloader/OTA)",
        }[self]


def _crc16_ccitt(data: bytes, *, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT (polinomio 0x1021) usato dal Nordic SDK 11."""
    crc = init
    for byte in data:
        crc = ((crc >> 8) & 0xFF) | ((crc << 8) & 0xFFFF)
        crc ^= byte
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc & 0xFFFF


def _compute_app_crc16(
    hex_path: Path,
    app_start: int,
    on_log: LogCallback,
) -> tuple[int, int]:
    """Calcola CRC-16 CCITT e dimensione dell'applicazione nel file HEX.

    Considera solo i segmenti flash a partire da ``app_start`` e sotto
    ``_FLASH_REGION_END``.  Gli spazi vuoti (gap) nel layout vengono riempiti
    con 0xFF (flash erased) per produrre un blob contiguo identico a quello
    che il bootloader vede in flash.

    Ritorna ``(crc16, app_size_bytes)``.
    """
    from intelhex import IntelHex

    ihex = IntelHex(str(hex_path))
    # Prendi solo i segmenti dell'applicazione (non UICR, non SoftDevice/MBR)
    app_segs = [
        (s, e) for s, e in ihex.segments()
        if s >= app_start and e <= _FLASH_REGION_END
    ]
    if not app_segs:
        raise RuntimeError(
            f"Nessun segmento applicazione trovato a partire da 0x{app_start:X}."
        )

    seg_min = min(s for s, _ in app_segs)
    seg_max = max(e for _, e in app_segs)
    app_size = seg_max - seg_min

    # Estrai il blob contiguo: ihex.tobinarray riempie i gap con padding (default 0xFF)
    app_blob = bytes(ihex.tobinarray(start=seg_min, size=app_size))
    crc = _crc16_ccitt(app_blob)

    on_log(
        f"CRC-16 applicazione: 0x{crc:04X}, dimensione: {app_size} byte "
        f"(0x{seg_min:X}–0x{seg_max:X})"
    )
    return crc, app_size


def _write_bootloader_settings(
    session: object,
    target: "TargetChip",
    app_crc: int,
    app_size: int,
    on_log: LogCallback,
) -> None:
    """Scrive la Bootloader Settings Page con bank_0 valida.

    Cancella la pagina di settings via NVMC e poi scrive i 3 campi della
    struct ``bootloader_settings_t`` (SDK 11 legacy).
    """
    import struct
    import time

    settings_addr = _BL_SETTINGS_ADDR.get(target.value)
    if settings_addr is None:
        on_log(f"Indirizzo Bootloader Settings sconosciuto per {target.label}, skip.")
        return

    core = session.board.target  # type: ignore[attr-defined]
    nvmc_config = 0x4001E504
    nvmc_ready = 0x4001E400
    nvmc_erasepage = 0x4001E508

    def wait_nvmc() -> None:
        for _ in range(200):
            if core.read_memory(nvmc_ready) & 1:
                return
            time.sleep(0.005)
        raise RuntimeError("NVMC non pronto dopo timeout.")

    on_log(f"Scrittura Bootloader Settings a 0x{settings_addr:X}…")

    # 1. Cancella la pagina di settings
    core.write_memory(nvmc_config, 2)  # NVMC CONFIG = ERASEEN
    core.write_memory(nvmc_erasepage, settings_addr)
    wait_nvmc()
    core.write_memory(nvmc_config, 0)  # READONLY

    # 2. Scrivi la struct bootloader_settings_t
    #    bank_0 (u32) | bank_0_crc (u32) | bank_0_size (u32)
    settings_data = struct.pack("<III", _BANK_VALID_APP, app_crc, app_size)

    core.write_memory(nvmc_config, 1)  # NVMC CONFIG = WRITEEN
    for offset in range(0, len(settings_data), 4):
        word = int.from_bytes(settings_data[offset:offset + 4], "little")
        core.write_memory(settings_addr + offset, word)
        wait_nvmc()
    core.write_memory(nvmc_config, 0)  # READONLY

    on_log(
        f"Bootloader Settings scritte: bank_0=VALID_APP, "
        f"CRC=0x{app_crc:04X}, size={app_size}"
    )


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
        softdevice_path: Path | None = None,
        post_flash_action: PostFlashAction = PostFlashAction.ERASE_UICR,
    ) -> None:
        if self.busy:
            on_done(False, "Operazione già in corso.")
            return

        for label, path in (("HEX", hex_path), ("SoftDevice", softdevice_path)):
            if path is None:
                continue
            if not path.is_file():
                on_done(False, f"File {label} non trovato: {path}")
                return
            if path.suffix.lower() not in {".hex", ".ihex"}:
                on_done(False, f"Il file {label} deve essere un Intel HEX (.hex).")
                return

        with self._lock:
            self._busy = True

        thread = threading.Thread(
            target=self._flash_worker,
            args=(
                hex_path,
                target,
                probe_uid,
                erase_all,
                reset_after,
                on_log,
                on_done,
                add_mbr_trampoline,
                softdevice_path,
                post_flash_action,
            ),
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
        softdevice_path: Path | None,
        post_flash_action: PostFlashAction,
    ) -> None:
        try:
            self._run_flash(
                hex_path,
                target,
                probe_uid,
                erase_all,
                reset_after,
                on_log,
                add_mbr_trampoline,
                softdevice_path,
                post_flash_action,
            )
            if hex_path or softdevice_path:
                msg = "Programmazione completata con successo."
            else:
                msg = "Cancellazione completata con successo."
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
        softdevice_path: Path | None = None,
        post_flash_action: PostFlashAction = PostFlashAction.ERASE_UICR,
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
            if softdevice_path:
                on_log(f"SoftDevice: {softdevice_path}")
            if hex_path:
                on_log(f"File: {hex_path}")

            # Prepara l'immagine PRIMA di toccare il chip: se i file non sono
            # coerenti (sovrapposizioni, SoftDevice non valido) si esce senza
            # aver cancellato nulla.
            program_path: Path | None = None
            temp_hex: Path | None = None
            if hex_path or softdevice_path:
                program_path, temp_hex = _prepare_image(
                    hex_path, softdevice_path, add_mbr_trampoline, on_log
                )

            if softdevice_path and not erase_all:
                on_log(
                    "Suggerimento: quando si carica o si cambia SoftDevice è "
                    "consigliata la cancellazione completa della flash."
                )

            session_kwargs: dict = {
                "target_override": target.value,
                # Sblocca automaticamente un chip con APPROTECT attivo eseguendo
                # un mass erase via CTRL-AP alla connessione. Sulle revisioni con
                # APPROTECT "hardware" pyOCD scrive anche UICR.APPROTECT = 0x5A
                # così il chip resta accessibile dopo il power-cycle.
                "auto_unlock": True,
                "connect_mode": "halt",      # Arresta immediatamente il core alla connessione per evitare interferenze
                "frequency": 1000000,        # Abbassa la frequenza SWD a 1 MHz per maggiore stabilità dei segnali
            }
            if probe_uid:
                session_kwargs["unique_id"] = probe_uid

            try:
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
                        on_log("Forzo un riavvio a freddo (reset_and_halt) per spegnere il SoftDevice...")
                        core_target.reset_and_halt()
                    except Exception as e:
                        on_log(f"Nota su reset_and_halt: {e}")

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

                    if program_path:
                        on_log("Scrittura firmware…")
                        FileProgrammer(session).program(str(program_path))
                    on_log("Scrittura completata con successo.")

                    # --- AZIONE POST-PROGRAMMAZIONE ---
                    if post_flash_action == PostFlashAction.ERASE_UICR:
                        # Cancella UICR per bypassare il bootloader Adafruit:
                        # l'MBR non troverà l'indirizzo del bootloader e salterà
                        # direttamente all'app a 0x26000.
                        try:
                            on_log("Aggiramento Bootloader in corso (cancellazione UICR)...")
                            session.board.target.write_memory(0x4001E504, 2)  # NVMC CONFIG = ERASEEN
                            session.board.target.write_memory(0x4001E514, 1)  # ERASEUICR
                            while session.board.target.read_memory(0x4001E400) == 0:
                                pass
                            session.board.target.write_memory(0x4001E504, 0)  # READONLY
                            on_log("Bootloader bypassato con successo! L'app partirà diretta.")
                        except Exception as e:
                            on_log(f"Errore durante bypass bootloader: {e}")

                    elif post_flash_action == PostFlashAction.WRITE_BL_SETTINGS:
                        # Scrivi la Bootloader Settings Page con CRC valido
                        # così il bootloader Adafruit valida l'app e la avvia.
                        if hex_path is None:
                            on_log("Nessun firmware HEX: impossibile calcolare CRC, skip.")
                        else:
                            try:
                                # Determina l'indirizzo di inizio dell'app
                                from intelhex import IntelHex as _IH
                                _app_ihex = _IH(str(hex_path))
                                _app_segs = [
                                    (s, e) for s, e in _app_ihex.segments()
                                    if s >= APP_VECTOR_TABLE_ADDR and e <= _FLASH_REGION_END
                                ]
                                if _app_segs:
                                    app_start = min(s for s, _ in _app_segs)
                                else:
                                    app_start = APP_VECTOR_TABLE_ADDR

                                app_crc, app_size = _compute_app_crc16(
                                    hex_path, app_start, on_log
                                )
                                _write_bootloader_settings(
                                    session, target, app_crc, app_size, on_log
                                )
                            except Exception as e:
                                on_log(f"Errore durante scrittura Bootloader Settings: {e}")

                    else:
                        on_log("Nessuna azione post-programmazione selezionata.")

                    if reset_after:
                        session.board.target.reset()
                        on_log("Reset del target eseguito.")
                    else:
                        on_log("Operazione completata (senza reset).")
            finally:
                if temp_hex is not None:
                    temp_hex.unlink(missing_ok=True)
        finally:
            root_logger.removeHandler(handler)
