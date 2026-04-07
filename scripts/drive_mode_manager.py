"""
Drive Mode Manager
=================
Bridges robot_control motor logic into the robot_inference FastAPI service.

Modes:
1) interactive: manual directional actions from Web UI
2) vlm: automatic cruise driven by VLM inference status API
"""

from __future__ import annotations

from collections import deque
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def _resolve_robot_control_dir() -> Path:
    env_value = os.environ.get("ROBOT_CONTROL_DIR", "").strip()
    if env_value:
        env_dir = Path(env_value).expanduser().resolve()
        if (env_dir / "config" / "cli_config.json").exists():
            return env_dir
        logger.warning(
            "ROBOT_CONTROL_DIR does not contain cli config: %s",
            env_dir / "config" / "cli_config.json",
        )

    candidates = (
        PROJECT_ROOT / "robot_control",
        WORKSPACE_ROOT / "robot_control",
    )
    for path in candidates:
        if (path / "config" / "cli_config.json").exists():
            return path

    # Default to project-local path so error messages point to expected location.
    return PROJECT_ROOT / "robot_control"


ROBOT_CONTROL_DIR = _resolve_robot_control_dir()
CLI_CONFIG_PATH = ROBOT_CONTROL_DIR / "config" / "cli_config.json"

if str(ROBOT_CONTROL_DIR) not in sys.path:
    sys.path.insert(0, str(ROBOT_CONTROL_DIR))

PWM = None
ActionDecisionEngine = None
MotionPolicy = None
VLMAction = None
VLMActionSource = None

try:
    from script.Motor import PWM  # type: ignore
    from script.vlm_action_controller import (  # type: ignore
        ActionDecisionEngine,
        MotionPolicy,
        VLMAction,
        VLMActionSource,
    )

    HARDWARE_AVAILABLE = True
    HARDWARE_IMPORT_ERROR = None
except Exception as exc:
    HARDWARE_AVAILABLE = False
    HARDWARE_IMPORT_ERROR = str(exc)

try:
    from gpiozero import DistanceSensor
except Exception:
    DistanceSensor = None


def _clamp_speed(value: int, max_duty: int) -> int:
    return max(0, min(max_duty, value))


class DriveModeManager:
    def __init__(self, *, status_url: str) -> None:
        self._lock = threading.RLock()
        self._mode = "interactive"
        self._last_manual_action = "stop"
        self._last_duties = (0, 0, 0, 0)
        self._error: str | None = HARDWARE_IMPORT_ERROR
        self._log_seq = 0
        self._logs: deque[dict[str, Any]] = deque(maxlen=500)

        self._vlm_thread: threading.Thread | None = None
        self._vlm_stop_event: threading.Event | None = None

        self._distance_sensor = None

        self._config = self._load_config()
        self._config.setdefault("vlm", {})
        self._config["vlm"]["status_url"] = status_url

        self._append_log(
            level="INFO",
            mode="interactive",
            message="Drive manager initialized",
            robot_control_dir=str(ROBOT_CONTROL_DIR),
            cli_config_path=str(CLI_CONFIG_PATH),
        )

        if not HARDWARE_AVAILABLE:
            logger.error("Drive control disabled: %s", self._error)
            self._append_log(
                level="ERROR",
                mode=self._mode,
                message="Drive control backend unavailable",
                error=self._error,
            )
            return

        ultrasonic_cfg = self._config.get("ultrasonic", {})
        if DistanceSensor is None:
            logger.warning("gpiozero unavailable, VLM distance safety is disabled")
        else:
            try:
                self._distance_sensor = DistanceSensor(
                    echo=int(ultrasonic_cfg.get("echo_pin", 22)),
                    trigger=int(ultrasonic_cfg.get("trigger_pin", 27)),
                    max_distance=3,
                )
            except Exception as exc:
                logger.warning("Distance sensor init failed: %s", exc)
                self._distance_sensor = None
                self._append_log(
                    level="WARNING",
                    mode=self._mode,
                    message="Distance sensor init failed",
                    error=str(exc),
                )

    def _append_log(
        self,
        *,
        level: str,
        mode: str,
        message: str,
        **fields: Any,
    ) -> None:
        event = {
            "id": self._log_seq,
            "ts": time.time(),
            "level": level,
            "mode": mode,
            "message": message,
            "fields": fields,
        }
        self._log_seq += 1
        self._logs.append(event)

    def get_logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            safe_limit = max(1, min(1000, int(limit)))
            return list(self._logs)[-safe_limit:]

    def _load_config(self) -> dict[str, Any]:
        if not CLI_CONFIG_PATH.exists():
            self._error = f"Missing CLI config: {CLI_CONFIG_PATH}"
            self._append_log(
                level="ERROR",
                mode=self._mode,
                message="CLI config not found",
                path=str(CLI_CONFIG_PATH),
            )
            return {}

        try:
            with CLI_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            self._error = f"Failed to load CLI config: {exc}"
            self._append_log(
                level="ERROR",
                mode=self._mode,
                message="Failed to load CLI config",
                error=str(exc),
            )
            return {}

    def _ensure_available(self) -> None:
        if not HARDWARE_AVAILABLE or PWM is None:
            raise RuntimeError(self._error or "Motor control backend unavailable")

    def _read_distance_cm(self) -> int | None:
        if self._distance_sensor is None:
            return None
        try:
            return int(self._distance_sensor.distance * 100)
        except Exception:
            return None

    def _set_motor(self, duties: tuple[int, int, int, int]) -> None:
        self._ensure_available()
        PWM.setMotorModel(*duties)
        self._last_duties = duties

    def _duties_for_action(self, action: str) -> tuple[int, int, int, int]:
        limits_cfg = self._config.get("limits", {})
        manual_cfg = self._config.get("manual_control", {})
        max_duty = int(limits_cfg.get("max_duty", 4095))
        drive_speed = _clamp_speed(int(manual_cfg.get("drive_speed", 1000)), max_duty)
        turn_speed = _clamp_speed(int(manual_cfg.get("turn_speed", 1200)), max_duty)

        mapping = {
            "forward": (drive_speed, drive_speed, drive_speed, drive_speed),
            "back": (-drive_speed, -drive_speed, -drive_speed, -drive_speed),
            "left": (-turn_speed, -turn_speed, turn_speed, turn_speed),
            "right": (turn_speed, turn_speed, -turn_speed, -turn_speed),
            "stop": (0, 0, 0, 0),
        }
        if action not in mapping:
            raise ValueError(f"Unsupported action: {action}")
        return mapping[action]

    def _start_vlm_locked(self) -> None:
        self._ensure_available()
        if self._vlm_thread and self._vlm_thread.is_alive():
            return

        self._append_log(
            level="INFO",
            mode="vlm",
            message="Starting VLM drive loop",
        )

        self._vlm_stop_event = threading.Event()
        self._vlm_thread = threading.Thread(
            target=self._vlm_loop,
            args=(self._vlm_stop_event,),
            daemon=True,
            name="vlm-drive-loop",
        )
        self._vlm_thread.start()

    def _stop_vlm_locked(self) -> None:
        thread = self._vlm_thread
        stop_event = self._vlm_stop_event
        self._vlm_thread = None
        self._vlm_stop_event = None

        if thread:
            self._append_log(
                level="INFO",
                mode="vlm",
                message="Stopping VLM drive loop",
            )

        if stop_event:
            stop_event.set()
        if thread:
            thread.join(timeout=3.0)

    def _vlm_loop(self, stop_event: threading.Event) -> None:
        vlm_cfg = self._config.get("vlm", {})
        limits_cfg = self._config.get("limits", {})
        max_duty = int(limits_cfg.get("max_duty", 4095))

        action_source = VLMActionSource(
            status_url=str(vlm_cfg.get("status_url", "http://127.0.0.1:8000/api/status")),
            poll_interval_sec=max(0.05, float(vlm_cfg.get("poll_interval_sec", 0.4))),
            timeout_sec=max(0.1, float(vlm_cfg.get("api_timeout_sec", 1.5))),
        )
        policy = MotionPolicy(
            base_speed=_clamp_speed(int(vlm_cfg.get("base_speed", 1000)), max_duty),
            slow_speed=_clamp_speed(int(vlm_cfg.get("slow_speed", 750)), max_duty),
            turn_speed=_clamp_speed(int(vlm_cfg.get("turn_speed", 1200)), max_duty),
            steer_phase_sec=max(0.05, float(vlm_cfg.get("steer_phase_sec", 0.8))),
            steer_cooldown_sec=max(0.0, float(vlm_cfg.get("steer_cooldown_sec", 1.2))),
            hard_stop_cm=max(1, int(self._config.get("avoidance", {}).get("stop_cm", 20))),
            caution_cm=max(
                max(1, int(self._config.get("avoidance", {}).get("stop_cm", 20))),
                int(self._config.get("avoidance", {}).get("caution_cm", 30)),
            ),
            stop_confirm_count=max(1, int(vlm_cfg.get("stop_confirm_count", 2))),
            recovery_stop_sec=max(0.0, float(vlm_cfg.get("recovery_stop_sec", 1.0))),
            recovery_probe_turn_sec=max(
                0.1, float(vlm_cfg.get("recovery_probe_turn_sec", 0.5))
            ),
            recovery_pause_sec=max(0.0, float(vlm_cfg.get("recovery_pause_sec", 2.0))),
            recovery_reverse_sec=max(0.0, float(vlm_cfg.get("recovery_reverse_sec", 1.5))),
            recovery_turn_sec=max(0.1, float(vlm_cfg.get("recovery_turn_sec", 0.6))),
            recovery_cooldown_sec=max(
                0.0, float(vlm_cfg.get("recovery_cooldown_sec", 2.0))
            ),
        )
        decision_engine = ActionDecisionEngine(policy=policy)

        stale_timeout = max(0.1, float(vlm_cfg.get("vlm_stale_timeout_sec", 1.2)))
        loop_interval = max(0.05, float(vlm_cfg.get("control_interval_sec", 0.1)))

        action_source.start()
        last_duties: tuple[int, int, int, int] | None = None
        last_source_err: str | None = None
        last_reason: str | None = None
        last_effective: str | None = None
        last_raw_action: str | None = None

        try:
            while not stop_event.is_set():
                now = time.monotonic()
                action, action_age, source_err = action_source.latest()
                distance_cm = self._read_distance_cm()

                stale_action = action_age is None or action_age > stale_timeout
                allow_recovery = not stale_action
                if stale_action:
                    action = VLMAction.STOP

                duties, reason, effective_action, detail = decision_engine.decide(
                    action=action,
                    distance_cm=distance_cm,
                    now_mono=now,
                    allow_recovery=allow_recovery,
                )

                raw_action_text = action.value if action else "none"
                effective_action_text = effective_action.value
                state_changed = (
                    duties != last_duties
                    or reason != last_reason
                    or effective_action_text != last_effective
                    or raw_action_text != last_raw_action
                )

                with self._lock:
                    if self._mode == "vlm" and duties != last_duties:
                        self._set_motor(duties)
                    if self._mode == "vlm" and state_changed:
                        self._append_log(
                            level="INFO",
                            mode="vlm",
                            message="VLM state update",
                            raw_action=raw_action_text,
                            effective_action=effective_action_text,
                            reason=reason,
                            duties=list(duties),
                            distance_cm=distance_cm,
                            detail=detail,
                        )

                last_duties = duties
                last_reason = reason
                last_effective = effective_action_text
                last_raw_action = raw_action_text

                if source_err and source_err != last_source_err:
                    logger.warning("VLM status source error: %s", source_err)
                    with self._lock:
                        self._append_log(
                            level="WARNING",
                            mode="vlm",
                            message="VLM status source error",
                            error=source_err,
                        )
                if not source_err and last_source_err:
                    with self._lock:
                        self._append_log(
                            level="INFO",
                            mode="vlm",
                            message="VLM status source recovered",
                        )
                last_source_err = source_err

                stop_event.wait(loop_interval)
        except Exception as exc:
            with self._lock:
                self._error = f"VLM loop failed: {exc}"
                self._append_log(
                    level="ERROR",
                    mode="vlm",
                    message="VLM loop crashed",
                    error=str(exc),
                )
            logger.exception("VLM loop crashed")
        finally:
            with self._lock:
                try:
                    self._set_motor((0, 0, 0, 0))
                except Exception:
                    pass
                self._append_log(
                    level="INFO",
                    mode="vlm",
                    message="VLM drive loop exited",
                )
            action_source.stop()

    def switch_mode(self, mode: str) -> dict[str, Any]:
        if mode not in ("interactive", "vlm"):
            raise ValueError("mode must be interactive or vlm")

        with self._lock:
            previous_mode = self._mode
            self._ensure_available()
            self._stop_vlm_locked()
            self._set_motor((0, 0, 0, 0))
            self._last_manual_action = "stop"

            self._mode = mode
            if mode == "vlm":
                self._start_vlm_locked()

            self._append_log(
                level="INFO",
                mode=mode,
                message="Drive mode switched",
                from_mode=previous_mode,
                to_mode=mode,
            )

            return self.status()

    def apply_manual_action(self, action: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_available()
            if self._mode != "interactive":
                raise RuntimeError("Manual action is only allowed in interactive mode")

            duties = self._duties_for_action(action)
            self._set_motor(duties)
            self._last_manual_action = action
            self._append_log(
                level="INFO",
                mode="interactive",
                message="Manual action applied",
                action=action,
                duties=list(duties),
            )
            return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "available": HARDWARE_AVAILABLE,
            "mode": self._mode,
            "vlm_running": bool(self._vlm_thread and self._vlm_thread.is_alive()),
            "last_manual_action": self._last_manual_action,
            "last_duties": list(self._last_duties),
            "error": self._error,
            "log_count": len(self._logs),
        }

    def shutdown(self) -> None:
        with self._lock:
            self._stop_vlm_locked()
            if HARDWARE_AVAILABLE and PWM is not None:
                try:
                    self._set_motor((0, 0, 0, 0))
                except Exception:
                    logger.exception("Failed to stop motors during shutdown")
