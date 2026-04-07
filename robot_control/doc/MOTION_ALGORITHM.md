# Robot Motion Algorithm

## Overview

This document summarizes the current motion-control pipeline used by the robot car in VLM mode.

Core implementation:

- `robot_control/script/vlm_action_controller.py`
- `robot_control/cli.py`
- `robot_control/config/cli_config.json`

The controller is a stateful decision engine that combines:

- VLM navigation output
- ultrasonic obstacle distance
- action debounce and cooldown
- staged probe behavior
- multi-step recovery behavior

Its goal is to keep the robot responsive to fresh VLM commands while still handling close-range obstacles safely.

## End-to-End Pipeline

### 1. VLM action source

The robot continuously polls the inference server `/api/status` endpoint and extracts the latest action from the returned JSON.

Typical actions are:

- `Move Forward`
- `Slow Down`
- `Stop`
- `Steer Left`
- `Steer Right`

The polling loop stores:

- latest parsed action
- latest update timestamp
- latest source error

If the action becomes stale for longer than `vlm_stale_timeout_sec`, the controller forces the effective action to `Stop`.

### 2. Main decision loop

Each control tick does the following:

1. Read the latest VLM action.
2. Read ultrasonic distance if enabled.
3. Check whether the VLM action is stale.
4. Run the action through the decision engine.
5. Convert the final decision into four wheel PWM duties.
6. Only send motor output if duties actually changed.

## Priority Order

At a high level, the controller handles motion in this order:

1. Probe interruption by a fresh non-stop VLM action
2. Post-recovery fallback decision
3. Post-probe follow-up decision
4. Active probe/recovery phase execution
5. Hard safety stop for very close obstacles
6. Normal VLM action processing
7. Sustained stop detection and recovery triggering

This ordering is important because some states are long-running staged behaviors and should finish or be explicitly interrupted.

## Normal VLM Motion Logic

### 1. Stop debounce

`Stop` commands are debounced using `stop_confirm_count`.

Behavior:

- If the number of consecutive `Stop` commands is still below the threshold, the controller keeps the last non-stop action.
- Once the threshold is reached, `Stop` becomes effective.

This avoids overreacting to a single noisy stop output from the VLM.

### 2. Steer cooldown

Repeated same-direction steer outputs are suppressed for a cooldown window.

Behavior:

- First `Steer Left` or `Steer Right` executes normally.
- During `steer_cooldown_sec`, the same steer direction is downgraded to `Move Forward`.
- The opposite steer direction is still allowed immediately.

This prevents the robot from over-committing to repeated identical turn commands from the VLM.

### 3. Near-distance forward constraint

If the ultrasonic distance is inside `caution_cm` and the effective action is `Move Forward`, the action is downgraded to `Slow Down`.

This lets the robot keep moving cautiously instead of blindly charging ahead.

### 4. Duty mapping

Final actions map to motor duties as follows:

- `Move Forward`: all wheels positive at base speed
- `Slow Down`: all wheels positive at reduced speed
- `Stop`: all wheels zero
- `Steer Left`: curved left duty profile
- `Steer Right`: curved right duty profile

Steer commands use a two-phase curve:

- phase 1: stronger bias into the desired direction
- phase 2: heading recovery bias

This produces smoother steering than a single fixed differential turn.

## Hard Safety Stop

If `distance_cm <= hard_stop_cm`, the controller enters hard safety handling immediately.

Behavior:

- steering phase state is reset
- current action is forced to `Stop`
- sustained stop time starts accumulating

If the sustained hard stop lasts long enough and recovery is allowed, the controller starts the staged probe sequence described below.

## Sustained Stop and Recovery Trigger

Even outside hard-stop distance, if the effective action stays `Stop` continuously for at least `recovery_stop_sec`, the controller treats this as being stuck and attempts recovery.

There are two main cases:

- hard-stop driven recovery
- sustained-stop driven recovery

Both cases use the same staged recovery framework below.

## Pre-Recovery Probe

Before entering the main reverse recovery, the robot first tries a lighter probe maneuver to see if it can find a route around the obstacle without backing up.

### Probe sequence

The current probe sequence is:

1. `probe_left`
2. `probe_pause`
3. `probe_right`
4. `probe_assess`

Detailed timing:

- left turn duration: `recovery_probe_turn_sec`
- pause duration: `recovery_pause_sec`
- right turn duration: `2 * recovery_probe_turn_sec`
- final assess duration: `recovery_pause_sec`

### Probe interruption

During `probe_pause` and `probe_assess`, a fresh non-stop VLM command can interrupt probe immediately.

Current behavior:

- if VLM outputs `Move Forward`, `Slow Down`, `Steer Left`, or `Steer Right`
- the probe state is cleared
- the new action is executed immediately

This keeps the robot responsive once the scene becomes passable again.

### Probe result

After the full probe sequence ends:

- if VLM is still `Stop`, the robot enters normal recovery
- otherwise probe is considered successful and control returns to the normal VLM loop

## Normal Recovery

If probe fails, the robot enters the main recovery sequence.

### Normal recovery sequence

The current normal recovery order is:

1. `reverse`
2. `left_turn`
3. `turn_pause`
4. `right_turn`
5. `settle`

Detailed timing:

- reverse duration: `recovery_reverse_sec`
- left turn duration: `recovery_turn_sec`
- middle pause duration: `recovery_pause_sec`
- right turn duration: `recovery_turn_sec`
- final settle duration: `recovery_pause_sec`

Notes:

- reverse uses `slow_speed`
- left/right recovery turns use `turn_speed`
- recovery selector state is still tracked for future variation, but the current recovery path is explicitly `left -> pause -> right`

### Recovery cooldown

After the normal recovery sequence completes, the controller enters a cooldown window of `recovery_cooldown_sec`.

This prevents the controller from immediately retriggering recovery every single loop tick.

## Final Fallback Turn

If the full normal recovery finishes and the VLM still reports `Stop`, the controller performs one more stronger fallback maneuver:

1. `final_right`

Detailed timing:

- final right duration: `2 * recovery_turn_sec`

This is a last attempt to rotate the robot into a different heading before giving up and waiting.

## Current Full Obstacle-Handling Flow

The current staged obstacle logic is:

1. Detect hard stop or sustained stop.
2. Start pre-recovery probe:
   - left turn
   - pause
   - right turn
   - pause
3. If a fresh non-stop VLM action appears during probe pause windows, interrupt probe and execute it immediately.
4. If probe still ends with `Stop`, run normal recovery:
   - reverse
   - left turn
   - pause
   - right turn
   - pause
5. If recovery still ends with `Stop`, run final fallback:
   - right turn for `2x`
6. If still blocked afterward, the robot remains stopped until a new valid action or a later recovery opportunity appears.

## Key Configuration Parameters

These values live under `vlm` in `robot_control/cli_config.json`.

- `base_speed`: forward speed
- `slow_speed`: cautious forward speed and reverse recovery speed
- `turn_speed`: turn and probe speed
- `steer_phase_sec`: phase length for normal steering curve
- `steer_cooldown_sec`: repeated same-direction steer suppression window
- `vlm_stale_timeout_sec`: stale VLM cutoff
- `stop_confirm_count`: stop debounce threshold
- `recovery_stop_sec`: sustained stop time needed before probe/recovery
- `recovery_probe_turn_sec`: base left probe turn duration
- `recovery_pause_sec`: shared pause duration used between probe/recovery steps
- `recovery_reverse_sec`: reverse duration in normal recovery
- `recovery_turn_sec`: left and right turn duration in normal recovery
- `recovery_cooldown_sec`: cooldown after recovery completes
- `hard_stop_cm`: immediate obstacle stop threshold
- `caution_cm`: slow-down threshold

## Practical Interpretation

You can think of the algorithm as four nested layers:

1. VLM says where the robot wants to go.
2. Ultrasonic distance limits unsafe forward motion.
3. Probe tries the lightest possible reorientation before backing up.
4. Recovery escalates to reverse + turns, and finally a stronger right turn fallback if still blocked.

This structure is designed to balance:

- safety
- responsiveness to fresh VLM judgments
- resistance to noisy repeated actions
- ability to escape local deadlocks

## Suggested Future Extensions

Possible next improvements:

- use actual VLM left/right preference to bias probe order
- allow probe interruption during active turning, not only pause windows
- persist failure counters and escalate differently after repeated blocked cycles
- log state transitions to a dedicated markdown or CSV trace for tuning
