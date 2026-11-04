"""Presynaptic-Shunting Fault-Tolerant VNC Motor/Thruster Allocator (`ShuntingThrusterCord`).

Translates 3-DOF body wrench commands `[Fx, Fy, Tz]` (or `DescendingFrame` cervical packets)
across `4` to `8` redundant actuators (vectored marine USVs, ROVs, multirotors).

Biological mechanism (*Drosophila* VNC campaniform sensilla + presynaptic shunting):
  1. Online proprioceptive load monitor compares expected actuator effort against measured
     thrust / RPM / hull-wrench feedback to estimate actuator health `h_i in [0, 1]` within
     milliseconds of weed-fouling, propulsor shear, or ESC brownout.
  2. Fast presynaptic shunting inhibition (`s_i -> 0`) suppresses drive to severely degraded
     actuators (`h_i <= shunt_floor`), preventing motor stall heating and further entanglement.
  3. Surviving actuators continuously rebalance the 3-DOF wrench via weighted damped
     pseudo-inverse `W H W^T + lambda I` (`H_ii = h_i^2 * s_i`) with yaw/sway-prioritized
     desaturation so losing 1 of 4 vectored thrusters mid-mission causes virtually zero spin.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np

from .cervical import DescendingFrame, decode_descending_4b


@dataclass
class AllocationResult:
    """Single-step output of `ShuntingThrusterCord`."""

    commands: np.ndarray
    health: np.ndarray
    shunt_gain: np.ndarray
    effective_weights: np.ndarray
    predicted_wrench: np.ndarray
    isolated_mask: np.ndarray


class ShuntingThrusterCord:
    """Fault-tolerant 3-DOF wrench allocator for `4`..`8` actuators with presynaptic shunting."""

    def __init__(
        self,
        wrench_matrix: np.ndarray,
        *,
        u_max: float = 35.0,
        damping_lambda: float = 1e-4,
        tau_actuator: float = 0.04,
        tau_fault: float = 0.028,
        tau_recover: float = 0.85,
        shunt_floor: float = 0.22,
        shunt_knee: float = 0.48,
        effort_deadband_frac: float = 0.05,
        prioritize_yaw_sway: bool = True,
    ) -> None:
        w_mat = np.asarray(wrench_matrix, dtype=np.float64)
        if w_mat.ndim != 2 or w_mat.shape[0] != 3 or not (4 <= w_mat.shape[1] <= 8):
            raise ValueError(
                f"wrench_matrix must have shape (3, N) with 4 <= N <= 8, got {w_mat.shape}"
            )
        self.W = w_mat.copy()
        self.n_actuators = int(w_mat.shape[1])
        self.u_max = float(u_max)
        self.damping_lambda = float(damping_lambda)
        self.tau_actuator = float(tau_actuator)
        self.tau_fault = float(tau_fault)
        self.tau_recover = float(tau_recover)
        self.shunt_floor = float(shunt_floor)
        self.shunt_knee = float(max(shunt_floor + 1e-3, shunt_knee))
        self.effort_deadband = float(effort_deadband_frac) * self.u_max
        self.prioritize_yaw_sway = bool(prioritize_yaw_sway)

        self.health = np.ones(self.n_actuators, dtype=np.float64)
        self.shunt_gain = np.ones(self.n_actuators, dtype=np.float64)
        self._last_cmd = np.zeros(self.n_actuators, dtype=np.float64)
        self._expected_effort = np.zeros(self.n_actuators, dtype=np.float64)
        self._initialized_effort = False

    @classmethod
    def usv_vectored_4(
        cls,
        *,
        lx: float = 0.45,
        ly: float = 0.35,
        cant_deg: float = 45.0,
        u_max: float = 35.0,
        **kwargs: float | bool,
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
    def hex_vectored_6(
        cls,
        *,
        radius: float = 0.50,
        u_max: float = 30.0,
        **kwargs: float | bool,
    ) -> "ShuntingThrusterCord":
        """Build a 6-actuator omnidirectional ROV / hex-vessel wrench allocator."""
        cols = []
        for i in range(6):
            ang = 2.0 * math.pi * i / 6.0
            px = radius * math.cos(ang)
            py = radius * math.sin(ang)
            # Alternating tangential + surge cant so surge, sway, and yaw are fully spanned
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
        **kwargs: float | bool,
    ) -> "ShuntingThrusterCord":
        """Build an 8-actuator heavy ROV / octo-thruster wrench allocator."""
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

    def reset_health(self) -> None:
        """Reset all actuator health estimates and presynaptic shunting gates to `1.0`."""
        self.health.fill(1.0)
        self.shunt_gain.fill(1.0)
        self._last_cmd.fill(0.0)
        self._expected_effort.fill(0.0)
        self._initialized_effort = False

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
                u_ref = float(self._expected_effort[i])
                if abs(u_ref) > self.effort_deadband:
                    raw_ratio = float(fb[i]) / u_ref
                    est = max(0.0, min(1.0, raw_ratio))
                    tau = self.tau_fault if est < self.health[i] else self.tau_recover
                    alpha = 1.0 - math.exp(-dt_pos / max(tau, 1e-4))
                    self.health[i] += alpha * (est - self.health[i])
        elif measured_wrench is not None:
            w_meas = np.asarray(measured_wrench, dtype=np.float64)
            if w_meas.shape != (3,):
                raise ValueError(f"measured_wrench must have shape (3,), got {w_meas.shape}")
            if float(np.linalg.norm(self._expected_effort)) > self.effort_deadband:
                w_pred = self.W @ (self.health * self._expected_effort)
                residual = w_meas - w_pred
                # Column-preconditioned LMS proprioceptive wrench-residual update
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

    def _compute_gain_matrix(self) -> tuple[np.ndarray, np.ndarray]:
        eff_w = (self.health**2) * self.shunt_gain
        # W H W^T + lambda I_3
        gram = (self.W * eff_w[None, :]) @ self.W.T + self.damping_lambda * np.eye(
            3, dtype=np.float64
        )
        gram_inv = np.linalg.inv(gram)
        # u = diag(shunt_gain * health) W^T (W H W^T + lambda I)^-1 w_des
        k_mat = ((self.shunt_gain * self.health)[:, None] * self.W.T) @ gram_inv
        return k_mat, eff_w

    def allocate(self, wrench_des: np.ndarray) -> AllocationResult:
        """Allocate `wrench_des = [Fx, Fy, Tz]` using current health and shunting weights."""
        w = np.asarray(wrench_des, dtype=np.float64)
        if w.shape != (3,):
            raise ValueError(f"wrench_des must have shape (3,), got {w.shape}")

        k_mat, eff_w = self._compute_gain_matrix()

        if self.prioritize_yaw_sway:
            w_lat = np.array([0.0, w[1], w[2]], dtype=np.float64)
            w_surge = np.array([w[0], 0.0, 0.0], dtype=np.float64)

            u_lat = k_mat @ w_lat
            peak_lat = float(np.max(np.abs(u_lat)))
            if peak_lat > self.u_max and peak_lat > 1e-9:
                u_lat = u_lat * (self.u_max / peak_lat)

            u_surge = k_mat @ w_surge
            alpha = 1.0
            for i in range(self.n_actuators):
                ds = float(u_surge[i])
                if abs(ds) > 1e-9:
                    if ds > 0.0:
                        rem = max(0.0, self.u_max - float(u_lat[i]))
                    else:
                        rem = max(0.0, self.u_max + float(u_lat[i]))
                    alpha = min(alpha, rem / abs(ds))
            u_cmd = np.clip(u_lat + alpha * u_surge, -self.u_max, self.u_max)
        else:
            u_raw = k_mat @ w
            peak = float(np.max(np.abs(u_raw)))
            if peak > self.u_max and peak > 1e-9:
                u_cmd = u_raw * (self.u_max / peak)
            else:
                u_cmd = u_raw

        # Enforce strict zero drive on shunted actuators
        isolated = self.shunt_gain <= 1e-4
        u_cmd = np.where(isolated, 0.0, u_cmd)

        pred_wrench = self.W @ (self.health * u_cmd)
        return AllocationResult(
            commands=u_cmd,
            health=self.health.copy(),
            shunt_gain=self.shunt_gain.copy(),
            effective_weights=eff_w,
            predicted_wrench=pred_wrench,
            isolated_mask=isolated,
        )

    def step(
        self,
        wrench_or_frame: np.ndarray | DescendingFrame | bytes | bytearray,
        *,
        feedback_thrust: np.ndarray | None = None,
        measured_wrench: np.ndarray | None = None,
        dt: float = 0.02,
        max_surge: float = 60.0,
        max_sway: float = 40.0,
        max_yaw: float = 25.0,
    ) -> AllocationResult:
        """Update proprioceptive actuator health + shunting and allocate next thrust vector."""
        self._update_health_from_feedback(
            feedback_thrust=feedback_thrust,
            measured_wrench=measured_wrench,
            dt=dt,
        )

        if isinstance(wrench_or_frame, (bytes, bytearray, memoryview)):
            frame = decode_descending_4b(wrench_or_frame)
            w_des = frame.to_wrench(max_surge=max_surge, max_sway=max_sway, max_yaw=max_yaw)
        elif isinstance(wrench_or_frame, DescendingFrame):
            w_des = wrench_or_frame.to_wrench(
                max_surge=max_surge, max_sway=max_sway, max_yaw=max_yaw
            )
        else:
            w_des = np.asarray(wrench_or_frame, dtype=np.float64)

        res = self.allocate(w_des)
        self._last_cmd = res.commands.copy()
        if not self._initialized_effort:
            if self.tau_actuator <= 1e-4:
                self._expected_effort = res.commands.copy()
            self._initialized_effort = True
        return res
