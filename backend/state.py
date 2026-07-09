"""
Application State Management
Single source of truth for all persisted settings:
    - TOMV3: serial port, sweep type, experiment params
    - Banco de Ensayo: hostname, positions, desorption_time, pre_conditioning_time, cycles
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import aiofiles




STATE_FILE = Path(__file__).parent / "session.json"

# Single source of truth for the number of sample positions.
# Change this value to add or remove positions in the UI.
MAX_POSITIONS = 12


def _default_positions() -> List[Dict]:
    return [{"enabled": i == 0, "name": ""} for i in range(MAX_POSITIONS)]


def _normalize_positions(raw_positions: List[Dict]) -> List[Dict]:
    defaults = _default_positions()
    normalized = []
    for i in range(MAX_POSITIONS):
        item = raw_positions[i] if i < len(raw_positions) and isinstance(raw_positions[i], dict) else {}
        normalized.append({
            "enabled": bool(item.get("enabled", defaults[i]["enabled"])),
            "name": str(item.get("name", defaults[i]["name"]) or ""),
        })
    return normalized


def _default_experiment_params() -> Dict:
    return {
        "TMin": 200,
        "TMax": 400,
        "VMin": 0.5,
        "VMax": 1.5,
        "Steps": 21,
        "Cycles": 4,
    }


@dataclass
class AppState:
    """Unified application state for both TOMV3 and Banco de Ensayo."""

    # ── TOMV3 ──────────────────────────────────────────────────────────
    serial_port: Optional[str] = None
    sweep_type: str = "TR"
    experiment_params: Dict = field(default_factory=_default_experiment_params)

    # ── Banco de Ensayo ──────────────────────────────────────────────────────
    positions: List[Dict] = field(default_factory=_default_positions)
    desorption_time: float = 60.0
    pre_conditioning_time: float = 300.0
    cycles: int = 1
    ble_address: str = ""


async def save_state(state: AppState):
    """Persist state to session.json (data arrays excluded — too large)."""
    try:
        state_dict = {
            "serial_port":           state.serial_port,
            "sweep_type":            state.sweep_type,
            "experiment_params":     state.experiment_params,
            "positions":             state.positions,
            "desorption_time":       state.desorption_time,
            "pre_conditioning_time": state.pre_conditioning_time,
            "cycles":                state.cycles,
            "ble_address":           state.ble_address,
        }
        async with aiofiles.open(STATE_FILE, "w") as f:
            await f.write(json.dumps(state_dict, indent=2))
    except Exception as e:
        print(f"Error saving state: {e}")


async def load_state() -> AppState:
    """Load state from session.json; fall back to defaults on any error."""
    try:
        if STATE_FILE.exists():
            async with aiofiles.open(STATE_FILE, "r") as f:
                content = await f.read()
            d = json.loads(content)
            legacy_key_removed = "led_brightness" in d
            if legacy_key_removed:
                d.pop("led_brightness", None)

            state = AppState(
                serial_port=d.get("serial_port"),
                sweep_type=d.get("sweep_type", "TR"),
                experiment_params=d.get("experiment_params") or _default_experiment_params(),
                positions=_normalize_positions(d.get("positions") or _default_positions()),
                desorption_time=float(d.get("desorption_time", 60.0)),
                pre_conditioning_time=float(d.get("pre_conditioning_time", 300.0)),
                cycles=int(d.get("cycles", 1)),
                ble_address=str(d.get("ble_address", "") or "").strip(),
            )
            if legacy_key_removed:
                await save_state(state)
            return state
    except Exception as e:
        print(f"Error loading state: {e}")
    return AppState()

