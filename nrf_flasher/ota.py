"""Modulo per la gestione OTA via BLE (Nordic Legacy DFU per Adafruit Bootloader)."""

import asyncio
import json
import struct
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
DFU_CP_UUID = "00001531-1212-efde-1523-785feabcd123"
DFU_PKT_UUID = "00001532-1212-efde-1523-785feabcd123"

# Opcodes for Legacy DFU
OP_START_DFU = 0x01
OP_INIT_DFU = 0x02
OP_RECEIVE_FW = 0x03
OP_VALIDATE = 0x04
OP_ACTIVATE_RESET = 0x05
OP_PKT_RCPT_NOTIF_REQ = 0x08
OP_RESPONSE = 0x10
OP_PKT_RCPT_NOTIF = 0x11

# Firmware types
FW_TYPE_APPLICATION = 0x04

PRN_COUNT = 10  # Packet Receipt Notification count


@dataclass
class BleDeviceInfo:
    name: str
    address: str
    rssi: int
    is_dfu: bool


async def scan_ble_devices(timeout_s: float = 3.0) -> list[BleDeviceInfo]:
    """Esegue una scansione BLE alla ricerca di dispositivi."""
    devices: dict[str, BleDeviceInfo] = {}

    def detection_callback(device: BLEDevice, adv_data: AdvertisementData) -> None:
        name = adv_data.local_name or device.name or "Sconosciuto"
        is_dfu = (
            "DfuTarg" in name or 
            "Adafruit" in name or 
            DFU_SERVICE_UUID.lower() in [u.lower() for u in adv_data.service_uuids]
        )
        devices[device.address] = BleDeviceInfo(
            name=name, address=device.address, rssi=adv_data.rssi, is_dfu=is_dfu
        )

    scanner = BleakScanner(detection_callback)
    await scanner.start()
    await asyncio.sleep(timeout_s)
    await scanner.stop()

    return sorted(devices.values(), key=lambda d: (-d.rssi, d.name))


class OtaFlasher:
    def __init__(self) -> None:
        self.busy = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

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
        self._run_coroutine(
            self._do_flash(zip_path, mac_address, on_log, on_progress, on_done)
        )

    async def _do_flash(
        self,
        zip_path: Path,
        mac_address: str,
        on_log: Callable[[str], None],
        on_progress: Callable[[float], None],
        on_done: Callable[[bool, str], None],
    ) -> None:
        try:
            on_log(f"Estrazione del pacchetto OTA da: {zip_path.name}")
            manifest_dict, dat_bytes, bin_bytes = self._extract_zip(zip_path)
            
            # Attualmente supportiamo solo l'Application
            if "application" not in manifest_dict.get("manifest", {}):
                raise ValueError("Il pacchetto ZIP non contiene un'application valida.")

            app_size = len(bin_bytes)
            on_log(f"Firmware trovato: {app_size} bytes. Ricerca dispositivo (bypass cache Windows)...")

            # 1. Trova il BLEDevice per bypassare la cache di Windows
            device = await BleakScanner.find_device_by_address(mac_address, timeout=5.0)
            if not device:
                on_log("Dispositivo non trovato nello scanner, tentativo fallback MAC diretto...")
                device = mac_address  # Fallback alla stringa
            
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    on_log(f"Tentativo connessione {attempt}/{max_retries}...")
                    # Utilizza il client BLE
                    async with BleakClient(device, timeout=10.0) as client:
                        on_log(f"Connesso al dispositivo {mac_address}!")

                        service = client.services.get_service(DFU_SERVICE_UUID)
                        if service is None:
                            raise RuntimeError(
                                "Il dispositivo non espone il servizio DFU legacy "
                                "(0x1530). Se è un modulo Zephyr usa il tab \"OTA Zephyr\"."
                            )
                        cp_char = service.get_characteristic(DFU_CP_UUID)
                        pkt_char = service.get_characteristic(DFU_PKT_UUID)

                        response_queue: asyncio.Queue[bytes] = asyncio.Queue()

                        def notification_handler(sender: int, data: bytearray) -> None:
                            response_queue.put_nowait(bytes(data))

                        await client.start_notify(cp_char, notification_handler)

                        async def wait_for_response(expected_opcode: int) -> None:
                            try:
                                resp = await asyncio.wait_for(response_queue.get(), timeout=10.0)
                            except asyncio.TimeoutError:
                                raise TimeoutError(f"Timeout attesa risposta per opcode 0x{expected_opcode:02X}")

                            if resp[0] == OP_RESPONSE and resp[1] == expected_opcode:
                                if resp[2] != 0x01:
                                    raise Exception(f"Errore dal dispositivo per 0x{expected_opcode:02X}, status: 0x{resp[2]:02X}")
                            elif resp[0] == OP_PKT_RCPT_NOTIF:
                                pass # Gestito altrove
                            else:
                                raise Exception(f"Risposta imprevista: {resp.hex()}")

                        on_log("Avvio DFU...")
                        await client.write_gatt_char(cp_char, bytes([OP_START_DFU, FW_TYPE_APPLICATION]), response=False)

                        # Invia dimensione: SoftDevice(0), Bootloader(0), Application(app_size)
                        size_data = struct.pack("<III", 0, 0, app_size)
                        await client.write_gatt_char(pkt_char, size_data, response=False)
                        await wait_for_response(OP_START_DFU)

                        on_log("Inizializzazione DFU...")
                        await client.write_gatt_char(cp_char, bytes([OP_INIT_DFU, 0x00]), response=False)
                        await client.write_gatt_char(pkt_char, dat_bytes, response=False)
                        await client.write_gatt_char(cp_char, bytes([OP_INIT_DFU, 0x01]), response=False)
                        await wait_for_response(OP_INIT_DFU)

                        on_log("Impostazione PRN (Packet Receipt Notification)...")
                        await client.write_gatt_char(cp_char, bytes([OP_PKT_RCPT_NOTIF_REQ, PRN_COUNT, 0x00]), response=False)

                        on_log("Trasmissione Firmware...")
                        await client.write_gatt_char(cp_char, bytes([OP_RECEIVE_FW]), response=False)

                        chunk_size = 20
                        for i in range(0, app_size, chunk_size):
                            chunk = bin_bytes[i:i + chunk_size]
                            await client.write_gatt_char(pkt_char, chunk, response=False)

                            # Controlla PRN
                            packet_idx = (i // chunk_size) + 1
                            if packet_idx % PRN_COUNT == 0:
                                try:
                                    resp = await asyncio.wait_for(response_queue.get(), timeout=5.0)
                                    if resp[0] != OP_PKT_RCPT_NOTIF:
                                        raise Exception(f"Atteso PRN, ricevuto: {resp.hex()}")
                                except asyncio.TimeoutError:
                                    raise TimeoutError("Timeout attesa PRN")

                            # Calcola il progresso esatto
                            progress = min((i + chunk_size) / app_size, 1.0)
                            on_progress(progress)

                        await wait_for_response(OP_RECEIVE_FW)
                        on_log("Trasmissione completata. Validazione...")
                        on_progress(1.0)

                        await client.write_gatt_char(cp_char, bytes([OP_VALIDATE]), response=False)
                        await wait_for_response(OP_VALIDATE)

                        on_log("Validazione OK. Riavvio dispositivo...")
                        await client.write_gatt_char(cp_char, bytes([OP_ACTIVATE_RESET]), response=False)

                        on_done(True, "Aggiornamento OTA completato con successo!")
                        return  # Esce dalla funzione e dal ciclo di retry con successo

                except Exception as e:
                    on_log(f"Errore DFU al tentativo {attempt}: {e}")
                    if attempt < max_retries:
                        on_log("Attendo 2 secondi prima di riprovare...")
                        await asyncio.sleep(2.0)
                    else:
                        raise RuntimeError(f"Fallito dopo {max_retries} tentativi. Ultimo errore: {e}")

        except Exception as e:
            on_log(f"Errore OTA: {e}")
            on_done(False, f"Errore: {e}")
        finally:
            self.busy = False

    def _extract_zip(self, zip_path: Path) -> tuple[dict, bytes, bytes]:
        with zipfile.ZipFile(zip_path, 'r') as z:
            manifest_bytes = z.read("manifest.json")
            manifest = json.loads(manifest_bytes.decode('utf-8'))
            
            # Un pacchetto sysbuild Zephyr ha "files": [...] al livello alto:
            # se lo riconosciamo qui, l'utente ha sbagliato tab.
            if "files" in manifest and "manifest" not in manifest:
                raise ValueError(
                    "Questo è un pacchetto Zephyr (sysbuild), non un DFU legacy "
                    "Nordic/Adafruit. Usa il tab \"OTA Zephyr\"."
                )

            app_info = manifest.get("manifest", {}).get("application", {})
            if not app_info:
                raise ValueError("Nessuna application definita nel manifest.json.")
                
            dat_filename = app_info.get("dat_file")
            bin_filename = app_info.get("bin_file")
            
            if not dat_filename or not bin_filename:
                raise ValueError("Manca il riferimento al file .dat o .bin nel manifest.json.")
                
            dat_bytes = z.read(dat_filename)
            bin_bytes = z.read(bin_filename)
            
            return manifest, dat_bytes, bin_bytes
