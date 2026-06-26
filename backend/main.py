"""
Integrated TOMV3 + Banco de Ensayo — FastAPI Backend
Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
Then open http://localhost:8000
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Set

import sys
import os
from pathlib import Path

# Handle bundled vs development environment
if getattr(sys, 'frozen', False):
    # Running as bundled exe
    BASE_DIR = sys._MEIPASS
    FRONTEND_DIR = Path(BASE_DIR) / "frontend"
    # Use user's home directory for persistent exports
    EXPORTS_DIR = Path.home() / ".tomv3" / "exports"
else:
    # Running from source
    BACKEND_DIR = Path(__file__).parent
    FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
    EXPORTS_DIR = BACKEND_DIR / "exports"

EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from controller import BleController
from serial_handler import SerialHandler
from experiment import IntegratedExperimentController
from state import AppState, save_state, load_state
from camera import CameraStreamer

# ── Global singletons (initialised in startup) ────────────────────────────────

banco_controller: BleController             = None
serial_handler:   SerialHandler             = None
experiment_ctrl:  IntegratedExperimentController = None
app_state:        AppState                  = None
camera_streamer:  CameraStreamer             = None


# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead: Set[WebSocket] = set()
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Integrated TOMV3 + Banco de Ensayo", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    global banco_controller, serial_handler, experiment_ctrl, app_state, camera_streamer

    app_state = await load_state()
    camera_streamer = CameraStreamer()

    banco_controller = BleController(broadcast_callback=manager.broadcast)
    serial_handler   = SerialHandler(broadcast_callback=manager.broadcast)
    experiment_ctrl  = IntegratedExperimentController(
        banco_controller=banco_controller,
        serial_handler=serial_handler,
        broadcast_callback=manager.broadcast,
        exports_dir=EXPORTS_DIR,
    )

    print("✓ Integrated app started")
    print(f"  Frontend : {FRONTEND_DIR}")
    print(f"  Exports  : {EXPORTS_DIR}")


@app.on_event("shutdown")
async def shutdown_event():
    if app_state:
        await save_state(app_state)
    if experiment_ctrl and experiment_ctrl.experiment_running:
        await experiment_ctrl.cancel_experiment()
    if serial_handler and serial_handler.is_connected:
        await serial_handler.disconnect()
    if banco_controller and banco_controller.connected:
        await banco_controller.disconnect()
    if camera_streamer:
        camera_streamer.release()


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "serial_connected": serial_handler.is_connected if serial_handler else False,
        "pi_connected": banco_controller.connected if banco_controller else False,
        "experiment_running": experiment_ctrl.experiment_running if experiment_ctrl else False,
        "experiment_mode": experiment_ctrl.experiment_mode if experiment_ctrl else "",
    }


@app.get("/api/state")
async def get_state():
    if not app_state:
        return {"error": "State not initialised"}
    return {
        "serial_port":      app_state.serial_port,
        "serial_connected": serial_handler.is_connected if serial_handler else False,
        "serial_device_id": serial_handler.current_device_id if serial_handler else "",
        "ble_connected":    banco_controller.connected if banco_controller else False,
        "ble_address":      app_state.ble_address,
        "experiment_params": app_state.experiment_params,
        "experiment_running": experiment_ctrl.experiment_running if experiment_ctrl else False,
        "experiment_mode": experiment_ctrl.experiment_mode if experiment_ctrl else "",
        "sweep_type":       app_state.sweep_type,
        "positions":        app_state.positions,
        "desorption_time":  app_state.desorption_time,
        "pre_conditioning_time": app_state.pre_conditioning_time,
        "cycles":           app_state.cycles,
        "data_points":      len(serial_handler.timestamps) if serial_handler else 0,
    }


@app.get("/api/ports")
async def list_ports():
    if not serial_handler:
        return {"ports": []}
    ports = await serial_handler.list_ports()
    return {"ports": ports}


@app.post("/api/export")
async def export_data():
    """Manual on-demand CSV export (all data since last clear)."""
    if not serial_handler or len(serial_handler.timestamps) == 0:
        raise HTTPException(status_code=400, detail="No data to export")

    import pandas as pd

    if experiment_ctrl:
        device_id = await experiment_ctrl.get_device_id_for_filename()
    else:
        raw_response = await serial_handler.send_command_and_wait("DISPO\n", timeout_sec=5.0)
        device_id = "".join(c for c in (raw_response or "") if c.isalnum() or c in "-_") or "TOMV3"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{device_id}_File_{ts}.csv"
    filepath = EXPORTS_DIR / filename

    min_len = len(serial_handler.timestamps)
    for ch in serial_handler.datasaved:
        min_len = min(min_len, len(ch))

    data_dict = {"Fecha": serial_handler.timestamps[:min_len]}
    for i, name in enumerate(serial_handler.data_names):
        data_dict[name] = serial_handler.datasaved[i][:min_len]

    df = pd.DataFrame(data_dict)
    df.to_csv(filepath, index=False)

    return {
        "success": True,
        "filename": filename,
        "download_url": f"/api/download/{filename}",
        "rows": len(df),
        "columns": len(df.columns),
    }


@app.get("/api/download/{filename}")
async def download_file(filename: str):
    filepath = EXPORTS_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="text/csv", filename=filename)


@app.get("/api/video")
async def video_stream():
    """MJPEG stream from the USB camera."""
    return StreamingResponse(
        camera_streamer.frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/camera/status")
async def camera_status():
    """Returns whether a camera is available and which index is in use."""
    return {
        "available": camera_streamer.available if camera_streamer else False,
        "index": camera_streamer.camera_index if camera_streamer else None,
    }


@app.post("/api/camera/reconnect")
async def camera_reconnect():
    """Try to reopen the camera (blocking call run in a thread pool executor)."""
    if camera_streamer:
        await asyncio.get_event_loop().run_in_executor(None, camera_streamer.reconnect)
    return {
        "available": camera_streamer.available if camera_streamer else False,
        "index": camera_streamer.camera_index if camera_streamer else None,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial state
        await ws.send_json({"type": "connected", "message": "Connected to server"})
        if app_state:
            await ws.send_json({"type": "state_sync", "state": {
                "serial_connected":  serial_handler.is_connected if serial_handler else False,
                "serial_device_id":  serial_handler.current_device_id if serial_handler else "",
                "serial_port":       app_state.serial_port,
                "experiment_params": app_state.experiment_params,
                "sweep_type":        app_state.sweep_type,
                "positions":         app_state.positions,
                "desorption_time":   app_state.desorption_time,
                "pre_conditioning_time": app_state.pre_conditioning_time,
                "cycles":            app_state.cycles,
                "ble_address":       app_state.ble_address,
                "ble_connected":     banco_controller.connected if banco_controller else False,
                "experiment_running": experiment_ctrl.experiment_running if experiment_ctrl else False,
                "experiment_mode": experiment_ctrl.experiment_mode if experiment_ctrl else "",
                "experiment_state": {
                    "current_position":          experiment_ctrl.current_position,
                    "current_cycle":             experiment_ctrl.current_cycle,
                    "cycles_total":              experiment_ctrl.cycles_total,
                    "current_sample":            experiment_ctrl.current_sample,
                    "total_positions":           experiment_ctrl.total_positions,
                    "total_experiment_seconds":  experiment_ctrl.total_experiment_seconds,
                    "experiment_elapsed_seconds": experiment_ctrl.experiment_elapsed_seconds,
                    "current_phase":             experiment_ctrl.current_phase,
                    "current_phase_description": experiment_ctrl.current_phase_description,
                    "current_phase_total":        experiment_ctrl.current_phase_total,
                    "current_phase_remaining":    experiment_ctrl.current_phase_remaining,
                } if (experiment_ctrl and experiment_ctrl.experiment_running) else None,
            }})

        while True:
            data = await ws.receive_json()
            await _handle_message(data, ws)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS error: {e}")
    finally:
        manager.disconnect(ws)


async def _handle_message(data: dict, ws: WebSocket):
    msg_type = data.get("type")
    try:

        # ── Serial / TOMV3 ─────────────────────────────────────────────

        if msg_type == "connect_serial":
            port = data.get("port")
            success = await serial_handler.connect(port)
            if success:
                app_state.serial_port = port
                await save_state(app_state)
            else:
                err = serial_handler.last_connect_error or f"Failed to connect to {port}"
                await ws.send_json({"type": "error", "message": err})

        elif msg_type == "disconnect_serial":
            await serial_handler.disconnect()

        elif msg_type == "led":
            print("Deprecated websocket message received: type='led' (ignored)")
            await ws.send_json({"type": "error", "message": "LED control is no longer supported"})

        elif msg_type == "config_params":
            params = {
                "TMin":   data.get("TMin",   200),
                "TMax":   data.get("TMax",   400),
                "VMin":   data.get("VMin",   0.5),
                "VMax":   data.get("VMax",   1.5),
                "Steps":  data.get("Steps",  21),
                "Cycles": data.get("Cycles", 2),
            }
            app_state.experiment_params = params
            await serial_handler.send_experiment_params(params)
            await save_state(app_state)

        elif msg_type == "sweep_type":
            sweep = data.get("mode", "TR")
            app_state.sweep_type = sweep
            await serial_handler.send_sweep_type(sweep)
            await save_state(app_state)

        # ── Banco de Ensayo / Raspberry Pi ──────────────────────────────────

        elif msg_type == "connect_ble":
            address = str(data.get("address", "") or "").strip()
            app_state.ble_address = address
            await save_state(app_state)
            asyncio.create_task(banco_controller.connect(address=address if address else None))

        elif msg_type == "disconnect_ble":
            await banco_controller.disconnect()

        # ── Integrated Experiment ────────────────────────────────────────────

        elif msg_type == "start_experiment":
            if experiment_ctrl.experiment_running:
                await ws.send_json({"type": "error", "message": "Another experiment is already running"})
                return

            sample_names      = data.get("sample_names", [""] * 5)
            enabled_positions = [int(p) for p in data.get("enabled_positions", [1])]
            desorption_time       = float(data.get("desorption_time", app_state.desorption_time))
            pre_conditioning_time = float(data.get("pre_conditioning_time", app_state.pre_conditioning_time))
            cycles                = int(data.get("cycles", app_state.cycles))

            # Persist updated settings
            app_state.positions = [
                {"enabled": (i + 1) in enabled_positions, "name": sample_names[i] if i < len(sample_names) else ""}
                for i in range(12)
            ]
            app_state.desorption_time = desorption_time
            app_state.pre_conditioning_time = pre_conditioning_time
            app_state.cycles = cycles
            await save_state(app_state)

            await experiment_ctrl.start_experiment(
                sample_names=sample_names,
                enabled_positions=enabled_positions,
                desorption_time=desorption_time,
                cycles=cycles,
                pre_conditioning_time=pre_conditioning_time,
                experiment_params=app_state.experiment_params,
                sweep_type=app_state.sweep_type,
            )

        elif msg_type == "start_manual_experiment":
            if experiment_ctrl.experiment_running:
                await ws.send_json({"type": "error", "message": "Another experiment is already running"})
                return

            sample_name = str(data.get("sample_name", "") or "").strip()
            if not sample_name:
                await ws.send_json({"type": "error", "message": "Sample name is required for manual experiment"})
                return

            pre_conditioning_time = float(data.get("pre_conditioning_time", app_state.pre_conditioning_time))
            app_state.pre_conditioning_time = pre_conditioning_time
            await save_state(app_state)

            await experiment_ctrl.start_manual_experiment(
                sample_name=sample_name,
                pre_conditioning_time=pre_conditioning_time,
                experiment_params=app_state.experiment_params,
                sweep_type=app_state.sweep_type,
            )

        elif msg_type == "cancel_experiment":
            await experiment_ctrl.cancel_experiment()

        # ── Config save ─────────────────────────────────────────────────────

        elif msg_type == "save_config":
            cfg = data.get("config", {})
            if "desorption_time"       in cfg: app_state.desorption_time       = float(cfg["desorption_time"])
            if "pre_conditioning_time" in cfg: app_state.pre_conditioning_time = float(cfg["pre_conditioning_time"])
            if "cycles"                in cfg: app_state.cycles                = int(cfg["cycles"])
            if "positions"             in cfg: app_state.positions             = cfg["positions"]
            if "experiment_params"     in cfg: app_state.experiment_params     = cfg["experiment_params"]
            if "sweep_type"            in cfg: app_state.sweep_type            = cfg["sweep_type"]
            if "ble_address"           in cfg: app_state.ble_address           = str(cfg["ble_address"] or "").strip()
            await save_state(app_state)
            await ws.send_json({"type": "config_saved"})

        # ── Misc ─────────────────────────────────────────────────────────────

        elif msg_type == "clear_log":
            await manager.broadcast({"type": "log_cleared"})

        else:
            await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except Exception as e:
        await ws.send_json({"type": "error", "message": f"Error handling '{msg_type}': {e}"})


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    try:
        print("Starting TOMV3 server...")
        print("Server will be accessible at http://localhost:8053")
        print(f"Frontend: {FRONTEND_DIR}")
        print(f"Exports: {EXPORTS_DIR}")
        # Don't use reload in bundled exe mode
        uvicorn.run(app, host="0.0.0.0", port=8053, reload=False)
    except Exception as e:
        print(f"❌ Server startup failed: {e}")
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")

