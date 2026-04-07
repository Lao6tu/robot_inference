#!/usr/bin/env python3
"""
VLM Action Motion Controller
===========================
Non-blocking motion controller that consumes latest VLM inference JSON from
the inference dashboard API (/api/status), extracts a navigation action, and
combines it with ultrasonic distance constraints.

Priority order:
1) Hard safety: very close obstacle -> forced stop
2) Near constraint: close obstacle -> limit speed / block forward rush
3) VLM direction: forward / slow down / stop / steer left / steer right
4) Recovery: sustained stop under near-distance -> perform scan/u-turn
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from urllib import error, request


DutyTuple = tuple[int, int, int, int]


def _duties_to_label(duties: DutyTuple) -> str:
    d1, d2, d3, d4 = duties
    if duties == (0, 0, 0, 0):
        return "STOP"
    if d1 > 0 and d2 > 0 and d3 > 0 and d4 > 0:
        return "FORWARD"
    if d1 < 0 and d2 < 0 and d3 > 0 and d4 > 0:
        return "STEER_LEFT"
    if d1 > 0 and d2 > 0 and d3 < 0 and d4 < 0:
        return "STEER_RIGHT"
    return "RAW"


class VLMAction(str, Enum):
    MOVE_FORWARD = "Move Forward"
    SLOW_DOWN = "Slow Down"
    STOP = "Stop"
    STEER_RIGHT = "Steer Right"
    STEER_LEFT = "Steer Left"


def _normalize_action(value: Any) -> VLMAction | None:
    if value is None:
        return None

    text = str(value).strip().lower()
    if not text:
        return None

    compact = text.replace("_", " ").replace("-", " ")

    if any(token in compact for token in ("steer right", "turn right", "go right")):
        return VLMAction.STEER_RIGHT
    if any(token in compact for token in ("steer left", "turn left", "go left")):
        return VLMAction.STEER_LEFT
    if any(token in compact for token in ("slow down", "slow", "caution")):
        return VLMAction.SLOW_DOWN
    if any(token in compact for token in ("move forward", "go forward", "forward")):
        return VLMAction.MOVE_FORWARD
    if any(token in compact for token in ("stop", "halt", "brake")):
        return VLMAction.STOP

    return None


def _iter_string_values(obj: Any):
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_string_values(value)
        return
    if isinstance(obj, list):
        for value in obj:
            yield from _iter_string_values(value)


def extract_action_from_result(result: dict[str, Any]) -> VLMAction | None:
    candidate_keys = (
        "action",
        "action_advice",
        "nav_action",
        "motion_action",
        "decision",
        "command",
        "next_action",
    )
    for key in candidate_keys:
        action = _normalize_action(result.get(key))
        if action:
            return action

    for text in _iter_string_values(result):
        action = _normalize_action(text)
        if action:
            return action

    return None


class VLMActionSource:
    """Polls inference /api/status in background and keeps latest parsed action."""

    def __init__(
        self,
        status_url: str,
        poll_interval_sec: float = 0.25,
        timeout_sec: float = 1.0,
    ) -> None:
        self._status_url = status_url
        self._poll_interval_sec = max(0.05, poll_interval_sec)
        self._timeout_sec = max(0.1, timeout_sec)

        self._lock = threading.Lock()
        self._latest_action: VLMAction | None = None
        self._latest_update_mono: float | None = None
        self._latest_error: str | None = None

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def latest(self) -> tuple[VLMAction | None, float | None, str | None]:
        with self._lock:
            age = None
            if self._latest_update_mono is not None:
                age = max(0.0, time.monotonic() - self._latest_update_mono)
            return self._latest_action, age, self._latest_error

    def _run(self) -> None:
        while self._running:
            t0 = time.monotonic()
            try:
                action = self._fetch_action_once()
                with self._lock:
                    if action is not None:
                        self._latest_action = action
                    self._latest_update_mono = time.monotonic()
                    self._latest_error = None
            except Exception as exc:
                with self._lock:
                    self._latest_error = str(exc)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, self._poll_interval_sec - elapsed))

    def _fetch_action_once(self) -> VLMAction | None:
        req = request.Request(
            self._status_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=self._timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Inference status request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON from inference status API: {exc}") from exc

        result: dict[str, Any] | None = None

        if isinstance(payload, dict):
            latest = payload.get("latest_result")
            if isinstance(latest, dict):
                result = latest
            else:
                result = payload

        if not isinstance(result, dict):
            raise RuntimeError("Inference status payload does not contain latest_result")

        return extract_action_from_result(result)


@dataclass
class MotionPolicy:
    base_speed: int = 1400
    slow_speed: int = 900
    turn_speed: int = 1500
    steer_phase_sec: float = 0.35
    steer_cooldown_sec: float = 1.0
    hard_stop_cm: int = 15
    caution_cm: int = 28
    stop_confirm_count: int = 1
    recovery_stop_sec: float = 0.2
    recovery_probe_turn_sec: float = 0.5
    recovery_pause_sec: float = 0.5
    recovery_reverse_sec: float = 0.3
    recovery_turn_sec: float = 0.8
    recovery_cooldown_sec: float = 1.5


class ActionDecisionEngine:
    """Stateful policy engine for action debounce, safety constraints, and recovery."""

    def __init__(self, policy: MotionPolicy) -> None:
        self.policy = policy
        self._last_action = VLMAction.SLOW_DOWN
        self._last_non_stop_action = VLMAction.SLOW_DOWN
        self._pending_stop_count = 0
        self._sustained_stop_started_at: float | None = None
        self._steer_phase_action: VLMAction | None = None
        self._steer_phase_started_at = 0.0
        self._last_steer_action: VLMAction | None = None
        self._steer_cooldown_until = 0.0

        self._probe_left_until = 0.0
        self._probe_pause_until = 0.0
        self._probe_right_until = 0.0
        self._probe_assess_until = 0.0
        self._probe_source: str | None = None

        self._recovery_reverse_until = 0.0
        self._recovery_left_turn_until = 0.0
        self._recovery_mid_pause_until = 0.0
        self._recovery_right_turn_until = 0.0
        self._recovery_settle_until = 0.0
        self._recovery_final_right_until = 0.0
        self._recovery_cooldown_until = 0.0
        self._recovery_reverse_duties: DutyTuple = (0, 0, 0, 0)
        self._recovery_left_turn_duties: DutyTuple = (0, 0, 0, 0)
        self._recovery_right_turn_duties: DutyTuple = (0, 0, 0, 0)
        self._recovery_source: str | None = None

    def decide(
        self,
        action: VLMAction | None,
        distance_cm: int | None,
        now_mono: float,
        allow_recovery: bool = True,
    ) -> tuple[DutyTuple, str, VLMAction, str]:
        probe_interrupt = self._interrupt_probe_on_new_action(action, now_mono)
        if probe_interrupt is not None:
            return probe_interrupt

        recovery_followup = self._continue_post_recovery_fallback(
            action=action,
            now_mono=now_mono,
            allow_recovery=allow_recovery,
        )
        if recovery_followup is not None:
            return recovery_followup
        self._clear_completed_recovery(now_mono)

        recovery_followup = self._continue_post_probe_recovery(
            action=action,
            now_mono=now_mono,
            allow_recovery=allow_recovery,
        )
        if recovery_followup is not None:
            return recovery_followup

        recovery_duties, recovery_detail = self._get_recovery_phase(now_mono)
        if recovery_duties is not None:
            return recovery_duties, "recovery_u_turn", self._last_action, recovery_detail

        if distance_cm is not None and distance_cm <= self.policy.hard_stop_cm:
            self._reset_steer_phase()
            self._last_action = VLMAction.STOP
            self._pending_stop_count = 0
            stop_duration = self._mark_sustained_stop(now_mono)
            detail_parts = [
                f"distance={distance_cm}cm <= hard_stop_cm={self.policy.hard_stop_cm}cm",
                "hard_stop_duration="
                f"{stop_duration:.2f}/{self.policy.recovery_stop_sec:.2f}s",
            ]
            if (
                allow_recovery
                and stop_duration >= self.policy.recovery_stop_sec
                and now_mono >= self._recovery_cooldown_until
            ):
                detail_parts.append(self._start_probe_recovery(now_mono, from_hard_stop=True))
                return (
                    self._probe_turn_duties("left"),
                    "hard_stop_probe_triggered",
                    VLMAction.STOP,
                    "; ".join(detail_parts),
                )
            if now_mono < self._recovery_cooldown_until:
                cooldown_left = self._recovery_cooldown_until - now_mono
                detail_parts.append(f"recovery_cooldown={cooldown_left:.2f}s")
            return (0, 0, 0, 0), "hard_safety_stop", VLMAction.STOP, "; ".join(detail_parts)

        effective, debounce_detail = self._resolve_action_with_stop_debounce(action)
        effective, steer_cooldown_detail = self._apply_steer_cooldown(
            effective,
            now_mono,
        )

        near_distance = (
            distance_cm is not None and distance_cm <= self.policy.caution_cm
        )

        reason = "vlm_action"
        detail_parts: list[str] = []
        if action is not None:
            detail_parts.append(f"vlm_action={action.value}")
        if debounce_detail:
            detail_parts.append(debounce_detail)
        if steer_cooldown_detail:
            detail_parts.append(steer_cooldown_detail)

        if near_distance and effective == VLMAction.MOVE_FORWARD:
            effective = VLMAction.SLOW_DOWN
            reason = "near_constraint_slow_down"
            detail_parts.append(
                f"distance={distance_cm}cm <= caution_cm={self.policy.caution_cm}cm"
            )

        if effective == VLMAction.STOP:
            stop_duration = self._mark_sustained_stop(now_mono)
            detail_parts.append(
                "sustained_stop_duration="
                f"{stop_duration:.2f}/{self.policy.recovery_stop_sec:.2f}s"
            )
            if (
                allow_recovery
                and stop_duration >= self.policy.recovery_stop_sec
                and now_mono >= self._recovery_cooldown_until
            ):
                detail_parts.append(self._start_probe_recovery(now_mono, from_hard_stop=False))
                return (
                    self._probe_turn_duties("left"),
                    "probe_triggered",
                    VLMAction.STOP,
                    "; ".join(detail_parts),
                )
        else:
            self._reset_sustained_stop()

        duties, motion_detail = self._duties_for_action(
            effective,
            near_distance,
            now_mono,
        )
        self._remember_effective_action(effective, now_mono)

        if motion_detail:
            detail_parts.append(motion_detail)
        detail_parts.append(f"effective={effective.value}")
        return duties, reason, effective, "; ".join(detail_parts)

    def _resolve_action_with_stop_debounce(
        self,
        action: VLMAction | None,
    ) -> tuple[VLMAction, str | None]:
        if action is None:
            return self._last_action, "no_new_vlm_action_hold_last"

        if action == VLMAction.STOP:
            self._pending_stop_count += 1
            if self._pending_stop_count < self.policy.stop_confirm_count:
                detail = (
                    "stop_debounce_ignore "
                    f"{self._pending_stop_count}/{self.policy.stop_confirm_count}"
                )
                return self._last_non_stop_action, detail
            detail = (
                "stop_debounce_confirmed "
                f"{self._pending_stop_count}/{self.policy.stop_confirm_count}"
            )
            return VLMAction.STOP, detail

        reset_detail = None
        if self._pending_stop_count > 0:
            reset_detail = "stop_debounce_reset"
        self._pending_stop_count = 0
        return action, reset_detail

    def _apply_steer_cooldown(
        self,
        action: VLMAction,
        now_mono: float,
    ) -> tuple[VLMAction, str | None]:
        if action not in (VLMAction.STEER_LEFT, VLMAction.STEER_RIGHT):
            return action, None

        cooldown_sec = max(0.0, self.policy.steer_cooldown_sec)
        if cooldown_sec <= 0.0:
            return action, None

        if action != self._last_steer_action:
            return action, None

        if now_mono >= self._steer_cooldown_until:
            return action, None

        remaining = max(0.0, self._steer_cooldown_until - now_mono)
        self._reset_steer_phase()
        return VLMAction.MOVE_FORWARD, (
            "steer_cooldown_active "
            f"repeat={action.value} remaining={remaining:.2f}s "
            "fallback=Move Forward"
        )

    def _start_recovery(self, now_mono: float, from_hard_stop: bool) -> str:
        self._reset_steer_phase()
        self._clear_probe_recovery()

        reverse_speed = max(0, min(4095, self.policy.slow_speed))
        self._recovery_left_turn_duties = self._probe_turn_duties("left")
        self._recovery_right_turn_duties = self._probe_turn_duties("right")

        self._recovery_reverse_duties = (
            -reverse_speed,
            -reverse_speed,
            -reverse_speed,
            -reverse_speed,
        )

        reverse_sec = max(0.0, self.policy.recovery_reverse_sec)
        turn_sec = max(0.1, self.policy.recovery_turn_sec)
        pause_sec = max(0.0, self.policy.recovery_pause_sec)
        self._recovery_reverse_until = now_mono + reverse_sec
        self._recovery_left_turn_until = self._recovery_reverse_until + turn_sec
        self._recovery_mid_pause_until = self._recovery_left_turn_until + pause_sec
        self._recovery_right_turn_until = self._recovery_mid_pause_until + turn_sec
        self._recovery_settle_until = self._recovery_right_turn_until + pause_sec

        self._recovery_source = "hard_stop" if from_hard_stop else "sustained_stop"
        self._last_action = VLMAction.STOP

        self._recovery_cooldown_until = (
            self._recovery_settle_until + max(0.0, self.policy.recovery_cooldown_sec)
        )
        self._reset_sustained_stop()
        self._pending_stop_count = 0

        source = self._recovery_source
        return (
            f"triggered_recovery source={source} "
            f"reverse_sec={reverse_sec:.2f} left_sec={turn_sec:.2f} "
            f"pause_sec={pause_sec:.2f} right_sec={turn_sec:.2f} "
            f"final_pause_sec={pause_sec:.2f}"
        )

    def _start_probe_recovery(self, now_mono: float, from_hard_stop: bool) -> str:
        self._reset_steer_phase()
        probe_left_sec = max(0.1, self.policy.recovery_probe_turn_sec)
        probe_right_sec = probe_left_sec * 2.0
        probe_pause_sec = max(0.0, self.policy.recovery_pause_sec)
        self._probe_left_until = now_mono + probe_left_sec
        self._probe_pause_until = self._probe_left_until + probe_pause_sec
        self._probe_right_until = self._probe_pause_until + probe_right_sec
        self._probe_assess_until = self._probe_right_until + probe_pause_sec
        self._probe_source = "hard_stop" if from_hard_stop else "sustained_stop"
        self._last_action = VLMAction.STOP
        self._reset_sustained_stop()
        self._pending_stop_count = 0
        return (
            f"triggered_probe_recovery source={self._probe_source} "
            f"left_sec={probe_left_sec:.2f} pause_sec={probe_pause_sec:.2f} "
            f"right_sec={probe_right_sec:.2f} assess_sec={probe_pause_sec:.2f}"
        )

    def _continue_post_probe_recovery(
        self,
        action: VLMAction | None,
        now_mono: float,
        allow_recovery: bool,
    ) -> tuple[DutyTuple, str, VLMAction, str] | None:
        if not self._probe_pending_followup(now_mono):
            return None

        source = self._probe_source or "unknown"
        if allow_recovery and action == VLMAction.STOP:
            detail = (
                f"probe_complete source={source}; "
                f"{self._start_recovery(now_mono, from_hard_stop=source == 'hard_stop')}"
            )
            return (
                self._recovery_reverse_duties,
                "probe_failed_recovery_triggered",
                VLMAction.STOP,
                detail,
            )

        detail = (
            f"probe_complete source={source}; "
            f"probe_cleared next_action={action.value if action else 'none'}"
        )
        self._clear_probe_recovery()
        return None if action is not None else (
            (0, 0, 0, 0),
            "probe_complete_waiting_action",
            VLMAction.STOP,
            detail,
        )

    def _continue_post_recovery_fallback(
        self,
        action: VLMAction | None,
        now_mono: float,
        allow_recovery: bool,
    ) -> tuple[DutyTuple, str, VLMAction, str] | None:
        if not self._recovery_pending_fallback(now_mono):
            return None

        source = self._recovery_source or "unknown"
        if allow_recovery and action == VLMAction.STOP:
            detail = self._start_final_right_fallback(now_mono, source)
            return (
                self._probe_turn_duties("right"),
                "recovery_failed_final_right_triggered",
                VLMAction.STOP,
                detail,
            )

        self._clear_recovery_tracking()
        if action is not None:
            return None
        return (
            (0, 0, 0, 0),
            "recovery_complete_waiting_action",
            VLMAction.STOP,
            f"recovery_complete source={source}; next_action=none",
        )

    def _interrupt_probe_on_new_action(
        self,
        action: VLMAction | None,
        now_mono: float,
    ) -> tuple[DutyTuple, str, VLMAction, str] | None:
        if action is None or action == VLMAction.STOP or self._probe_source is None:
            return None

        phase = self._probe_interrupt_phase(now_mono)
        if phase is None:
            return None

        source = self._probe_source
        self._clear_probe_recovery()
        self._reset_sustained_stop()
        self._pending_stop_count = 0

        duties, motion_detail = self._duties_for_action(
            action,
            near_distance=False,
            now_mono=now_mono,
        )
        self._remember_effective_action(action, now_mono)

        detail_parts = [
            f"probe_interrupted source={source} phase={phase}",
            f"vlm_action={action.value}",
        ]
        if motion_detail:
            detail_parts.append(motion_detail)
        detail_parts.append(f"effective={action.value}")
        return duties, "probe_interrupted_by_vlm_action", action, "; ".join(detail_parts)

    def _probe_pending_followup(self, now_mono: float) -> bool:
        return (
            self._probe_source is not None
            and self._probe_left_until > 0.0
            and now_mono >= self._probe_assess_until
        )

    def _recovery_pending_fallback(self, now_mono: float) -> bool:
        return (
            self._recovery_source is not None
            and self._recovery_settle_until > 0.0
            and self._recovery_final_right_until == 0.0
            and now_mono >= self._recovery_settle_until
        )

    def _clear_probe_recovery(self) -> None:
        self._probe_left_until = 0.0
        self._probe_pause_until = 0.0
        self._probe_right_until = 0.0
        self._probe_assess_until = 0.0
        self._probe_source = None

    def _clear_recovery_tracking(self) -> None:
        self._recovery_reverse_until = 0.0
        self._recovery_left_turn_until = 0.0
        self._recovery_mid_pause_until = 0.0
        self._recovery_right_turn_until = 0.0
        self._recovery_settle_until = 0.0
        self._recovery_final_right_until = 0.0
        self._recovery_reverse_duties = (0, 0, 0, 0)
        self._recovery_left_turn_duties = (0, 0, 0, 0)
        self._recovery_right_turn_duties = (0, 0, 0, 0)
        self._recovery_source = None

    def _start_final_right_fallback(self, now_mono: float, source: str) -> str:
        final_right_sec = max(0.1, self.policy.recovery_turn_sec * 2.0)
        self._recovery_final_right_until = now_mono + final_right_sec
        return (
            f"recovery_complete source={source}; "
            f"triggered_final_right right_sec={final_right_sec:.2f}"
        )

    def _clear_completed_recovery(self, now_mono: float) -> None:
        if (
            self._recovery_source is not None
            and self._recovery_final_right_until > 0.0
            and now_mono >= self._recovery_final_right_until
        ):
            self._clear_recovery_tracking()

    def _probe_interrupt_phase(self, now_mono: float) -> str | None:
        if self._probe_left_until > 0.0 and now_mono < self._probe_pause_until:
            return "probe_pause"
        if self._probe_right_until > 0.0 and now_mono < self._probe_assess_until:
            return "probe_assess"
        return None

    def _remember_effective_action(self, action: VLMAction, now_mono: float) -> None:
        self._last_action = action
        if action != VLMAction.STOP:
            self._last_non_stop_action = action
        if action in (VLMAction.STEER_LEFT, VLMAction.STEER_RIGHT):
            self._last_steer_action = action
            self._steer_cooldown_until = (
                now_mono + max(0.0, self.policy.steer_cooldown_sec)
            )

    def _probe_turn_duties(self, direction: str) -> DutyTuple:
        turn_speed = max(0, min(4095, self.policy.turn_speed))
        if direction == "left":
            return (-turn_speed, -turn_speed, turn_speed, turn_speed)
        return (turn_speed, turn_speed, -turn_speed, -turn_speed)

    def _get_recovery_phase(self, now_mono: float) -> tuple[DutyTuple | None, str]:
        if now_mono < self._probe_left_until:
            remaining = max(0.0, self._probe_left_until - now_mono)
            detail = (
                "recovery_phase=probe_left "
                f"remaining={remaining:.2f}s "
                f"source={self._probe_source}"
            )
            return self._probe_turn_duties("left"), detail

        if self._probe_left_until > 0.0 and now_mono < self._probe_pause_until:
            remaining = max(0.0, self._probe_pause_until - now_mono)
            detail = (
                "recovery_phase=probe_pause "
                f"remaining={remaining:.2f}s "
                f"source={self._probe_source}"
            )
            return (0, 0, 0, 0), detail

        if self._probe_left_until > 0.0 and now_mono < self._probe_right_until:
            remaining = max(0.0, self._probe_right_until - now_mono)
            detail = (
                "recovery_phase=probe_right "
                f"remaining={remaining:.2f}s "
                f"source={self._probe_source}"
            )
            return self._probe_turn_duties("right"), detail

        if self._probe_right_until > 0.0 and now_mono < self._probe_assess_until:
            remaining = max(0.0, self._probe_assess_until - now_mono)
            detail = (
                "recovery_phase=probe_assess "
                f"remaining={remaining:.2f}s "
                f"source={self._probe_source}"
            )
            return (0, 0, 0, 0), detail

        if now_mono < self._recovery_reverse_until:
            remaining = max(0.0, self._recovery_reverse_until - now_mono)
            detail = (
                "recovery_phase=reverse "
                f"remaining={remaining:.2f}s"
            )
            return self._recovery_reverse_duties, detail

        if now_mono < self._recovery_left_turn_until:
            remaining = max(0.0, self._recovery_left_turn_until - now_mono)
            detail = (
                "recovery_phase=left_turn "
                f"remaining={remaining:.2f}s"
            )
            return self._recovery_left_turn_duties, detail

        if now_mono < self._recovery_mid_pause_until:
            remaining = max(0.0, self._recovery_mid_pause_until - now_mono)
            detail = (
                "recovery_phase=turn_pause "
                f"remaining={remaining:.2f}s"
            )
            return (0, 0, 0, 0), detail

        if now_mono < self._recovery_right_turn_until:
            remaining = max(0.0, self._recovery_right_turn_until - now_mono)
            detail = (
                "recovery_phase=right_turn "
                f"remaining={remaining:.2f}s"
            )
            return self._recovery_right_turn_duties, detail

        if now_mono < self._recovery_settle_until:
            remaining = max(0.0, self._recovery_settle_until - now_mono)
            detail = (
                "recovery_phase=settle "
                f"remaining={remaining:.2f}s"
            )
            return (0, 0, 0, 0), detail

        if now_mono < self._recovery_final_right_until:
            remaining = max(0.0, self._recovery_final_right_until - now_mono)
            detail = (
                "recovery_phase=final_right "
                f"remaining={remaining:.2f}s "
                f"source={self._recovery_source}"
            )
            return self._probe_turn_duties("right"), detail

        return None, ""

    def _mark_sustained_stop(self, now_mono: float) -> float:
        if self._sustained_stop_started_at is None:
            self._sustained_stop_started_at = now_mono
            return 0.0
        return max(0.0, now_mono - self._sustained_stop_started_at)

    def _reset_sustained_stop(self) -> None:
        self._sustained_stop_started_at = None

    def _reset_steer_phase(self) -> None:
        self._steer_phase_action = None
        self._steer_phase_started_at = 0.0

    def _duties_for_action(
        self,
        action: VLMAction,
        near_distance: bool,
        now_mono: float,
    ) -> tuple[DutyTuple, str | None]:
        slow_speed = max(0, min(4095, self.policy.slow_speed))
        base_speed = max(0, min(4095, self.policy.base_speed))
        turn_speed = max(0, min(4095, self.policy.turn_speed))

        if near_distance:
            base_speed = min(base_speed, slow_speed)

        if action == VLMAction.MOVE_FORWARD:
            self._reset_steer_phase()
            return (base_speed, base_speed, base_speed, base_speed), None
        if action == VLMAction.SLOW_DOWN:
            self._reset_steer_phase()
            return (slow_speed, slow_speed, slow_speed, slow_speed), None
        if action == VLMAction.STEER_LEFT:
            return self._steer_adjust_duties(action, base_speed, turn_speed, now_mono)
        if action == VLMAction.STEER_RIGHT:
            return self._steer_adjust_duties(action, base_speed, turn_speed, now_mono)

        self._reset_steer_phase()
        return (0, 0, 0, 0), None

    def _steer_adjust_duties(
        self,
        action: VLMAction,
        base_speed: int,
        turn_speed: int,
        now_mono: float,
    ) -> tuple[DutyTuple, str]:
        phase_sec = max(0.05, float(self.policy.steer_phase_sec))
        if self._steer_phase_action != action:
            self._steer_phase_action = action
            self._steer_phase_started_at = now_mono

        elapsed = max(0.0, now_mono - self._steer_phase_started_at)
        cycle_sec = 2.0 * phase_sec
        phase_1 = (elapsed % cycle_sec) < phase_sec

        forward_speed = max(base_speed, int(turn_speed * 0.65))
        forward_speed = max(0, min(4095, forward_speed))
        major_delta = max(40, int(turn_speed * 0.35))
        minor_delta = max(25, int(turn_speed * 0.2))

        def _clamp(speed: int) -> int:
            return max(0, min(4095, speed))

        if action == VLMAction.STEER_LEFT:
            if phase_1:
                left_speed = _clamp(forward_speed - major_delta)
                right_speed = _clamp(forward_speed + major_delta)
                phase_name = "phase1_left_bias"
            else:
                left_speed = _clamp(forward_speed + minor_delta)
                right_speed = _clamp(forward_speed - minor_delta)
                phase_name = "phase2_heading_recover_right"
        else:
            if phase_1:
                left_speed = _clamp(forward_speed + major_delta)
                right_speed = _clamp(forward_speed - major_delta)
                phase_name = "phase1_right_bias"
            else:
                left_speed = _clamp(forward_speed - minor_delta)
                right_speed = _clamp(forward_speed + minor_delta)
                phase_name = "phase2_heading_recover_left"

        duties = (left_speed, left_speed, right_speed, right_speed)
        detail = f"steer_curve={phase_name} phase_sec={phase_sec:.2f} heading~stable"
        return duties, detail


class VLMMotionController:
    """Runs control loop without blocking on VLM API latency."""

    def __init__(
        self,
        *,
        action_source: VLMActionSource,
        decision_engine: ActionDecisionEngine,
        motor_setter: Callable[[int, int, int, int], None],
        distance_reader: Optional[Callable[[], int | None]] = None,
        loop_interval_sec: float = 0.1,
        stale_action_timeout_sec: float = 1.0,
    ) -> None:
        self._action_source = action_source
        self._decision_engine = decision_engine
        self._motor_setter = motor_setter
        self._distance_reader = distance_reader
        self._loop_interval_sec = max(0.05, loop_interval_sec)
        self._stale_action_timeout_sec = max(0.1, stale_action_timeout_sec)

    def run_until_interrupt(self) -> None:
        self._action_source.start()
        print("VLM control loop started. Press Ctrl+C to stop.")

        last_duties: DutyTuple | None = None
        last_rule: str | None = None
        last_effective: VLMAction | None = None
        last_raw_action: VLMAction | None = None
        last_source_err: str | None = None
        last_report_at = 0.0

        try:
            while True:
                now = time.monotonic()
                action, action_age, source_err = self._action_source.latest()
                distance_cm = self._distance_reader() if self._distance_reader else None
                stale_action = (
                    action_age is None or action_age > self._stale_action_timeout_sec
                )
                allow_recovery = True
                if stale_action:
                    action = VLMAction.STOP
                    allow_recovery = False

                duties, reason, effective_action, detail = self._decision_engine.decide(
                    action=action,
                    distance_cm=distance_cm,
                    now_mono=now,
                    allow_recovery=allow_recovery,
                )
                if stale_action:
                    stale_detail = (
                        "vlm_stale_timeout="
                        f"{self._stale_action_timeout_sec:.2f}s "
                        f"action_age={'n/a' if action_age is None else f'{action_age:.2f}s'}"
                    )
                    reason = "vlm_stale_failsafe_stop"
                    detail = stale_detail if not detail else f"{detail}; {stale_detail}"

                if duties != last_duties:
                    self._motor_setter(*duties)
                state_changed = (
                    duties != last_duties
                    or reason != last_rule
                    or effective_action != last_effective
                    or action != last_raw_action
                )
                if state_changed:
                    print(
                        "[EVENT]",
                        f"rule={reason}",
                        f"raw={action.value if action else 'none'}",
                        f"effective={effective_action.value}",
                        f"state={_duties_to_label(duties)}",
                        f"duties={duties}",
                        f"distance_cm={distance_cm}",
                        f"detail={detail or 'n/a'}",
                    )

                if source_err != last_source_err:
                    print(
                        "[SOURCE]",
                        "status_api_error=",
                        source_err if source_err else "none",
                    )

                last_duties = duties
                last_rule = reason
                last_effective = effective_action
                last_raw_action = action
                last_source_err = source_err

                if now - last_report_at >= 1.0:
                    age_text = "n/a" if action_age is None else f"{action_age:.2f}s"
                    print(
                        "[HEARTBEAT]",
                        f"raw={action.value if action else 'none'}",
                        f"effective={effective_action.value}",
                        f"rule={reason}",
                        f"state={_duties_to_label(duties)}",
                        f"distance_cm={distance_cm}",
                        f"action_age={age_text}",
                        f"source_err={source_err}",
                    )
                    last_report_at = now

                time.sleep(self._loop_interval_sec)
        finally:
            self._motor_setter(0, 0, 0, 0)
            self._action_source.stop()
