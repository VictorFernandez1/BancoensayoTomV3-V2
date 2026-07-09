"""
Serial Communication Handler (TOMV3)
Async wrapper around pyserial — identical to tomv3-web version.
"""

import asyncio
from datetime import datetime
from typing import Optional, Callable, List

import serial
import serial.tools.list_ports


class SerialHandler:
    """Handles async serial communication with the TOMV3 device."""

    def __init__(self, broadcast_callback: Optional[Callable] = None):
        self.ser: Optional[serial.Serial] = None
        self.is_connected = False
        self.current_device_id: str = ""
        self.last_connect_error: str = ""
        self.broadcast_callback = broadcast_callback
        self.read_task: Optional[asyncio.Task] = None
        self.baud_rate = 9600

        # Data buffers
        self.data = [[] for _ in range(47)]        # last 100 pts for plotting
        self.datasaved = [[] for _ in range(47)]   # full history for CSV
        self.timestamps: List[str] = []

        self.data_names = [
            "SGP40_VOC", "SGP40_NOx", "BME_Res", "BME_Temp", "BME_HR","AQI","ENS_eCO2", "ENS_TVOC","ENS_R1", "ENS_R2", "ENS_R3", "ENS_R4",
            "Violet", "Dark blue", "Blue", "Light blue", "Green",
            "Yellow", "Orange", "Red", "Clear", "NIR",
            "MICS_CO2", "MICS_VOC", "MICS_R",
            "ZMOD_R1", "ZMOD_R2", "ZMOD_R3", "ZMOD_R4", "ZMOD_R5", "ZMOD_R6",
            "ZMOD_R7", "ZMOD_R8", "ZMOD_R9", "ZMOD_R10", "ZMOD_R11", "ZMOD_R12",
            "ZMOD_R13", "ZMOD_RCDA", "ZMOD_ETOH", "ZMOD_TVOC", "ZMOD_ECO2",
            "ZMOD_IAQ", "ZMOD_REL_IAQ", "TEMPINDX", "TEMP", "VOLT",
        ]

        self.last_command_response: str = ""
        self._command_response_event = asyncio.Event()
        self._command_lock = asyncio.Lock()


    # ── Port discovery ───────────────────────────────────────────────────────

    async def list_ports(self) -> List[str]:
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]

    # ── Connection ───────────────────────────────────────────────────────────

    async def connect(self, port: str) -> bool:
        self.last_connect_error = ""
        try:
            if self.ser and self.ser.is_open:
                await self.disconnect()

            self.ser = serial.Serial(port, self.baud_rate, timeout=0.01)
            self.is_connected = True
            self.current_device_id = ""
            self.read_task = asyncio.create_task(self._read_loop())

            # Give TOMV3 a short warm-up time after opening the serial port.
            await asyncio.sleep(1.0)
            raw_response = await self.send_command_and_wait("DISPO\n", timeout_sec=5.0)
            if not raw_response:
                self.last_connect_error = "DISPO handshake failed: no response from device"
                await self._log(f"✗ {self.last_connect_error}")
                await self._emit_serial_status(message="Disconnected", connected=False)
                await self._close_serial_resources()
                return False

            self.current_device_id = raw_response
            await self._emit_serial_status(
                message=f"Connected to {raw_response}",
                connected=True,
                device_id=raw_response,
                port=port,
            )
            await self._log(f"Connected to {port} at {self.baud_rate} baud (device: {raw_response})")
            return True
        except Exception as e:
            self.last_connect_error = f"Error connecting to {port}: {e}"
            await self._log(self.last_connect_error)
            await self._emit_serial_status(message="Disconnected", connected=False)
            await self._close_serial_resources()
            return False

    async def disconnect(self):
        await self._close_serial_resources()
        await self._emit_serial_status(message="Disconnected", connected=False)
        await self._log("Disconnected from serial port")

    # ── Read loop ────────────────────────────────────────────────────────────

    async def _read_loop(self):
        try:
            while self.is_connected and self.ser and self.ser.is_open:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self.ser.readline
                )
                if line:
                    line_str = line.decode("utf-8").strip()
                    if line_str:
                        await self._parse_data(line_str)
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self._log(f"Error in read loop: {e}")
            self.is_connected = False
            self.current_device_id = ""
            await self._emit_serial_status(message="Disconnected", connected=False)
            if self.broadcast_callback:
                await self.broadcast_callback({
                    "type": "serial_disconnected",
                    "message": f"Serial port disconnected unexpectedly: {e}",
                })

    # ── Data parsing ─────────────────────────────────────────────────────────

    async def _parse_data(self, line: str):
        values = line.split("_")
        if len(values) == 47:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.timestamps.append(current_time)

            for i in range(47):
                try:
                    value = float(values[i])
                    self.data[i].append(value)
                    self.datasaved[i].append(value)
                    if len(self.data[i]) > 100:
                        self.data[i].pop(0)
                except ValueError:
                    pass

            if self.broadcast_callback:
                plot_indices = [0, 1, 2, 9, 10, 11]
                plot_data = {str(idx): self.data[idx].copy() for idx in plot_indices}
                await self.broadcast_callback({
                    "type": "data",
                    "timestamp": current_time,
                    "plot_data": plot_data,
                    "latest_values": {str(i): values[i] for i in range(47)},
                })
            return

        self.last_command_response = line.strip()
        self._command_response_event.set()

    # ── Commands ─────────────────────────────────────────────────────────────

    async def send_experiment_params(self, params: dict):
        if not self.is_connected or not self.ser:
            return
        campo_map = {
            "TMin": "TMA", "TMax": "TMI",
            "VMin": "VMA", "VMax": "VMI",
            "Steps": "NTEM", "Cycles": "NRE",
        }
        parts = [f"{campo_map[k]}{params[k]}" for k in campo_map if k in params]
        await self._send_command(";".join(parts))

    async def send_sweep_type(self, sweep_type: str):
        if not self.is_connected or not self.ser:
            return
        await self._send_command(f"{sweep_type}\n")

    async def send_exper_command(self):
        if not self.is_connected or not self.ser:
            return
        await self._send_command("EXPER\n")
        await self._log("Sent EXPER command")

    async def clear(self):
        """Clear data buffers  — called by the integrated experiment."""
        self.clear_data_buffers()
        await self._log("Data buffers cleared.")


    async def exper(self):
        """Send EXPER — called by the integrated experiment."""
        await self._log("Sending EXPER command…")
        await self.send_exper_command()

    async def _send_command(self, command: str):
        if not self.ser or not self.ser.is_open:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self.ser.write, command.encode()
            )
            await self._log(f"Enviado: {command.strip()}")
        except Exception as e:
            await self._log(f"Error sending command: {e}")

    async def send_command_and_wait(self, command: str, timeout_sec: float = 5.0) -> str:
        """Send a command and wait for a non-telemetry response line."""
        if not self.is_connected or not self.ser:
            return ""

        async with self._command_lock:
            self.last_command_response = ""
            self._command_response_event.clear()
            await self._send_command(command)

            try:
                await asyncio.wait_for(self._command_response_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                await self._log(f"Timeout waiting for response to: {command.strip()}")
                return ""

            return self.last_command_response.strip()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def clear_data_buffers(self):
        self.datasaved = [[] for _ in range(47)]
        self.timestamps = []

    async def _close_serial_resources(self):
        self.is_connected = False
        self.current_device_id = ""

        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            finally:
                self.read_task = None

        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None

    async def _emit_serial_status(self, message: str, connected: bool, **kwargs):
        if self.broadcast_callback:
            await self.broadcast_callback({
                "type": "serial_status",
                "message": message,
                "connected": connected,
                **kwargs,
            })

    async def _log(self, message: str):
        if self.broadcast_callback:
            await self.broadcast_callback({
                "type": "log",
                "message": message,
                "timestamp": datetime.now().isoformat(),
            })

