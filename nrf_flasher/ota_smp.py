"""OTA via BLE con protocollo SMP / MCUmgr (Zephyr + MCUboot).

Controparte di :mod:`nrf_flasher.ota`, che implementa invece il DFU legacy
Nordic usato dal bootloader Adafruit. I due protocolli non sono compatibili:
il pacchetto .zip di Zephyr (sysbuild) contiene un manifest diverso e viaggia
sul servizio SMP, non sul servizio DFU 0x1530.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import threading
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cbor2
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

SMP_SERVICE_UUID = "8D53DC1D-1DBA-4EA3-88CE-D146C524AC28"
SMP_CHAR_UUID = "DA2E7828-FBCE-4E01-AE9E-261174997C48"

OP_WRITE_REQ = 2
GROUP_OS = 0
GROUP_IMG = 1

CMD_OS_RESET = 5
CMD_IMG_STATE = 0
CMD_IMG_UPLOAD = 1

SMP_HEADER_LEN = 8
CHUNK_SIZE = 128  # dimensione prudente per un MTU BLE non negoziato


@dataclass
class SmpDeviceInfo:
    name: str
    address: str
    rssi: int
    is_dfu: bool


async def scan_smp_devices(timeout_s: float = 3.0) -> list[SmpDeviceInfo]:
    """Scansione BLE alla ricerca di dispositivi Zephyr che espongono SMP."""
    devices: dict[str, SmpDeviceInfo] = {}

    def detection_callback(device: BLEDevice, adv_data: AdvertisementData) -> None:
        name = adv_data.local_name or device.name or "Sconosciuto"
        is_dfu = (
            "Zephyr" in name
            or "Mailbox" in name
            or SMP_SERVICE_UUID.lower() in [u.lower() for u in adv_data.service_uuids]
        )
        devices[device.address] = SmpDeviceInfo(
            name=name, address=device.address, rssi=adv_data.rssi, is_dfu=is_dfu
        )

    scanner = BleakScanner(detection_callback)
    await scanner.start()
    await asyncio.sleep(timeout_s)
    await scanner.stop()

    return sorted(devices.values(), key=lambda d: (-d.rssi, d.name))


class SmpOtaFlasher:
    def __init__(self) -> None:
        self.busy = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()
        self._seq = 0

    def _start_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coroutine(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def flash_async(
        self,
        zip_path: Path,
        mac_address: str,
        on_log: Callable[[str], None],
        on_progress: Callable[[float], None],
        on_done: Callable[[bool, str], None],
    ) -> None:
        if self.busy:
            on_done(False, "Operazione già in corso.")
            return
        self.busy = True
        self._seq = 0
        self._run_coroutine(
            self._do_flash(zip_path, mac_address, on_log, on_progress, on_done)
        )

    async def _send_smp_req(
        self,
        client: BleakClient,
        response_queue: "asyncio.Queue[bytes]",
        group: int,
        cmd: int,
        payload: dict,
    ) -> dict:
        cbor_payload = cbor2.dumps(payload)
        header = struct.pack(
            ">BBHHBB", OP_WRITE_REQ, 0, len(cbor_payload), group, self._seq, cmd
        )
        packet = header + cbor_payload

        # Scarta risposte arretrate di richieste andate in timeout
        while not response_queue.empty():
            response_queue.get_nowait()

        await client.write_gatt_char(SMP_CHAR_UUID, packet, response=False)

        try:
            # Reassembly: una risposta SMP può arrivare su più notifiche
            resp_buf = bytearray()
            target_len = -1

            while True:
                resp = await asyncio.wait_for(response_queue.get(), timeout=10.0)
                resp_buf.extend(resp)

                if target_len == -1 and len(resp_buf) >= SMP_HEADER_LEN:
                    target_len = struct.unpack(">H", resp_buf[2:4])[0] + SMP_HEADER_LEN

                if target_len != -1 and len(resp_buf) >= target_len:
                    break

            cbor_resp = cbor2.loads(resp_buf[SMP_HEADER_LEN:target_len])
            self._seq = (self._seq + 1) % 256
            return cbor_resp
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timeout risposta SMP per gruppo {group} cmd {cmd}")

    async def _do_flash(
        self,
        zip_path: Path,
        mac_address: str,
        on_log: Callable[[str], None],
        on_progress: Callable[[float], None],
        on_done: Callable[[bool, str], None],
    ) -> None:
        try:
            on_log(f"Estrazione firmware Zephyr da: {zip_path.name}")
            bin_bytes = self._extract_bin(zip_path)
            app_size = len(bin_bytes)
            on_log(f"Firmware estratto: {app_size} bytes. Ricerca dispositivo…")

            # Passare l'oggetto BLEDevice invece del MAC aggira la cache di Windows
            device = await BleakScanner.find_device_by_address(mac_address, timeout=5.0)
            if not device:
                on_log("Dispositivo non trovato nello scanner, fallback su MAC diretto…")
                device = mac_address

            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    on_log(f"Tentativo connessione {attempt}/{max_retries}…")
                    async with BleakClient(device, timeout=10.0) as client:
                        on_log("Connesso! Sottoscrizione notifiche SMP…")
                        await asyncio.sleep(1.0)

                        response_queue: asyncio.Queue[bytes] = asyncio.Queue()

                        def notification_handler(sender: int, data: bytearray) -> None:
                            response_queue.put_nowait(bytes(data))

                        await client.start_notify(SMP_CHAR_UUID, notification_handler)

                        on_log("Inizio upload SMP…")

                        offset = 0
                        while offset < app_size:
                            chunk = bin_bytes[offset:offset + CHUNK_SIZE]
                            req_data: dict = {"data": chunk, "off": offset}
                            if offset == 0:
                                req_data["len"] = app_size

                            resp = await self._send_smp_req(
                                client, response_queue, GROUP_IMG, CMD_IMG_UPLOAD, req_data
                            )

                            if resp.get("rc", 0) != 0:
                                raise RuntimeError(f"Errore SMP rc={resp['rc']}")

                            offset = resp.get("off", offset + len(chunk))
                            on_progress(min(offset / app_size, 1.0))

                        on_progress(1.0)
                        on_log("Upload completato. Marco l'immagine come pending (test)…")

                        fw_hash = hashlib.sha256(bin_bytes).digest()
                        resp = await self._send_smp_req(
                            client,
                            response_queue,
                            GROUP_IMG,
                            CMD_IMG_STATE,
                            {"hash": fw_hash, "confirm": False},
                        )

                        if resp.get("rc", 0) != 0:
                            raise RuntimeError(f"Errore Image State rc={resp['rc']}")

                        on_log("Immagine validata. Riavvio dispositivo…")

                        # Il reset chiude la connessione: spesso non arriva risposta
                        try:
                            await self._send_smp_req(
                                client, response_queue, GROUP_OS, CMD_OS_RESET, {}
                            )
                        except Exception:
                            pass

                        on_done(
                            True,
                            "Aggiornamento SMP completato! Il dispositivo si sta riavviando.",
                        )
                        return

                except Exception as e:  # noqa: BLE001 — riprova su qualunque errore BLE
                    on_log(f"Errore al tentativo {attempt}: {e}")
                    if attempt < max_retries:
                        on_log("Attendo 2 secondi…")
                        await asyncio.sleep(2.0)
                    else:
                        raise RuntimeError(
                            f"Fallito dopo {max_retries} tentativi. Ultimo errore: {e}"
                        )

        except Exception as e:  # noqa: BLE001 — l'esito va comunque riportato alla UI
            on_log(f"Errore fatale: {e}\n{traceback.format_exc()}")
            on_done(False, f"Errore: {e}")
        finally:
            self.busy = False

    def _extract_bin(self, zip_path: Path) -> bytes:
        with zipfile.ZipFile(zip_path, "r") as z:
            try:
                manifest_bytes = z.read("manifest.json")
            except KeyError:
                raise ValueError(
                    "Il file ZIP non contiene manifest.json (non è un pacchetto Zephyr?)."
                )

            manifest = json.loads(manifest_bytes.decode("utf-8"))

            # Un pacchetto Adafruit ha manifest.manifest.application: se lo
            # riconosciamo qui, l'utente ha sbagliato tab.
            if "manifest" in manifest and "application" in manifest.get("manifest", {}):
                raise ValueError(
                    "Questo è un pacchetto DFU legacy Nordic/Adafruit, non Zephyr. "
                    "Usa il tab \"OTA Adafruit\"."
                )

            # Formato MCUboot sysbuild: "files": [{"file": "app_update.bin", ...}]
            files = manifest.get("files", [])
            if not files:
                raise ValueError("Nessun array 'files' nel manifest di Zephyr.")

            bin_filename = files[0].get("file")
            if not bin_filename:
                raise ValueError("Nome file non trovato nel manifest.")

            return z.read(bin_filename)
