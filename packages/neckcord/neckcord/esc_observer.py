"""Sensorless ESC `RPM + Current` Proprioceptive Fault Observer (`ESCObserver`).

Estimates per-actuator effective thrust [N] and structural health `h_i in [0, 1]`
purely from standard ESC telemetry (`rpm` and bus/phase `current_a`, as reported by
`BLHeli_32`, `AM32`, `DroneCAN`, or `INA219` monitors) without requiring load cells.

Resolves three physical propulsor fault modes:
  1. **Open-phase / stalled / seized prop / desync** (`ESCFaultCode.STALL_OR_OPEN`):
     `|rpm|` sags far below `k_rpm * |u_cmd|^0.5` -> effective thrust drops as `(rpm / rpm_max)^2`.
  2. **Sheared prop / broken blade / shaft slip** (`ESCFaultCode.BROKEN_PROP`):
     `|rpm|` tracks nominal speed in air/water, but `current_a` drops far below the cubic
     aerodynamic/hydrodynamic torque curve `I_expected = I_idle + k_load * (|rpm| / rpm_max)^3`
     -> catches free-spinning broken propellers in `< 50 ms`.
  3. **Weed-fouled / entangled prop** (`ESCFaultCode.OVERCURRENT_FOUL`):
     `current_a` spikes `> overcurrent_ratio * I_expected` (`1.6x` default) while `rpm` sags
     -> flags `overcurrent_mask` and attenuates effective thrust so `ShuntingThrusterCord`
     shunts the entangled actuator before ESC MOSFET thermal failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
import numpy as np


class ESCFaultCode(IntEnum):
    """Sensorless ESC proprioceptive fault classification."""

    NONE = 0
    OK = 0
    STALL_OR_OPEN = 1
    BROKEN_PROP = 2
    OVERCURRENT_FOUL = 3


@dataclass
class ESCObserverResult:
    """Single-step output of `ESCObserver`."""

    estimated_thrust: np.ndarray
    health: np.ndarray
    expected_rpm: np.ndarray
    expected_current_a: np.ndarray
    fault_codes: np.ndarray
    stall_mask: np.ndarray
    broken_prop_mask: np.ndarray
    overcurrent_mask: np.ndarray


class ESCObserver:
    """Proprioceptive thrust & fault observer from ESC `RPM + Current` telemetry.

    Provenance (`PROVENANCE = "engineering"`):
      Sensorless brushless ESC `RPM + Current` cubic load modeling, open-phase stall detection,
      free-spinning broken-blade estimation, and overcurrent weed-fouling triggers are
      non-biological electromechanical actuator engineering.
    """

    PROVENANCE: str = "engineering"

    def __init__(
        self,
        n_actuators: int = 4,
        *,
        u_nom: float = 35.0,
        rpm_max: float = 5000.0,
        k_rpm: float | None = None,
        i_idle_a: float = 0.40,
        k_load_a: float = 18.0,
        overcurrent_ratio: float = 1.6,
        broken_load_ratio: float = 0.45,
        stall_rpm_ratio: float = 0.50,
        tau_motor: float = 0.015,
        tau_fault: float = 0.016,
        tau_recover: float = 0.60,
        cmd_deadband_frac: float = 0.05,
        fault_health_threshold: float = 0.45,
    ) -> None:
        if not 1 <= int(n_actuators) <= 16:
            raise ValueError(f"n_actuators must be in 1..16, got {n_actuators}")
        if u_nom <= 0.0 or rpm_max <= 0.0:
            raise ValueError("u_nom and rpm_max must be positive")
        if overcurrent_ratio <= 1.05:
            raise ValueError("overcurrent_ratio must be > 1.05")

        self.n_actuators = int(n_actuators)
        self.u_nom = float(u_nom)
        if k_rpm is not None:
            if k_rpm <= 0.0:
                raise ValueError("k_rpm must be positive")
            self.k_rpm = float(k_rpm)
            self.rpm_max = float(self.k_rpm * math.sqrt(self.u_nom))
        else:
            self.rpm_max = float(rpm_max)
            self.k_rpm = float(self.rpm_max / math.sqrt(self.u_nom))

        self.i_idle_a = float(max(0.0, i_idle_a))
        self.k_load_a = float(max(1e-3, k_load_a))
        self.overcurrent_ratio = float(overcurrent_ratio)
        self.broken_load_ratio = float(broken_load_ratio)
        self.stall_rpm_ratio = float(stall_rpm_ratio)
        self.tau_motor = float(max(0.0, tau_motor))
        self.tau_fault = float(max(1e-4, tau_fault))
        self.tau_recover = float(max(1e-4, tau_recover))
        self.cmd_deadband = float(cmd_deadband_frac) * self.u_nom
        self.fault_health_threshold = float(fault_health_threshold)

        self.health = np.ones(self.n_actuators, dtype=np.float64)
        self._lagged_cmd = np.zeros(self.n_actuators, dtype=np.float64)
        self._overcurrent_score = np.zeros(self.n_actuators, dtype=np.float64)
        self._broken_score = np.zeros(self.n_actuators, dtype=np.float64)
        self._stall_score = np.zeros(self.n_actuators, dtype=np.float64)
        self._initialized = False

    @classmethod
    def multicopter_hex_6(
        cls,
        *,
        u_nom: float = 18.0,
        rpm_max: float = 9200.0,
        i_idle_a: float = 0.35,
        k_load_a: float = 20.0,
        **kwargs: float,
    ) -> "ESCObserver":
        """Build a 6-rotor multicopter ESC observer (`BLHeli_32` / `AM32` / `DroneCAN`)."""
        return cls(
            6,
            u_nom=u_nom,
            rpm_max=rpm_max,
            i_idle_a=i_idle_a,
            k_load_a=k_load_a,
            **kwargs,
        )

    @classmethod
    def usv_t200_4(
        cls,
        *,
        u_nom: float = 52.5,
        rpm_max: float = 3900.0,
        i_idle_a: float = 0.50,
        k_load_a: float = 24.0,
        **kwargs: float,
    ) -> "ESCObserver":
        """Build a 4-thruster BlueRobotics `T200` marine USV/ROV ESC observer."""
        return cls(
            4,
            u_nom=u_nom,
            rpm_max=rpm_max,
            i_idle_a=i_idle_a,
            k_load_a=k_load_a,
            **kwargs,
        )

    def reset(self) -> None:
        """Reset all filtered health and fault integrators."""
        self.health.fill(1.0)
        self._lagged_cmd.fill(0.0)
        self._overcurrent_score.fill(0.0)
        self._broken_score.fill(0.0)
        self._stall_score.fill(0.0)
        self._initialized = False

    def expected_current_from_rpm(self, rpm: np.ndarray | float) -> np.ndarray:
        """Evaluate cubic shaft load `I_expected = I_idle + k_load * (|rpm| / rpm_max)^3`."""
        rpm_arr = np.asarray(rpm, dtype=np.float64)
        norm_w = np.clip(np.abs(rpm_arr) / self.rpm_max, 0.0, 2.0)
        return self.i_idle_a + self.k_load_a * (norm_w**3)

    def update(
        self,
        commands: np.ndarray,
        rpm: np.ndarray,
        current_a: np.ndarray,
        dt: float = 0.01,
    ) -> ESCObserverResult:
        """Update effective thrust and fault masks from commanded thrust, `rpm`, and `current_a`."""
        u_cmd = np.asarray(commands, dtype=np.float64)
        rpm_meas = np.asarray(rpm, dtype=np.float64)
        curr_meas = np.asarray(current_a, dtype=np.float64)
        expected_shape = (self.n_actuators,)
        if u_cmd.shape != expected_shape or rpm_meas.shape != expected_shape or curr_meas.shape != expected_shape:
            raise ValueError(
                f"commands, rpm, and current_a must all have shape {expected_shape}, "
                f"got {u_cmd.shape}, {rpm_meas.shape}, {curr_meas.shape}"
            )

        dt_pos = max(float(dt), 1e-4)
        if not self._initialized or self.tau_motor <= 1e-4:
            self._lagged_cmd = u_cmd.copy()
            self._initialized = True
        else:
            alpha_m = 1.0 - math.exp(-dt_pos / self.tau_motor)
            self._lagged_cmd += alpha_m * (u_cmd - self._lagged_cmd)

        exp_rpm = np.zeros(self.n_actuators, dtype=np.float64)
        exp_curr = np.zeros(self.n_actuators, dtype=np.float64)
        est_thrust = np.zeros(self.n_actuators, dtype=np.float64)
        fault_codes = np.zeros(self.n_actuators, dtype=np.int32)
        stall_mask = np.zeros(self.n_actuators, dtype=bool)
        broken_mask = np.zeros(self.n_actuators, dtype=bool)
        overcurr_mask = np.zeros(self.n_actuators, dtype=bool)

        alpha_fast = 1.0 - math.exp(-dt_pos / self.tau_fault)
        alpha_slow = 1.0 - math.exp(-dt_pos / self.tau_recover)
        min_dyn_curr = max(0.02 * self.k_load_a, 0.15)

        for i in range(self.n_actuators):
            lag_i = float(self._lagged_cmd[i])
            cmd_i = float(u_cmd[i])
            if lag_i * cmd_i <= 0.0:
                u_ref = 0.0
            else:
                u_ref = cmd_i if abs(cmd_i) < abs(lag_i) else lag_i
            abs_u = abs(u_ref)
            sign_u = -1.0 if u_ref < 0.0 else (1.0 if u_ref > 0.0 else 0.0)

            w_exp = self.k_rpm * math.sqrt(abs_u)
            exp_rpm[i] = math.copysign(w_exp, u_ref) if abs_u > 0.0 else 0.0

            abs_rpm = abs(float(rpm_meas[i]))
            norm_rpm = min(2.0, abs_rpm / self.rpm_max)
            i_dyn_exp = self.k_load_a * (norm_rpm**3)
            i_exp = self.i_idle_a + i_dyn_exp
            exp_curr[i] = i_exp

            # Quadratic RPM thrust before hydrodynamic load-coupling verification
            # If signed telemetry spins opposite to u_ref, effective forward thrust is 0.
            if float(rpm_meas[i]) * u_ref < -1e-3 * self.rpm_max:
                t_rpm_mag = 0.0
            else:
                t_rpm_mag = self.u_nom * (norm_rpm**2)

            if abs_u <= self.cmd_deadband:
                est_thrust[i] = sign_u * t_rpm_mag
                # Decay transient fault integrators slowly when idle
                self._overcurrent_score[i] *= 1.0 - 0.5 * alpha_slow
                self._broken_score[i] *= 1.0 - 0.5 * alpha_slow
                self._stall_score[i] *= 1.0 - 0.5 * alpha_slow
            else:
                r_omega = abs_rpm / max(w_exp, 1.0)
                curr_i = max(0.0, float(curr_meas[i]))

                # Check cubic load coupling for sheared prop / broken blade
                # Also compare against expected current at commanded RPM when free-spinning
                cmd_norm_rpm = min(2.0, w_exp / self.rpm_max)
                i_cmd_dyn = self.k_load_a * (cmd_norm_rpm**3)
                i_cmd_exp = self.i_idle_a + i_cmd_dyn

                is_overcurr_inst = (
                    curr_i > self.overcurrent_ratio * i_exp and r_omega < 0.95
                ) or (curr_i > self.overcurrent_ratio * i_cmd_exp)
                is_broken_inst = False
                is_stall_inst = False

                coupling = 1.0
                if is_overcurr_inst:
                    # Weed-fouled / entangled prop: high torque drag, degraded axial jet
                    ratio_oc = curr_i / max(i_exp, 1e-3)
                    excess = max(0.0, ratio_oc - 1.0)
                    foul_eff = max(0.04, 1.0 / (1.0 + 2.5 * excess * excess))
                    coupling = foul_eff
                elif r_omega < self.stall_rpm_ratio:
                    # Open-phase / seized motor / ESC desync
                    is_stall_inst = True
                    coupling = 1.0  # t_rpm_mag is already small (~ r_omega^2)
                elif i_dyn_exp >= min_dyn_curr:
                    eta_load = max(0.0, curr_i - self.i_idle_a) / i_dyn_exp
                    if eta_load < self.broken_load_ratio:
                        is_broken_inst = True
                        coupling = max(0.0, eta_load / max(0.60, self.broken_load_ratio))
                    elif eta_load < 0.65:
                        # Smooth transition window below normal wave/gust inflow band
                        coupling = eta_load / 0.65

                eff_mag = t_rpm_mag * coupling
                est_thrust[i] = sign_u * eff_mag

                inst_h = max(0.0, min(1.0, eff_mag / abs_u))
                alpha_h = alpha_fast if inst_h < self.health[i] else alpha_slow
                self.health[i] += alpha_h * (inst_h - self.health[i])

                self._overcurrent_score[i] += (alpha_fast if is_overcurr_inst else alpha_slow) * (
                    (1.0 if is_overcurr_inst else 0.0) - self._overcurrent_score[i]
                )
                self._broken_score[i] += (alpha_fast if is_broken_inst else alpha_slow) * (
                    (1.0 if is_broken_inst else 0.0) - self._broken_score[i]
                )
                self._stall_score[i] += (alpha_fast if is_stall_inst else alpha_slow) * (
                    (1.0 if is_stall_inst else 0.0) - self._stall_score[i]
                )

            if self._overcurrent_score[i] >= 0.45:
                overcurr_mask[i] = True
                fault_codes[i] = int(ESCFaultCode.OVERCURRENT_FOUL)
            elif self._broken_score[i] >= 0.45:
                broken_mask[i] = True
                fault_codes[i] = int(ESCFaultCode.BROKEN_PROP)
            elif self._stall_score[i] >= 0.45 or self.health[i] < self.fault_health_threshold:
                stall_mask[i] = True
                fault_codes[i] = int(ESCFaultCode.STALL_OR_OPEN)

        self.health = np.clip(self.health, 0.0, 1.0)
        return ESCObserverResult(
            estimated_thrust=est_thrust,
            health=self.health.copy(),
            expected_rpm=exp_rpm,
            expected_current_a=exp_curr,
            fault_codes=fault_codes,
            stall_mask=stall_mask,
            broken_prop_mask=broken_mask,
            overcurrent_mask=overcurr_mask,
        )

    observe = update
