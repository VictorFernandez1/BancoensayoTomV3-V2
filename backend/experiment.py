"""
Integrated Experiment Controller
Orchestrates the full measurement workflow:

  For each cycle × enabled position:
    1. MOVEDOWN  (motor)
    2. 300 s pre-conditioning countdown
    3. clear_and_arm()  (clear TOMV3 buffers + send EXPER)
                4. Acquisition countdown  = (Steps + 1) × Cycles × 2 + 30 s
        5. MOVEUP  (motor)
        6. MOVECLOCKWISE  (skip after last position in cycle)
        7. Desorption countdown  (always — every position including last)
        8. Auto-save CSV  →  exports_dir / <device>_File_<ts>_<name>.csv  (downloaded to client)

  After all positions in a cycle:
    8. Return home  (MOVECOUNTERCLOCKWISE × (n_positions − 1))

  Repeat for the configured number of cycles.
"""

import asyncio
import math
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, List, Dict

EXPORTS_DIR_DEFAULT = Path(__file__).parent / "exports"

PRE_CONDITIONING_SECONDS = 300   # default fallback — overridden by AppState


class IntegratedExperimentController:

    def __init__(
        self,
        banco_controller,        # BancoController instance
        serial_handler,          # SerialHandler instance
        broadcast_callback: Optional[Callable] = None,
        exports_dir: Optional[Path] = None,
    ):
        self.banco = banco_controller
        self.serial = serial_handler
        self.broadcast_callback = broadcast_callback
        self.exports_dir = exports_dir or EXPORTS_DIR_DEFAULT
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        self.experiment_running = False
        self.experiment_mode: str = ""
        self._task: Optional[asyncio.Task] = None

        # ── Live state (for reconnecting clients) ────────────────────────────
        self.current_position: int        = 0
        self.current_cycle: int           = 0
        self.cycles_total: int            = 0
        self.current_sample: str          = ""
        self.total_positions: int         = 0
        self.total_experiment_seconds: int = 0
        self.experiment_elapsed_seconds: int = 0
        # Current phase (for reconnecting clients)
        self.current_phase: str             = ""
        self.current_phase_description: str = ""
        self.current_phase_total: int       = 0
        self.current_phase_remaining: int   = 0

        # Position-check gate state (for reconnect + operator decisions)
        self.position_check_pending: bool = False
        self.position_check_response: str = ""
        self.position_check_position: int = 0
        self.position_check_sample: str = ""
        self.position_check_cycle: int = 0
        self.position_check_cycles_total: int = 0
        self._position_check_event = asyncio.Event()
        self._position_check_action: str = ""
        self.fan_is_on: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def start_experiment(
        self,
        sample_names: List[str],
        enabled_positions: List[int],
        desorption_time: float,
        cycles: int,
        pre_conditioning_time: float,
        experiment_params: Dict,
        sweep_type: str = "TR",
    ):
        if self.experiment_running:
            await self._log("Experiment already running — ignoring request.")
            return False

        # Reset cancel flag on controller
        self.banco.cancel_requested = False
        self.banco.motor_is_down = False
        self.fan_is_on = False

        self.experiment_running = True
        self.experiment_mode = "integrated"
        self._task = asyncio.create_task(
            self._run(
                sample_names,
                enabled_positions,
                desorption_time,
                cycles,
                pre_conditioning_time,
                experiment_params,
                sweep_type,
            )
        )
        return True

    async def start_manual_experiment(
        self,
        sample_name: str,
        pre_conditioning_time: float,
        experiment_params: Dict,
        sweep_type: str = "TR",
    ):
        if self.experiment_running:
            await self._log("Experiment already running — ignoring manual request.")
            return False

        self.banco.cancel_requested = False
        self.experiment_running = True
        self.experiment_mode = "manual"
        self._task = asyncio.create_task(
            self._run_manual(
                sample_name=sample_name,
                pre_conditioning_time=pre_conditioning_time,
                experiment_params=experiment_params,
                sweep_type=sweep_type,
            )
        )
        return True

    async def cancel_experiment(self):
        """Request cancellation; motor safety is handled inside the task."""
        if not self.experiment_running:
            return
        await self._log("Cancel requested…")
        self.banco.cancel_requested = True
        self._position_check_event.set()
        if self._task:
            self._task.cancel()

    async def set_position_check_action(self, action: str):
        """Operator response while waiting for GETPOSITION validation."""
        act = (action or "").strip().lower()
        if act == "continue":
            self._position_check_action = "continue"
            self._position_check_event.set()
        elif act == "cancel":
            await self.cancel_experiment()

    # ── Main sequence ─────────────────────────────────────────────────────────

    async def _run(
        self,
        sample_names: List[str],
        enabled_positions: List[int],
        desorption_time: float,
        cycles: int,
        pre_conditioning_time: float,
        experiment_params: Dict,
        sweep_type: str = "TR",
    ):
        # ── Auto-send TOMV3 parameters before the loop ─────────────────────
        await self._log("Sending TOMV3 parameters…")
        await self.serial.send_experiment_params(experiment_params)
        await asyncio.sleep(3)  # small delay to ensure TOMV3 is ready for next command
        await self._log(f"Sending sweep type: {sweep_type}")
        await self.serial.send_sweep_type(sweep_type)
        await asyncio.sleep(0.5)

        # Compute acquisition duration from TOMV3 sweep parameters
        steps = int(experiment_params.get("Steps", 21))
        veggie_cycles = int(experiment_params.get("Cycles", 4))
        acq_duration = (steps + 1) * veggie_cycles * 2 + 30

        # Runtime estimate includes motor movements per position and cycle return-home rotations.
        linear_motor_time = 5.1       # MOVEDOWN / MOVEUP (seconds each)
        rotational_motor_time = 5.525 # MOVECLOCKWISE / MOVECOUNTERCLOCKWISE (seconds each)

        total_positions = len(enabled_positions)
        per_position_seconds = (
            pre_conditioning_time + acq_duration + desorption_time + (2 * linear_motor_time)
        )
        per_cycle_rotation_seconds = (total_positions - 1) * 2 * rotational_motor_time
        total_experiment_seconds = math.ceil(
            desorption_time + cycles * ((total_positions * per_position_seconds) + per_cycle_rotation_seconds)
        )

        # Persist for reconnecting clients
        self.total_positions          = total_positions
        self.total_experiment_seconds = total_experiment_seconds
        self.cycles_total             = cycles
        self.experiment_elapsed_seconds = 0

        await self._emit("experiment_start",
                         total_seconds=total_experiment_seconds,
                         total_positions=total_positions)

        await self._log("=" * 60)
        await self._log("INTEGRATED EXPERIMENT STARTED")
        await self._log(f"Positions: {enabled_positions}  |  Cycles: {cycles}")
        await self._log(f"Pre-cond: {pre_conditioning_time}s  |  Desorption: {desorption_time}s  |  Acquisition: {acq_duration}s")
        await self._log(f"Staging folder: {self.exports_dir}")
        await self._log("=" * 60)

        try:
            # One-time initial desorption before cycle 1 / position 1.
            await self._status(f"Initial desorption ({desorption_time}s)…")
            await self._log("INITIAL DESORPTION PHASE")
            await self._log("FANON")
            fan_ok = await self.banco.fan_on()
            if not fan_ok:
                await self._log("⚠ Fan did not start for initial desorption. Continuing without fan for this phase.")
                await self._status("Initial desorption: Fan did not start, continuing…")
            else:
                self.fan_is_on = True

            cancelled = await self._countdown(
                "initial_desorption",
                int(desorption_time),
                f"Initial desorption ({int(desorption_time)}s)",
            )
            if cancelled or self.banco.cancel_requested:
                await self._ensure_fan_off("initial desorption interrupted")
                raise asyncio.CancelledError()

            await self._ensure_fan_off("initial desorption complete")

            for cycle in range(1, cycles + 1):
                if self.banco.cancel_requested:
                    break

                await self._log(f"\n{'='*60}")
                await self._log(f"CYCLE {cycle}/{cycles}")
                await self._log(f"{'='*60}")

                for idx, position in enumerate(enabled_positions):
                    if self.banco.cancel_requested:
                        break

                    sample_name = sample_names[position - 1] if position - 1 < len(sample_names) else ""
                    sample_label = sample_name or f"Position_{position}"
                    is_last = idx == len(enabled_positions) - 1

                    # Update live state for reconnecting clients
                    self.current_position = position
                    self.current_cycle    = cycle
                    self.current_sample   = sample_label

                    await self._status(f"Cycle {cycle}/{cycles} — Pos {position}: {sample_label}")
                    await self._emit("position", value=position,
                                     cycle=cycle, cycles_total=cycles,
                                     sample_name=sample_label,
                                     total_positions=total_positions)
                    await self._log(f"\n--- Cycle {cycle}/{cycles} | Position {position}: {sample_label} ---")


                    # 1. clear buffers  ───────────────────────────────
                    await self._status(f"Pos {position}: Acquiring data…")
                    await self.serial.clear()


                    # 2. GETPOSITION gate before MOVEDOWN ───────────────────
                    gate_ok = await self._wait_for_position_ok(
                        position=position,
                        sample_label=sample_label,
                        cycle=cycle,
                        cycles_total=cycles,
                    )
                    if not gate_ok or self.banco.cancel_requested:
                        break

                    # 3. MOVEDOWN ────────────────────────────────────────────
                    await self._status(f"Pos {position}: Moving DOWN…")
                    await self._log("MOVEDOWN")
                    ok = await self.banco.send_command("MOVEDOWN")
                    if not ok or self.banco.cancel_requested:
                        break
                    self.banco.motor_is_down = True
                    await asyncio.sleep(0.1)

                    # 4. Pre-conditioning countdown ──────────────────────────
                    await self._status(f"Pos {position}: Pre-conditioning ({pre_conditioning_time}s)…")
                    cancelled = await self._countdown(
                        "pre_conditioning",
                        int(pre_conditioning_time),
                        f"Pre-conditioning — Position {position}",
                    )
                    if cancelled or self.banco.cancel_requested:
                        break

                    # 5. Acquisition countdown ───────────────────────────────
                    await self.serial.exper()
                    cancelled = await self._countdown(
                        "acquisition",
                        acq_duration,
                        f"Acquisition — Position {position} ({acq_duration}s)",
                    )
                    if cancelled or self.banco.cancel_requested:
                        break

                    # 6. MOVEUP ──────────────────────────────────────────────
                    await self._status(f"Pos {position}: Moving UP…")
                    await self._log("MOVEUP")
                    ok = await self.banco.send_command("MOVEUP")
                    self.banco.motor_is_down = False
                    if not ok or self.banco.cancel_requested:
                        break
                    await asyncio.sleep(0.1)

                    # 7. Fan ON + MOVECLOCKWISE (skip after last) + desorption (always) ────
                    await self._log("FANON")
                    fan_ok = await self.banco.fan_on()
                    if not fan_ok:
                        await self._log("⚠ Fan did not start. Continuing without fan for this desorption phase.")
                        await self._status(f"Pos {position}: Fan did not start, continuing desorption…")
                    else:
                        self.fan_is_on = True

                    if not is_last:
                        await self._status(f"Pos {position}: Moving to next position…")
                        ok = await self.banco.send_command("MOVECLOCKWISE")
                        if not ok or self.banco.cancel_requested:
                            await self._ensure_fan_off("rotation interruption")
                            break
                        await asyncio.sleep(0.1)

                    await self._status(f"Pos {position}: Desorption ({desorption_time}s)…")
                    cancelled = await self._countdown(
                        "desorption",
                        int(desorption_time),
                        f"Desorption — after Position {position}",
                    )
                    if cancelled or self.banco.cancel_requested:
                        await self._ensure_fan_off("desorption interrupted")
                        break
                    await self._ensure_fan_off("desorption complete")

                    # 8. Auto-save CSV (after desorption) ────────────────────
                    await self._status(f"Pos {position}: Saving CSV…")
                    await self._save_csv(sample_label)

                # ── Return home after each cycle ─────────────────────────────
                if self.banco.cancel_requested:
                    break

                rotations_home = len(enabled_positions) - 1
                if rotations_home > 0:
                    await self._log(f"Returning to home position ({rotations_home} rotations)…")
                    await self._status(f"Cycle {cycle}: Returning home…")
                    for i in range(rotations_home):
                        ok = await self.banco.send_command("MOVECOUNTERCLOCKWISE")
                        if not ok:
                            await self._log("✗ Failed to return home")
                            break
                        await asyncio.sleep(0.5)

            # ── Finished or cancelled ─────────────────────────────────────────
            if self.banco.cancel_requested:
                await self._log("=" * 60)
                await self._log("EXPERIMENT CANCELLED")
                await self._ensure_fan_off("experiment cancellation")
                if self.banco.motor_is_down:
                    await self._log("Motor is DOWN — moving UP for safety…")
                    await self._status("Cancelling: Moving UP for safety…")
                    await self.banco.send_command("MOVEUP")
                    self.banco.motor_is_down = False
                await self._log("=" * 60)
                await self._status("Experiment cancelled")
                await self._emit("experiment_complete", cancelled=True,
                                 message="Experiment cancelled")
            else:
                await self._log("=" * 60)
                await self._log("EXPERIMENT COMPLETED SUCCESSFULLY")
                await self._log("=" * 60)
                await self._status("Experiment completed!")
                await self._emit("experiment_complete", cancelled=False,
                                 message="Experiment completed successfully")

        except asyncio.CancelledError:
            await self._log("Experiment task cancelled externally")
            await self._ensure_fan_off("task cancellation")
            if self.banco.motor_is_down:
                await self._log("Moving motor UP for safety…")
                await self.banco.send_command("MOVEUP")
                self.banco.motor_is_down = False
            await self._status("Experiment cancelled")
            await self._emit("experiment_complete", cancelled=True,
                             message="Experiment cancelled")

        except Exception as e:
            await self._ensure_fan_off("experiment error")
            await self._log(f"✗ Experiment error: {e}")
            await self._status(f"Error: {e}")

        finally:
            await self._ensure_fan_off("experiment cleanup")
            self.experiment_running = False
            self.experiment_mode = ""
            self.banco.cancel_requested = False
            self.current_position = 0
            self.current_cycle    = 0
            self.cycles_total     = 0
            self.current_sample   = ""
            self.total_positions  = 0
            self.total_experiment_seconds   = 0
            self.experiment_elapsed_seconds = 0
            self.current_phase            = ""
            self.current_phase_description = ""
            self.current_phase_total      = 0
            self.current_phase_remaining  = 0
            self.position_check_pending = False
            self.position_check_response = ""
            self.position_check_position = 0
            self.position_check_sample = ""
            self.position_check_cycle = 0
            self.position_check_cycles_total = 0
            self._position_check_action = ""
            self._position_check_event.clear()
            await self._emit("elapsed_time", seconds=0)

    async def _run_manual(
        self,
        sample_name: str,
        pre_conditioning_time: float,
        experiment_params: Dict,
        sweep_type: str = "TR",
    ):
        sample_label = (sample_name or "").strip() or "Manual"

        steps = int(experiment_params.get("Steps", 21))
        tomato_cycles = int(experiment_params.get("Cycles", 4))
        acq_duration = (steps + 1) * tomato_cycles * 2 + 30

        pre_seconds = max(0, int(pre_conditioning_time))
        total_seconds = pre_seconds + acq_duration

        try:
            await self._log("=" * 60)
            await self._log("MANUAL EXPERIMENT STARTED (NO BANCO)")
            await self._log(f"Sample: {sample_label}")
            await self._log(f"Pre-conditioning: {pre_seconds}s  |  Acquisition: {acq_duration}s")
            await self._log("=" * 60)

            await self._emit(
                "manual_experiment_start",
                sample_name=sample_label,
                pre_conditioning_seconds=pre_seconds,
                acquisition_seconds=acq_duration,
                total_seconds=total_seconds,
            )

            await self._log("Sending TOMV3 parameters…")
            await self.serial.send_experiment_params(experiment_params)
            await asyncio.sleep(3)
            await self._log(f"Sending sweep type: {sweep_type}")
            await self.serial.send_sweep_type(sweep_type)
            await asyncio.sleep(0.5)

            await self.serial.clear()

            if pre_seconds > 0:
                await self._log(f"Manual pre-conditioning ({pre_seconds}s)…")
                cancelled = await self._countdown(
                    "manual_pre_conditioning",
                    pre_seconds,
                    f"Pre-conditioning manual ({pre_seconds}s)",
                )
                if cancelled or self.banco.cancel_requested:
                    raise asyncio.CancelledError()

            await self._log(f"Manual acquisition ({acq_duration}s)…")
            await self.serial.exper()
            cancelled = await self._countdown(
                "manual_acquisition",
                acq_duration,
                f"Acquisition manual ({acq_duration}s)",
            )
            if cancelled or self.banco.cancel_requested:
                raise asyncio.CancelledError()

            await self._save_csv(sample_label)
            await self._log("MANUAL EXPERIMENT COMPLETED")
            await self._emit(
                "manual_experiment_complete",
                success=True,
                cancelled=False,
                sample_name=sample_label,
                message="Manual experiment completed successfully",
            )

        except asyncio.CancelledError:
            await self._log("Manual experiment cancelled")
            await self._emit(
                "manual_experiment_complete",
                success=False,
                cancelled=True,
                sample_name=sample_label,
                message="Manual experiment cancelled",
            )

        except Exception as e:
            await self._log(f"✗ Manual experiment error: {e}")
            await self._emit(
                "manual_experiment_complete",
                success=False,
                cancelled=False,
                sample_name=sample_label,
                message=str(e),
            )

        finally:
            self.experiment_running = False
            self.experiment_mode = ""
            self.banco.cancel_requested = False

    async def _wait_for_position_ok(self, position: int, sample_label: str, cycle: int, cycles_total: int) -> bool:
        """
        Send GETPOSITION and block experiment progress until ESP32 replies OK.
        On non-OK, wait for operator Continue/Cancel.
        """
        while not self.banco.cancel_requested:
            await self._status(f"Pos {position}: Checking sample position…")
            response = await self.banco.send_command_raw("GETPOSITION")
            if response == "OK":
                if self.position_check_pending:
                    self.position_check_pending = False
                    await self._emit("position_check_cleared")
                await self._log(f"✓ GETPOSITION OK at position {position}")
                return True

            self.position_check_pending = True
            self.position_check_response = response
            self.position_check_position = position
            self.position_check_sample = sample_label
            self.position_check_cycle = cycle
            self.position_check_cycles_total = cycles_total

            await self._log(
                f"⚠ GETPOSITION failed before MOVEDOWN (position {position}): {response}. Awaiting operator action."
            )
            await self._status(
                f"Pos {position}: Position mismatch. Place sample correctly and choose Continue/Cancel."
            )
            await self._emit(
                "position_check_required",
                position=position,
                sample_name=sample_label,
                cycle=cycle,
                cycles_total=cycles_total,
                response=response,
                message="Verifica la posicion de la muestra antes de continuar.",
            )

            self._position_check_action = ""
            self._position_check_event.clear()

            while not self.banco.cancel_requested:
                await self._position_check_event.wait()
                action = self._position_check_action
                self._position_check_action = ""
                self._position_check_event.clear()
                if action == "continue":
                    await self._emit("position_check_cleared")
                    break
                # "cancel" is handled via cancel_experiment(), which flips cancel_requested

            if self.banco.cancel_requested:
                return False

        return False

    # ── Countdown helper ──────────────────────────────────────────────────────

    async def _countdown(self, phase: str, total_seconds: int, description: str) -> bool:
        """
        Async countdown with 1-second ticks.
        Returns True if cancelled/interrupted, False if completed normally.
        """
        if self.broadcast_callback:
            await self.broadcast_callback({
                "type": "timer_start",
                "phase": phase,
                "total_seconds": total_seconds,
                "description": description,
            })

        self.current_phase             = phase
        self.current_phase_description = description
        self.current_phase_total       = total_seconds
        self.current_phase_remaining   = total_seconds

        for remaining in range(total_seconds, -1, -1):
            if self.banco.cancel_requested:
                return True
            self.current_phase_remaining = remaining
            if self.broadcast_callback:
                await self.broadcast_callback({
                    "type": "timer_update",
                    "phase": phase,
                    "remaining": remaining,
                    "total": total_seconds,
                    "elapsed": total_seconds - remaining,
                })
            await self._emit("elapsed_time", seconds=total_seconds - remaining)
            if remaining == 0:
                break
            self.experiment_elapsed_seconds += 1
            await asyncio.sleep(1)

        return False

    async def _ensure_fan_off(self, reason: str) -> bool:
        """Best-effort fan OFF guard used on all integrated-flow exit paths."""
        if not self.fan_is_on:
            return True
        await self._log(f"FANOFF ({reason})")
        ok = await self.banco.fan_off()
        if ok:
            self.fan_is_on = False
            return True
        await self._log("⚠ Fan OFF command did not confirm")
        return False

    # ── CSV save ──────────────────────────────────────────────────────────────

    async def _save_csv(self, sample_name: str):
        """Save all 47 channels to a temporary CSV in exports_dir for browser download."""
        try:
            import pandas as pd

            device_id = await self.get_device_id_for_filename()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in sample_name)
            filename = f"{device_id}_File_{ts}_{safe_name}.csv"
            filepath = self.exports_dir / filename

            timestamps = self.serial.timestamps
            min_len = len(timestamps)
            if min_len == 0:
                await self._log("⚠ No data collected — CSV not saved.")
                return

            for ch in self.serial.datasaved:
                min_len = min(min_len, len(ch))

            data_dict = {"Fecha": timestamps[:min_len]}
            for i, name in enumerate(self.serial.data_names):
                data_dict[name] = self.serial.datasaved[i][:min_len]

            df = pd.DataFrame(data_dict)
            # Run blocking I/O in executor to keep event loop free
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: df.to_csv(filepath, index=False)
            )

            await self._log(f"✓ CSV saved: {filepath}  ({min_len} rows)")
            await self._emit("auto_save_result", success=True,
                             filename=filename, rows=min_len,
                             download_url=f"/api/download/{filename}")

        except Exception as e:
            await self._log(f"✗ CSV save error: {e}")
            await self._emit("auto_save_result", success=False, message=str(e))

    async def get_device_id_for_filename(self) -> str:
        """Returns TOMV3 device identifier for filename prefix; falls back to TOMV3."""
        raw_response = await self.serial.send_command_and_wait("DISPO\n", timeout_sec=5.0)

        if raw_response:
            device_id = raw_response
            await self._log(f"DISPO response: {raw_response} -> using prefix: {device_id}")

        else:
            device_id = "TOMV3"
            await self._log("DISPO response not available, using fallback prefix: TOMV3")

        return device_id

    @staticmethod
    def _sanitize_filename_token(value: str, fallback: str = "TOMV3") -> str:
        cleaned = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
        return cleaned or fallback

    # ── Emitters ─────────────────────────────────────────────────────────────

    async def _status(self, message: str):
        await self._emit("experiment_status", message=message)

    async def _emit(self, msg_type: str, **kwargs):
        if self.broadcast_callback:
            await self.broadcast_callback({"type": msg_type, **kwargs})

    async def _log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        await self._emit("log", message=f"[{ts}] {message}",
                         timestamp=datetime.now().isoformat())

