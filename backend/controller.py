"""
BleController — BLE communication with the ESP32 stepper motor controller.

Architecture (mirrors MotorControllerEsp.py BleWorker pattern):
  All bleak I/O runs on a *dedicated* asyncio event loop in a background
  daemon thread.  This sidesteps the COM-apartment conflict that occurs when
  bleak's WinRT callbacks share the uvicorn/FastAPI event loop on Windows.

  FastAPI-side async methods dispatch work to the BLE loop via
  run_coroutine_threadsafe + asyncio.wrap_future, then await the result.
  Callbacks from the BLE loop reach the FastAPI loop the same way.

BLE UUIDs (from ESP32 firmware ble_stepper_server.cpp):
  Service : AA000001-1234-1234-1234-1234567890AA
  Cmd     : AA000002-1234-1234-1234-1234567890AA  (client writes commands)
  Status  : AA000003-1234-1234-1234-1234567890AA  (server notifies: OK / COMPLETE / STOPPED / WAIT / INVALID)
"""

import asyncio
import threading
from datetime import datetime
from typing import Optional, Callable

from bleak import BleakScanner, BleakClient

BLE_DEVICE_NAME   = "ESP32_STEPPER"
BLE_CMD_CHAR_UUID = "AA000002-1234-1234-1234-1234567890AA"
BLE_STS_CHAR_UUID = "AA000003-1234-1234-1234-1234567890AA"
COMMAND_TIMEOUT   = 60   # seconds to wait for COMPLETE per motor command


class BleController:
    """Manages the BLE connection and sends motor commands to the ESP32."""

    def __init__(self, broadcast_callback: Optional[Callable] = None):
        self.broadcast_callback = broadcast_callback
        self.connected = False
        self.cancel_requested = False
        self.motor_is_down = False

        # Hook invoked from _on_ble_disconnect (BLE loop thread) so the
        # experiment controller can pause the running experiment.
        # The handler is responsible for hopping back onto the FastAPI loop.
        self.on_unexpected_disconnect: Optional[Callable] = None

        self._client: Optional[BleakClient] = None
        self._pending_future: Optional[asyncio.Future] = None
        self._accept_ok_response = False

        # FastAPI event loop — captured the first time connect() is called
        self._fastapi_loop: Optional[asyncio.AbstractEventLoop] = None

        # Dedicated BLE event loop running in a background daemon thread
        # (same pattern as BleWorker in MotorControllerEsp.py)
        self._ble_loop = asyncio.new_event_loop()
        self._ble_thread = threading.Thread(target=self._run_ble_loop, daemon=True)
        self._ble_thread.start()

    # ── BLE background loop ──────────────────────────────────────────────────

    def _run_ble_loop(self):
        asyncio.set_event_loop(self._ble_loop)
        self._ble_loop.run_forever()

    # ── Public API (called from FastAPI event loop) ───────────────────────────

    async def connect(self, address: Optional[str] = None):
        """Dispatch BLE scan + connect to the BLE loop; await the result."""
        self._fastapi_loop = asyncio.get_event_loop()
        cf = asyncio.run_coroutine_threadsafe(self._ble_connect(address=address), self._ble_loop)
        await asyncio.wrap_future(cf)

    async def disconnect(self):
        """Dispatch BLE disconnect to the BLE loop; await the result."""
        cf = asyncio.run_coroutine_threadsafe(self._ble_disconnect(), self._ble_loop)
        await asyncio.wrap_future(cf)

    async def send_command(self, command: str) -> bool:
        """
        Dispatch a motor command to the BLE loop and await COMPLETE.
        Fully awaitable from the experiment loop.
        """
        cf = asyncio.run_coroutine_threadsafe(self._ble_send(command), self._ble_loop)
        return await asyncio.wrap_future(cf)

    async def send_command_raw(self, command: str) -> str:
        """
        Dispatch a command and return the first ESP32 status token received.
        """
        cf = asyncio.run_coroutine_threadsafe(self._ble_send_raw(command), self._ble_loop)
        return await asyncio.wrap_future(cf)

    async def fan_on(self) -> bool:
        """Turn desorption fan ON (expects OK from firmware)."""
        result = await self.send_command_raw("FANON")
        if result == "OK":
            self._log_sync("✓ Fan ON")
            return True
        self._log_sync(f"✗ Fan ON failed: {result}")
        return False

    async def fan_off(self) -> bool:
        """Turn desorption fan OFF (expects OK from firmware)."""
        result = await self.send_command_raw("FANOFF")
        if result == "OK":
            self._log_sync("✓ Fan OFF")
            return True
        self._log_sync(f"✗ Fan OFF failed: {result}")
        return False

    async def optical_sensor_is_low(self) -> bool:
        """Read the active-low carousel optical sensor."""
        result = await self.send_command_raw("SENSORGPIO5")
        is_low = result == "SENSOR:LOW"
        self._log_sync(f"Optical sensor: {result}")
        return is_low

    async def stop(self):
        """Emergency STOP — immediately halt any motor movement."""
        cf = asyncio.run_coroutine_threadsafe(self._ble_stop(), self._ble_loop)
        await asyncio.wrap_future(cf)

    async def _ble_stop(self):
        """Write STOP directly to the GATT char, bypassing _pending_future to
        avoid a race with an in-flight motor command."""
        if not self.connected or not self._client:
            self._log_sync("✗ STOP failed: not connected")
            return False
        try:
            self._log_sync("→ STOP (emergency)")
            await self._client.write_gatt_char(
                BLE_CMD_CHAR_UUID, b"STOP", response=True,
            )
            self._log_sync("✓ STOP acknowledged — motors halted")
            return True
        except Exception as e:
            self._log_sync(f"✗ STOP error: {e}")
            return False

    # ── BLE-loop coroutines (run on self._ble_loop) ───────────────────────────

    async def _ble_connect(self, address: Optional[str] = None):
        if self.connected and self._client and self._client.is_connected:
            self._log_sync("Already connected to ESP32")
            self._emit_sync("connection_status", message="Connected", connected=True)
            return

        self._log_sync(f"Scanning for '{BLE_DEVICE_NAME}'…")
        self._emit_sync("connection_status", message="Scanning…", connected=False)

        device = None
        try:
            device = await BleakScanner.find_device_by_name(
                BLE_DEVICE_NAME, timeout=15.0, scanning_mode="active"
            )
        except Exception as e:
            self._log_sync(f"Name scan error: {e}")

        # 1) Name-based connect
        if device is not None:
            ok = await self._connect_client(device, label=f"{BLE_DEVICE_NAME} ({device.address})")
            if ok:
                return

        # 2) MAC fallback (optional)
        mac = str(address or "").strip()
        if mac:
            # Give BlueZ a moment to release state after a failed attempt.
            await asyncio.sleep(0.8)
            self._log_sync(f"Name-based connection failed. Trying MAC: {mac}…")
            ok = await self._connect_client(mac, label=f"ESP32 via MAC ({mac})")
            if ok:
                return

        self._log_sync(f"✗ Could not connect to {BLE_DEVICE_NAME} (name first, then MAC)")
        self._emit_sync("connection_status", message="Connection failed", connected=False)
        self.connected = False

    async def _connect_client(self, target, label: str) -> bool:
        """Connect to BLE target (BLEDevice or MAC string) and subscribe to notifications."""
        client = None
        try:
            self._log_sync(f"Connecting to {label}…")
            client = BleakClient(target, disconnected_callback=self._on_ble_disconnect)
            await client.connect()
            await client.start_notify(BLE_STS_CHAR_UUID, self._on_notification)
            self._client = client
            self.connected = True
            self._log_sync(f"✓ Connected to {label}")
            self._emit_sync("connection_status", message=f"Connected to {label}", connected=True)
            return True
        except Exception as e:
            self._log_sync(f"✗ Connection error ({label}): {e}")
            if client:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
            self._client = None
            self.connected = False
            return False

    async def _ble_disconnect(self):
        self.connected = False
        if self._client and self._client.is_connected:
            try:
                try:
                    await self._client.stop_notify(BLE_STS_CHAR_UUID)
                except Exception:
                    pass
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._log_sync("Disconnected from ESP32")
        self._emit_sync("connection_status", message="Disconnected", connected=False)

    async def _ble_send(self, command: str) -> bool:
        if not self.connected or not self._client:
            self._log_sync("✗ Not connected to ESP32")
            return False
        try:
            self._log_sync(f"→ {command}")
            # Future lives on the BLE loop — safe to create and await here
            self._pending_future = self._ble_loop.create_future()
            await self._client.write_gatt_char(
                BLE_CMD_CHAR_UUID,
                command.encode("utf-8"),
                response=True,
            )
            result = await asyncio.wait_for(self._pending_future, timeout=COMMAND_TIMEOUT)
            self._log_sync(f"← {result}")
            if result in ("COMPLETE", "STOPPED"):
                self._log_sync("✓ Movement completed")
                return True
            else:
                self._log_sync(f"✗ Unexpected response: {result}")
                return False
        except asyncio.TimeoutError:
            self._log_sync(f"✗ Timeout waiting for response to {command}")
            return False
        except Exception as e:
            self._log_sync(f"✗ Error sending {command}: {e}")
            self.connected = False
            return False
        finally:
            self._pending_future = None

    async def _ble_send_raw(self, command: str) -> str:
        if not self.connected or not self._client:
            self._log_sync("✗ Not connected to ESP32")
            return "ERROR"
        try:
            self._log_sync(f"→ {command}")
            self._accept_ok_response = True
            self._pending_future = self._ble_loop.create_future()
            await self._client.write_gatt_char(
                BLE_CMD_CHAR_UUID,
                command.encode("utf-8"),
                response=True,
            )
            result = await asyncio.wait_for(self._pending_future, timeout=COMMAND_TIMEOUT)
            result = str(result).strip().upper()
            self._log_sync(f"← {result}")
            return result
        except asyncio.TimeoutError:
            self._log_sync(f"✗ Timeout waiting for response to {command}")
            return "TIMEOUT"
        except Exception as e:
            self._log_sync(f"✗ Error sending {command}: {e}")
            self.connected = False
            return "ERROR"
        finally:
            self._pending_future = None
            self._accept_ok_response = False

    # ── Callbacks (may be called from bleak's internal thread) ───────────────

    def _on_notification(self, _sender, data: bytearray):
        """
        "OK" is the ESP32 start-ack — ignored; only COMPLETE/STOPPED unblocks send.
        Resolving via call_soon_threadsafe is safe from any thread.
        """
        value = data.decode("utf-8").strip().upper()
        if value == "OK" and not self._accept_ok_response:
            return
        if self._pending_future and not self._pending_future.done():
            self._ble_loop.call_soon_threadsafe(self._pending_future.set_result, value)

    def _on_ble_disconnect(self, _client: BleakClient):
        """Called by bleak on unexpected disconnect."""
        # Ignore callbacks from stale clients created during failed connect attempts.
        if self._client is not None and _client is not self._client:
            return

        was_connected = self.connected
        self.connected = False
        self._client = None
        if not was_connected:
            return
        self._log_sync("ESP32 disconnected unexpectedly")
        self._emit_sync("connection_status",
                        message="ESP32 disconnected unexpectedly", connected=False)
        if self._pending_future and not self._pending_future.done():
            self._ble_loop.call_soon_threadsafe(
                self._pending_future.set_exception,
                ConnectionError("ESP32 disconnected unexpectedly"),
            )
        try:
            if self.on_unexpected_disconnect:
                self.on_unexpected_disconnect()
        except Exception:
            pass

    # ── Thread-safe helpers (callable from BLE loop or any thread) ───────────

    def _emit_sync(self, msg_type: str, **kwargs):
        """Fire-and-forget: send a WS message from the BLE loop to the FastAPI loop."""
        if self.broadcast_callback and self._fastapi_loop:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_callback({"type": msg_type, **kwargs}),
                self._fastapi_loop,
            )

    def _log_sync(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._emit_sync("log", message=f"[{ts}] {message}",
                        timestamp=datetime.now().isoformat())
