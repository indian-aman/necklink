"""Presynaptic-Shunting Box-Constrained Fault-Tolerant VNC Allocator (`ShuntingThrusterCord`).

Translates 3-DOF `[Fx, Fy, Tz]` or 4-DOF `[Fz, Tx, Ty, Tz]` body wrench commands
(or 4-byte `DescendingFrame` cervical packets) across `4` to `8` redundant actuators:
  - Vectored marine USVs and ROVs (symmetric or asymmetric bounds like BlueRobotics `T200`
    `-40.0 N .. +52.5 N` with ESC deadzone jump)
  - Unidirectional `[0, u_max]` 6-rotor hexacopters and 8-rotor octocopters surviving
    complete motor loss (`health -> 0`) via active-set clamping and attitude-over-yaw priority

Biological & control mechanisms:
  1. Online proprioceptive load monitor compares expected actuator effort against load-cell
     thrust, sensorless ESC `RPM + Current` (`ESCObserver`), or IMU hull-wrench residuals
     to estimate actuator health `h_i in [0, 1]` within milliseconds.
  2. Fast presynaptic shunting inhibition (`s_i -> 0`) suppresses drive to severely degraded
     actuators (`h_i <= shunt_floor`), preventing ESC thermal burnout and entanglement.
  3. Surviving actuators rebalance the wrench via iterative active-set box-constrained
     redistribution over `[u_min_i, u_max_i]`, prioritizing `[Fz, Tx, Ty]` over `Tz` (4-DOF)
     or `[Fy, Tz]` over `Fx` (3-DOF) when actuator bounds saturate.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np

from .cervical import DescendingFrame, decode_descending_4b
from .esc_observer import ESCObserver, ESCObserverResult


@dataclass
class AllocationResult:
    """Single-step output of `ShuntingThrusterCord`."""

    commands: np.ndarray
    health: np.ndarray
    shunt_gain: np.ndarray
    effective_weights: np.ndarray
    predicted_wrench: np.ndarray
    isolated_mask: np.ndarray
    saturated_mask: np.ndarray | None = None
    esc_result: ESCObserverResult | None = None

    @property
    def esc_observation(self) -> ESCObserverResult | None:
        """Alias for `esc_result`."""
        return self.esc_result


class ShuntingThrusterCord:
    """Box-constrained fault-tolerant 3-DOF / 4-DOF wrench allocator for `4`..`8` actuators.

    Provenance (`PROVENANCE = "engineering"`):
      Bounded `[u_min_i, u_max_i]` active-set primal-dual thrust/rotor allocation, ESC deadband
      jumping, and attitude-over-yaw priority are non-biological control-allocation engineering
      for marine thrusters and aerial multicopter rotors (not *Drosophila* connectome weights).
    """

    PROVENANCE: str = "engineering"

    def __init__(
        self,
        wrench_matrix: np.ndarray,
        *,
        u_max: float | np.ndarray = 35.0,
        u_min: float | np.ndarray | None = None,
        deadzone: float | np.ndarray = 0.0,
        damping_lambda: float = 1e-4,
        tau_actuator: float = 0.04,
        tau_fault: float = 0.028,
        tau_recover: float = 0.85,
        shunt_floor: float = 0.22,
        shunt_knee: float = 0.48,
        effort_deadband_frac: float = 0.03,
        prioritize_yaw_sway: bool = True,
        prioritize_attitude: bool = True,
        probe_interval_s: float | None = None,
    ) -> None:
        w_mat = np.asarray(wrench_matrix, dtype=np.float64)
        if w_mat.ndim != 2 or w_mat.shape[0] not in (3, 4) or not (4 <= w_mat.shape[1] <= 8):
            raise ValueError(
                f"wrench_matrix must have shape (3, N) or (4, N) with 4 <= N <= 8, got {w_mat.shape}"
            )
        self.W = w_mat.copy()
        self.m_dof = int(w_mat.shape[0])
        self.n_actuators = int(w_mat.shape[1])

        if np.ndim(u_max) == 0:
            u_max_vec = np.full(self.n_actuators, float(u_max), dtype=np.float64)
        else:
            u_max_vec = np.asarray(u_max, dtype=np.float64).copy()
            if u_max_vec.shape != (self.n_actuators,):
                raise ValueError(
                    f"u_max array must have shape ({self.n_actuators},), got {u_max_vec.shape}"
                )
        if np.any(u_max_vec <= 0.0):
            raise ValueError("u_max must be strictly positive for all actuators")

        if u_min is None:
            u_min_vec = -u_max_vec.copy()
        elif np.ndim(u_min) == 0:
            u_min_vec = np.full(self.n_actuators, float(u_min), dtype=np.float64)
        else:
            u_min_vec = np.asarray(u_min, dtype=np.float64).copy()
            if u_min_vec.shape != (self.n_actuators,):
                raise ValueError(
                    f"u_min array must have shape ({self.n_actuators},), got {u_min_vec.shape}"
                )
        if np.any(u_min_vec > 0.0) or np.any(u_min_vec >= u_max_vec):
            raise ValueError("u_min must satisfy u_min <= 0 < u_max for every actuator")

        if np.ndim(deadzone) == 0:
            dz_vec = np.full(self.n_actuators, float(deadzone), dtype=np.float64)
        else:
            dz_vec = np.asarray(deadzone, dtype=np.float64).copy()
            if dz_vec.shape != (self.n_actuators,):
                raise ValueError(
                    f"deadzone array must have shape ({self.n_actuators},), got {dz_vec.shape}"
                )
        if np.any(dz_vec < 0.0):
            raise ValueError("deadzone must be non-negative")

        self.u_max_vec = u_max_vec
        self.u_min_vec = u_min_vec
        self.deadzone_vec = dz_vec
        self.u_max = float(np.max(u_max_vec))
        self.u_min = float(np.min(u_min_vec))

        self.damping_lambda = float(damping_lambda)
        self.tau_actuator = float(tau_actuator)
        self.tau_fault = float(tau_fault)
        self.tau_recover = float(tau_recover)
        self.shunt_floor = float(shunt_floor)
        self.shunt_knee = float(max(shunt_floor + 1e-3, shunt_knee))
        self.effort_deadband = float(effort_deadband_frac) * self.u_max
        self.prioritize_yaw_sway = bool(prioritize_yaw_sway)
        self.prioritize_attitude = bool(prioritize_attitude)
        if probe_interval_s is not None and probe_interval_s <= 0.0:
            raise ValueError("probe_interval_s must be positive or None")
        self.probe_interval_s = None if probe_interval_s is None else float(probe_interval_s)

        self.health = np.ones(self.n_actuators, dtype=np.float64)
        self.shunt_gain = np.ones(self.n_actuators, dtype=np.float64)
        self._last_cmd = np.zeros(self.n_actuators, dtype=np.float64)
        self._expected_effort = np.zeros(self.n_actuators, dtype=np.float64)
        self._initialized_effort = False
        self._isolated_time = np.zeros(self.n_actuators, dtype=np.float64)
        self.probe_count = 0

    @classmethod
    def usv_vectored_4(
        cls,
        *,
        lx: float = 0.45,
        ly: float = 0.35,
        cant_deg: float = 45.0,
        u_max: float = 35.0,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build a standard 4-thruster X-vectored marine USV / ROV allocator.

        Body convention: `+x` forward (surge), `+y` port/left (sway), `+z` up (CCW yaw).
        Thruster layout:
          - `0`: Port-Fore `(+lx, +ly)`, thrust angle `-cant_deg`
          - `1`: Stbd-Fore `(+lx, -ly)`, thrust angle `+cant_deg`
          - `2`: Port-Aft  `(-lx, +ly)`, thrust angle `+cant_deg`
          - `3`: Stbd-Aft  `(-lx, -ly)`, thrust angle `-cant_deg`
        Any 3-column submatrix has full 3-DOF rank (`det = +-4*cos*sin*m != 0`).
        """
        rad = math.radians(float(cant_deg))
        c = math.cos(rad)
        s = math.sin(rad)
        positions = [(+lx, +ly), (+lx, -ly), (-lx, +ly), (-lx, -ly)]
        dirs = [(+c, -s), (+c, +s), (+c, +s), (+c, -s)]
        cols = []
        for (px, py), (dx, dy) in zip(positions, dirs):
            mz = px * dy - py * dx
            cols.append([dx, dy, mz])
        w_mat = np.array(cols, dtype=np.float64).T
        return cls(w_mat, u_max=u_max, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def usv_t200_vectored_4(
        cls,
        *,
        lx: float = 0.45,
        ly: float = 0.35,
        cant_deg: float = 45.0,
        u_min: float = -40.0,
        u_max: float = 52.5,
        deadzone: float = 0.0,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build an asymmetric 4-thruster BlueRobotics `T200` (`-40.0..+52.5 N`) USV/ROV allocator."""
        return cls.usv_vectored_4(
            lx=lx,
            ly=ly,
            cant_deg=cant_deg,
            u_min=u_min,
            u_max=u_max,
            deadzone=deadzone,
            **kwargs,
        )

    @classmethod
    def hex_vectored_6(
        cls,
        *,
        radius: float = 0.50,
        u_max: float = 30.0,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build a 6-actuator omnidirectional ROV / hex-vessel 3-DOF wrench allocator."""
        cols = []
        for i in range(6):
            ang = 2.0 * math.pi * i / 6.0
            px = radius * math.cos(ang)
            py = radius * math.sin(ang)
            thrust_ang = (math.pi / 4.0) if (i % 2 == 0) else (-math.pi / 4.0)
            dx = math.cos(thrust_ang)
            dy = math.sin(thrust_ang)
            mz = px * dy - py * dx
            cols.append([dx, dy, mz])
        return cls(np.array(cols, dtype=np.float64).T, u_max=u_max, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def octo_vectored_8(
        cls,
        *,
        radius: float = 0.55,
        u_max: float = 25.0,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build an 8-actuator heavy ROV / octo-thruster 3-DOF wrench allocator."""
        cols = []
        for i in range(8):
            ang = 2.0 * math.pi * i / 8.0
            px = radius * math.cos(ang)
            py = radius * math.sin(ang)
            thrust_ang = (math.pi / 5.0) if (i % 2 == 0) else (-math.pi / 5.0)
            dx = math.cos(thrust_ang)
            dy = math.sin(thrust_ang)
            mz = px * dy - py * dx
            cols.append([dx, dy, mz])
        return cls(np.array(cols, dtype=np.float64).T, u_max=u_max, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def multicopter_hex_6(
        cls,
        *,
        arm_m: float = 0.28,
        k_yaw: float = 0.018,
        u_max: float = 18.0,
        damping_lambda: float = 1e-5,
        prioritize_attitude: bool = True,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build a 4-DOF `[Fz, Tx, Ty, Tz]` 6-rotor unidirectional (`[0, u_max]`) hexacopter allocator.

        Body FLU convention (`+x` nose, `+y` left, `+z` up):
          - `Fz = sum(u_i)`
          - `Tx = sum(py_i * u_i)`  (roll)
          - `Ty = sum(-px_i * u_i)` (pitch)
          - `Tz = sum((-1)^i * k_yaw * u_i)` (reaction yaw)
        Prioritizes `[Fz, Tx, Ty]` over `Tz` under rotor failure for relaxed-hover survival.
        """
        cols = []
        for i in range(6):
            ang = 2.0 * math.pi * i / 6.0
            px = float(arm_m) * math.cos(ang)
            py = float(arm_m) * math.sin(ang)
            spin = 1.0 if (i % 2 == 0) else -1.0
            cols.append([1.0, py, -px, spin * float(k_yaw)])
        w_mat = np.array(cols, dtype=np.float64).T
        return cls(
            w_mat,
            u_min=0.0,
            u_max=u_max,
            damping_lambda=damping_lambda,
            prioritize_attitude=prioritize_attitude,
            **kwargs,  # type: ignore[arg-type]
        )

    @classmethod
    def multicopter_octo_8(
        cls,
        *,
        arm_m: float = 0.32,
        k_yaw: float = 0.018,
        u_max: float = 16.0,
        damping_lambda: float = 1e-5,
        prioritize_attitude: bool = True,
        **kwargs: float | bool | np.ndarray | None,
    ) -> "ShuntingThrusterCord":
        """Build a 4-DOF `[Fz, Tx, Ty, Tz]` 8-rotor unidirectional (`[0, u_max]`) octocopter allocator."""
        cols = []
        for i in range(8):
            ang = 2.0 * math.pi * i / 8.0
            px = float(arm_m) * math.cos(ang)
            py = float(arm_m) * math.sin(ang)
            spin = 1.0 if (i % 2 == 0) else -1.0
            cols.append([1.0, py, -px, spin * float(k_yaw)])
        w_mat = np.array(cols, dtype=np.float64).T
        return cls(
            w_mat,
            u_min=0.0,
            u_max=u_max,
            damping_lambda=damping_lambda,
            prioritize_attitude=prioritize_attitude,
            **kwargs,  # type: ignore[arg-type]
        )

    def reset_health(self) -> None:
        """Reset all actuator health estimates and presynaptic shunting gates to `1.0`."""
        self.health.fill(1.0)
        self.shunt_gain.fill(1.0)
        self._last_cmd.fill(0.0)
        self._expected_effort.fill(0.0)
        self._initialized_effort = False
        self._isolated_time.fill(0.0)

    def reinstate_actuator(self, index: int) -> None:
        """Return an isolated actuator to service (health set to `shunt_knee`)."""
        self.set_actuator_health(index, self.shunt_knee)
        self._isolated_time[index] = 0.0

    def _probe_isolated(self, dt: float) -> None:
        """Periodically re-test isolated actuators so transient faults (ESC brownout) can recover."""
        if self.probe_interval_s is None:
            return
        for i in range(self.n_actuators):
            if self.shunt_gain[i] <= 1e-4:
                self._isolated_time[i] += max(float(dt), 0.0)
                if self._isolated_time[i] >= self.probe_interval_s:
                    self.reinstate_actuator(i)
                    self.probe_count += 1
            else:
                self._isolated_time[i] = 0.0

    def set_actuator_health(self, index: int, health: float) -> None:
        """Explicitly set health `h_i in [0, 1]` for actuator `index` and update shunting."""
        if not 0 <= index < self.n_actuators:
            raise ValueError(f"Actuator index {index} out of range 0..{self.n_actuators - 1}")
        self.health[index] = float(max(0.0, min(1.0, health)))
        self._recompute_shunt_gains()

    def _recompute_shunt_gains(self) -> None:
        for i in range(self.n_actuators):
            h = float(self.health[i])
            if h <= self.shunt_floor:
                self.shunt_gain[i] = 0.0
            elif h >= self.shunt_knee:
                self.shunt_gain[i] = 1.0
            else:
                t = (h - self.shunt_floor) / (self.shunt_knee - self.shunt_floor)
                self.shunt_gain[i] = 0.35 + 0.65 * t * t * (3.0 - 2.0 * t)

    def _update_health_from_feedback(
        self,
        feedback_thrust: np.ndarray | None,
        measured_wrench: np.ndarray | None,
        dt: float,
    ) -> None:
        if not self._initialized_effort:
            return

        dt_pos = max(float(dt), 1e-4)
        if self.tau_actuator > 1e-4:
            alpha_act = 1.0 - math.exp(-dt_pos / self.tau_actuator)
            self._expected_effort += alpha_act * (self._last_cmd - self._expected_effort)
        else:
            self._expected_effort = self._last_cmd.copy()

        if feedback_thrust is not None:
            fb = np.asarray(feedback_thrust, dtype=np.float64)
            if fb.shape != (self.n_actuators,):
                raise ValueError(
                    f"feedback_thrust must have shape ({self.n_actuators},), got {fb.shape}"
                )
            for i in range(self.n_actuators):
                e_i = float(self._expected_effort[i])
                c_i = float(self._last_cmd[i])
                if e_i * c_i <= 0.0:
                    u_ref = 0.0
                else:
                    u_ref = c_i if abs(c_i) < abs(e_i) else e_i
                if abs(e_i) > self.effort_deadband and abs(u_ref) > 0.2 * self.effort_deadband:
                    raw_ratio = float(fb[i]) / u_ref
                    est = max(0.0, min(1.0, raw_ratio))
                    tau = self.tau_fault if est < self.health[i] else self.tau_recover
                    alpha = 1.0 - math.exp(-dt_pos / max(tau, 1e-4))
                    self.health[i] += alpha * (est - self.health[i])
        elif measured_wrench is not None:
            w_meas = np.asarray(measured_wrench, dtype=np.float64)
            if w_meas.shape != (self.m_dof,):
                raise ValueError(
                    f"measured_wrench must have shape ({self.m_dof},), got {w_meas.shape}"
                )
            if float(np.linalg.norm(self._expected_effort)) > self.effort_deadband:
                w_pred = self.W @ (self.health * self._expected_effort)
                residual = w_meas - w_pred
                col_norms_sq = np.sum(self.W**2, axis=0)
                step_gain = min(0.65, dt_pos / max(self.tau_fault, 1e-3))
                for i in range(self.n_actuators):
                    ui = float(self._expected_effort[i])
                    if abs(ui) > self.effort_deadband:
                        denom = (ui * ui) * float(col_norms_sq[i]) + 1e-6
                        delta_h = float(ui * np.dot(self.W[:, i], residual)) / denom
                        self.health[i] = float(
                            np.clip(self.health[i] + step_gain * delta_h, 0.0, 1.0)
                        )

        self.health = np.clip(self.health, 0.0, 1.0)
        self._recompute_shunt_gains()

    def _compute_gain_matrix(self, w_sub: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        mat = self.W if w_sub is None else w_sub
        m = int(mat.shape[0])
        eff_w = (self.health**2) * self.shunt_gain
        gram = (mat * eff_w[None, :]) @ mat.T + self.damping_lambda * np.eye(m, dtype=np.float64)
        gram_inv = np.linalg.inv(gram)
        k_mat = ((self.shunt_gain * self.health)[:, None] * mat.T) @ gram_inv
        return k_mat, eff_w

    def _scale_into_box_from_zero(self, u_raw: np.ndarray) -> np.ndarray:
        """Directionally scale `u_raw` from `0` into `[u_min_vec, u_max_vec]` when `0` is interior."""
        scale = 1.0
        for i in range(self.n_actuators):
            if self.shunt_gain[i] <= 1e-4:
                continue
            ui = float(u_raw[i])
            if ui > self.u_max_vec[i] and ui > 1e-12:
                scale = min(scale, float(self.u_max_vec[i]) / ui)
            elif ui < self.u_min_vec[i] and ui < -1e-12:
                if self.u_min_vec[i] < -1e-12:
                    scale = min(scale, float(self.u_min_vec[i]) / ui)
                else:
                    scale = 0.0
        return np.clip(u_raw * scale, self.u_min_vec, self.u_max_vec)

    def _solve_active_set(
        self,
        w_sub: np.ndarray,
        w_target: np.ndarray,
        fixed_mask: np.ndarray | None = None,
        u_fixed: np.ndarray | None = None,
    ) -> np.ndarray:
        """Iterative active-set primal-dual box-constrained weighted allocator on `[u_min, u_max]`."""
        m = int(w_sub.shape[0])
        u = np.zeros(self.n_actuators, dtype=np.float64) if u_fixed is None else u_fixed.copy()
        free = (self.shunt_gain > 1e-4) & (self.health > 1e-6)
        u[~free] = 0.0
        if fixed_mask is not None:
            free = free & (~fixed_mask)

        # -1 = clamped at u_min, +1 = clamped at u_max, 0 = not bound-clamped
        clamp_dir = np.zeros(self.n_actuators, dtype=np.int32)
        releases = 0

        for _ in range(2 * self.n_actuators + 2):
            if not np.any(free):
                break

            fixed_now = ~free
            w_rem = w_target - w_sub[:, fixed_now] @ (self.health[fixed_now] * u[fixed_now])

            w_f = w_sub[:, free]
            h_f = self.health[free]
            s_f = self.shunt_gain[free]
            eff_f = (h_f**2) * s_f
            gram = (w_f * eff_f[None, :]) @ w_f.T + self.damping_lambda * np.eye(m, dtype=np.float64)
            x = np.linalg.solve(gram, w_rem)
            u_cand = (s_f * h_f) * (w_f.T @ x)

            free_indices = np.flatnonzero(free)
            worst_idx = -1
            worst_viol = 1e-7
            worst_dir = 0
            for loc_k, act_i in enumerate(free_indices):
                val = float(u_cand[loc_k])
                lo = float(self.u_min_vec[act_i])
                hi = float(self.u_max_vec[act_i])
                if val < lo - 1e-7 and (lo - val) > worst_viol:
                    worst_viol = lo - val
                    worst_idx = int(act_i)
                    worst_dir = -1
                elif val > hi + 1e-7 and (val - hi) > worst_viol:
                    worst_viol = val - hi
                    worst_idx = int(act_i)
                    worst_dir = +1

            if worst_idx >= 0:
                u[worst_idx] = (
                    float(self.u_min_vec[worst_idx])
                    if worst_dir < 0
                    else float(self.u_max_vec[worst_idx])
                )
                free[worst_idx] = False
                clamp_dir[worst_idx] = worst_dir
                continue

            for loc_k, act_i in enumerate(free_indices):
                u[act_i] = float(
                    np.clip(u_cand[loc_k], self.u_min_vec[act_i], self.u_max_vec[act_i])
                )

            # KKT multiplier sign check on bound-clamped actuators when wrench residual is non-zero
            res = w_target - w_sub @ (self.health * u)
            if float(np.linalg.norm(res)) > 1e-5 and releases < self.n_actuators:
                best_rel = -1
                best_gain = 1e-5
                for j in range(self.n_actuators):
                    if clamp_dir[j] == 0:
                        continue
                    grad_j = float(self.health[j] * np.dot(w_sub[:, j], res))
                    if clamp_dir[j] == -1 and grad_j > best_gain and self.u_max_vec[j] > self.u_min_vec[j]:
                        best_gain = grad_j
                        best_rel = j
                    elif clamp_dir[j] == +1 and (-grad_j) > best_gain and self.u_max_vec[j] > self.u_min_vec[j]:
                        best_gain = -grad_j
                        best_rel = j
                if best_rel >= 0:
                    free[best_rel] = True
                    clamp_dir[best_rel] = 0
                    releases += 1
                    continue
            break

        return u

    def _apply_deadzone(self, w_sub: np.ndarray, w_target: np.ndarray, u_cmd: np.ndarray) -> np.ndarray:
        """Snap non-zero commands outside `(-dz_i, +dz_i)` and rebalance remaining free actuators."""
        if not np.any(self.deadzone_vec > 0.0) or float(np.linalg.norm(w_target)) <= 1e-7:
            return u_cmd

        u = u_cmd.copy()
        fixed = np.zeros(self.n_actuators, dtype=bool)
        for _ in range(self.n_actuators):
            worst_i = -1
            smallest_au = float("inf")
            for i in range(self.n_actuators):
                if fixed[i] or self.shunt_gain[i] <= 1e-4:
                    continue
                dz = float(self.deadzone_vec[i])
                au = abs(float(u[i]))
                if dz > 0.0 and 0.0 < au < dz - 1e-9 and au < smallest_au:
                    smallest_au = au
                    worst_i = i
            if worst_i < 0:
                break

            next_fixed = fixed.copy()
            next_fixed[worst_i] = True
            dz_w = float(self.deadzone_vec[worst_i])

            u_try0 = u.copy()
            u_try0[worst_i] = 0.0
            u_res0 = self._solve_active_set(w_sub, w_target, fixed_mask=next_fixed, u_fixed=u_try0)

            u_trydz = u.copy()
            u_trydz[worst_i] = (
                min(float(self.u_max_vec[worst_i]), dz_w)
                if u[worst_i] > 0.0
                else max(float(self.u_min_vec[worst_i]), -dz_w)
            )
            u_resdz = self._solve_active_set(w_sub, w_target, fixed_mask=next_fixed, u_fixed=u_trydz)

            err0 = float(np.linalg.norm(w_sub @ (self.health * u_res0) - w_target, ord=1))
            errdz = float(np.linalg.norm(w_sub @ (self.health * u_resdz) - w_target, ord=1))
            fixed = next_fixed
            u = u_res0 if err0 <= errdz + 1e-3 else u_resdz

        # Final hard deadband guarantee
        for i in range(self.n_actuators):
            dz = float(self.deadzone_vec[i])
            if dz > 0.0 and 0.0 < abs(float(u[i])) < dz - 1e-9:
                if abs(float(u[i])) < 0.5 * dz:
                    u[i] = 0.0
                elif u[i] > 0.0:
                    u[i] = min(float(self.u_max_vec[i]), dz)
                else:
                    u[i] = max(float(self.u_min_vec[i]), -dz)
        return u

    def allocate(self, wrench_des: np.ndarray) -> AllocationResult:
        """Allocate 3-DOF `[Fx, Fy, Tz]` or 4-DOF `[Fz, Tx, Ty, Tz]` within `[u_min, u_max]`."""
        w = np.asarray(wrench_des, dtype=np.float64)
        if w.shape != (self.m_dof,):
            raise ValueError(f"wrench_des must have shape ({self.m_dof},), got {w.shape}")

        k_mat, eff_w = self._compute_gain_matrix()
        isolated = self.shunt_gain <= 1e-4

        if self.m_dof == 3:
            u_as = self._solve_active_set(self.W, w)
            err_as = float(np.linalg.norm((self.W @ (self.health * u_as)) - w))
            tol_as = 1e-2 * max(1.0, float(np.linalg.norm(w)))

            if err_as <= tol_as:
                u_cmd = u_as
            elif self.prioritize_yaw_sway:
                w_lat = np.array([0.0, w[1], w[2]], dtype=np.float64)
                w_surge = np.array([w[0], 0.0, 0.0], dtype=np.float64)

                u_lat = self._solve_active_set(self.W, w_lat)
                err_lat = float(np.linalg.norm((self.W @ (self.health * u_lat)) - w_lat))
                if err_lat > 1e-2 * max(1.0, float(np.linalg.norm(w_lat))) and np.all(
                    self.u_min_vec < -1e-6
                ):
                    u_lat = self._scale_into_box_from_zero(k_mat @ w_lat)

                u_surge = k_mat @ w_surge
                alpha = 1.0
                for i in range(self.n_actuators):
                    if isolated[i]:
                        continue
                    ds = float(u_surge[i])
                    if abs(ds) > 1e-9:
                        if ds > 0.0:
                            rem = max(0.0, float(self.u_max_vec[i]) - float(u_lat[i]))
                        else:
                            rem = max(0.0, float(u_lat[i]) - float(self.u_min_vec[i]))
                        alpha = min(alpha, rem / abs(ds))
                u_cmd = np.clip(u_lat + alpha * u_surge, self.u_min_vec, self.u_max_vec)
            elif np.all(self.u_min_vec < -1e-6):
                u_cmd = self._scale_into_box_from_zero(k_mat @ w)
            else:
                u_cmd = u_as

            u_cmd = self._apply_deadzone(self.W, w, u_cmd)
        else:
            # 4-DOF [Fz, Tx, Ty, Tz] active-set with attitude/thrust priority over reaction yaw
            u_4d = self._solve_active_set(self.W, w)
            pred_4d = self.W @ (self.health * u_4d)
            fz_ok = abs(float(pred_4d[0] - w[0])) <= 2e-3 * max(1.0, abs(float(w[0])))
            att_ok = float(np.linalg.norm(pred_4d[1:3] - w[1:3])) <= 2e-3 * max(
                0.2, float(np.linalg.norm(w[1:3]))
            )

            if (fz_ok and att_ok) or not self.prioritize_attitude:
                u_cmd = u_4d
            else:
                w_att = self.W[0:3, :]
                u_att = self._solve_active_set(w_att, w[0:3])
                e_yaw = float(w[3] - np.dot(self.W[3, :], self.health * u_att))
                movable = (
                    (~isolated)
                    & (self.health > 1e-6)
                    & (u_att > self.u_min_vec + 1e-5)
                    & (u_att < self.u_max_vec - 1e-5)
                )
                if abs(e_yaw) > 1e-5 and int(np.sum(movable)) >= 4:
                    eff_m = (self.health[movable] ** 2) * self.shunt_gain[movable]
                    w_m = w_att[:, movable]
                    gram_m = (w_m * eff_m[None, :]) @ w_m.T + self.damping_lambda * np.eye(
                        3, dtype=np.float64
                    )
                    rhs_m = w_m @ (eff_m * self.W[3, movable])
                    q_m = np.linalg.solve(gram_m, rhs_m)
                    du_m = (self.shunt_gain[movable] * self.health[movable]) * (
                        self.W[3, movable] - w_m.T @ q_m
                    )
                    yaw_gain = float(np.dot(self.W[3, movable], self.health[movable] * du_m))
                    if yaw_gain > 1e-7:
                        beta = e_yaw / yaw_gain
                        mov_idx = np.flatnonzero(movable)
                        if beta > 0.0:
                            for loc_k, act_i in enumerate(mov_idx):
                                d_k = float(du_m[loc_k])
                                if d_k > 1e-9:
                                    beta = min(
                                        beta,
                                        max(0.0, float(self.u_max_vec[act_i] - u_att[act_i])) / d_k,
                                    )
                                elif d_k < -1e-9:
                                    beta = min(
                                        beta,
                                        max(0.0, float(u_att[act_i] - self.u_min_vec[act_i])) / (-d_k),
                                    )
                        else:
                            for loc_k, act_i in enumerate(mov_idx):
                                d_k = float(du_m[loc_k])
                                if d_k > 1e-9:
                                    beta = max(
                                        beta,
                                        -max(0.0, float(u_att[act_i] - self.u_min_vec[act_i])) / d_k,
                                    )
                                elif d_k < -1e-9:
                                    beta = max(
                                        beta,
                                        -max(0.0, float(self.u_max_vec[act_i] - u_att[act_i])) / (-d_k),
                                    )
                        u_att[movable] = np.clip(
                            u_att[movable] + beta * du_m,
                            self.u_min_vec[movable],
                            self.u_max_vec[movable],
                        )
                u_cmd = u_att
            u_cmd = self._apply_deadzone(self.W[0:3, :] if self.prioritize_attitude else self.W, w[0:3] if self.prioritize_attitude else w, u_cmd)

        u_cmd = np.where(isolated, 0.0, np.clip(u_cmd, self.u_min_vec, self.u_max_vec))
        saturated = (~isolated) & (
            (u_cmd <= self.u_min_vec + 1e-5) | (u_cmd >= self.u_max_vec - 1e-5)
        )
        pred_wrench = self.W @ (self.health * u_cmd)
        return AllocationResult(
            commands=u_cmd,
            health=self.health.copy(),
            shunt_gain=self.shunt_gain.copy(),
            effective_weights=eff_w,
            predicted_wrench=pred_wrench,
            isolated_mask=isolated,
            saturated_mask=saturated,
        )

    def step(
        self,
        wrench_or_frame: np.ndarray | DescendingFrame | bytes | bytearray,
        *,
        feedback_thrust: np.ndarray | None = None,
        measured_wrench: np.ndarray | None = None,
        esc_observer: ESCObserver | None = None,
        esc_rpm: np.ndarray | None = None,
        esc_current_a: np.ndarray | None = None,
        dt: float = 0.02,
        max_surge: float = 60.0,
        max_sway: float = 40.0,
        max_yaw: float = 25.0,
        hover_thrust: float = 36.0,
        max_roll: float = 4.0,
        max_pitch: float = 4.0,
    ) -> AllocationResult:
        """Update proprioceptive actuator health + shunting and allocate next thrust vector."""
        esc_res: ESCObserverResult | None = None
        eff_feedback = feedback_thrust
        if esc_observer is not None and esc_rpm is not None and esc_current_a is not None:
            esc_res = esc_observer.update(self._last_cmd, esc_rpm, esc_current_a, dt=dt)
            if eff_feedback is None and self._initialized_effort:
                eff_feedback = esc_res.estimated_thrust

        self._update_health_from_feedback(
            feedback_thrust=eff_feedback,
            measured_wrench=measured_wrench,
            dt=dt,
        )
        self._probe_isolated(dt)

        if isinstance(wrench_or_frame, (bytes, bytearray, memoryview)):
            frame = decode_descending_4b(wrench_or_frame)
            w_des = (
                frame.to_wrench(max_surge=max_surge, max_sway=max_sway, max_yaw=max_yaw)
                if self.m_dof == 3
                else frame.to_wrench_4d(
                    hover_thrust=hover_thrust,
                    max_roll=max_roll,
                    max_pitch=max_pitch,
                    max_yaw=max_yaw,
                )
            )
        elif isinstance(wrench_or_frame, DescendingFrame):
            w_des = (
                wrench_or_frame.to_wrench(max_surge=max_surge, max_sway=max_sway, max_yaw=max_yaw)
                if self.m_dof == 3
                else wrench_or_frame.to_wrench_4d(
                    hover_thrust=hover_thrust,
                    max_roll=max_roll,
                    max_pitch=max_pitch,
                    max_yaw=max_yaw,
                )
            )
        else:
            w_des = np.asarray(wrench_or_frame, dtype=np.float64)

        res = self.allocate(w_des)
        res.esc_result = esc_res
        self._last_cmd = res.commands.copy()
        if not self._initialized_effort:
            if self.tau_actuator <= 1e-4:
                self._expected_effort = res.commands.copy()
            self._initialized_effort = True
        return res
