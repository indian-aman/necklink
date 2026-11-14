"""
ROS 2 (`rclpy`) Autonomy Bridge & Pure-Python Message Adapter (`necklink_cns.ros2_node`).

Provides:
  1. `NeckLinkROS2Adapter`: zero-dependency message translator between standard ROS 2
     topic schemas (`sensor_msgs/Imu`, `sensor_msgs/MagneticField`, `sensor_msgs/Image`,
     `std_msgs/Float32MultiArray`, `geometry_msgs/Twist`, `std_msgs/String`) and
     `NeckLinkCNS`. Testable anywhere without ROS 2 installed.
  2. `create_ros2_node(cns)`: constructs a live `rclpy.node.Node` subscribing to `/imu/data_raw`,
     `/imu/mag`, `/camera/image_raw`, `/gas_or_vib/raw` and publishing `/cmd_vel`,
     `/necklink/heading_rad`, and `/necklink/status_json` on Ubuntu ROS 2 Humble / Jazzy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import numpy as np


@dataclass
class TwistCommand:
    """Standard `geometry_msgs/msg/Twist` payload."""

    linear_x: float
    linear_y: float
    linear_z: float
    angular_x: float
    angular_y: float
    angular_z: float


class NeckLinkROS2Adapter:
    """Framework-agnostic adapter mapping ROS 2 sensor payloads into `NeckLinkCNS` steps."""

    PROVENANCE = "engineering"

    def __init__(self, cns: Any, max_linear_m_s: float = 1.5, max_yaw_rad_s: float = 1.8) -> None:
        self.cns = cns
        self.max_linear_m_s = float(max_linear_m_s)
        self.max_yaw_rad_s = float(max_yaw_rad_s)
        self._latest_gyro = np.zeros(3, dtype=np.float64)
        self._latest_accel = np.array([0.0, 0.0, 9.81], dtype=np.float64)
        self._latest_mag: Optional[np.ndarray] = None
        self._latest_chemical: Optional[np.ndarray] = None
        self._latest_vibration: Optional[np.ndarray] = None

    def on_imu(self, gyro_xyz_rad_s: np.ndarray, accel_xyz_m_s2: np.ndarray) -> None:
        self._latest_gyro = np.asarray(gyro_xyz_rad_s, dtype=np.float64).reshape(3)
        self._latest_accel = np.asarray(accel_xyz_m_s2, dtype=np.float64).reshape(3)

    def on_mag(self, mag_xyz: np.ndarray) -> None:
        self._latest_mag = np.asarray(mag_xyz, dtype=np.float64).reshape(3)

    def on_chemical(self, vec: np.ndarray) -> None:
        self._latest_chemical = np.asarray(vec, dtype=np.float64).ravel()

    def on_vibration_window(self, samples: np.ndarray) -> None:
        self._latest_vibration = np.asarray(samples, dtype=np.float64).ravel()

    def step_camera(
        self,
        frame_2d: Optional[np.ndarray] = None,
        speed_m_s: float = 0.8,
        dt: float = 0.02,
    ) -> Dict[str, Any]:
        out = self.cns.step(
            dt=dt,
            speed_m_s=speed_m_s,
            gyro_xyz=self._latest_gyro,
            accel_xyz=self._latest_accel,
            mag_xyz=self._latest_mag,
            camera_frame=frame_2d,
            chemical_vec=self._latest_chemical,
            vibration_window=self._latest_vibration,
        )
        twist = TwistCommand(
            linear_x=float(out.descending.drive_fwd * self.max_linear_m_s),
            linear_y=float(out.descending.strafe_lat * self.max_linear_m_s * 0.6),
            linear_z=0.0,
            angular_x=0.0,
            angular_y=0.0,
            angular_z=float(out.descending.turn_yaw * self.max_yaw_rad_s),
        )
        return {
            "twist": twist,
            "heading_rad": float(out.heading_rad),
            "status_json": json.dumps(
                {
                    "mode": out.descending.behavior_mode,
                    "heading_rad": round(float(out.heading_rad), 4),
                    "home_distance_m": round(float(out.home_distance_m), 3),
                    "loom_score": round(float(out.loom_score), 4),
                    "thin_loom_score": round(float(out.thin_loom_score), 4),
                    "novelty": round(float(out.novelty_score), 4),
                    "cord_min_health": round(float(out.cord_min_health), 3),
                    "descending": asdict(out.descending),
                }
            ),
            "cns_output": out,
        }


def create_ros2_node(cns: Any, node_name: str = "necklink_cns_node") -> Any:
    """
    Create a live `rclpy.node.Node` when `rclpy` is installed in the environment.
    Raises an informative `ImportError` if ROS 2 Python packages are not present.
    """
    try:
        import rclpy  # type: ignore
        from rclpy.node import Node  # type: ignore
        from geometry_msgs.msg import Twist  # type: ignore
        from sensor_msgs.msg import Image, Imu, MagneticField  # type: ignore
        from std_msgs.msg import Float32, Float32MultiArray, String  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "ROS 2 (`rclpy`, `geometry_msgs`, `sensor_msgs`, `std_msgs`) is not installed in this Python "
            "environment. Source `/opt/ros/humble/setup.bash` (or `jazzy`) on Linux, or use "
            "`NeckLinkROS2Adapter` directly."
        ) from exc

    class _NeckLinkNode(Node):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(node_name)
            self.adapter = NeckLinkROS2Adapter(cns)
            self.pub_cmd = self.create_publisher(Twist, "/cmd_vel", 10)
            self.pub_hdg = self.create_publisher(Float32, "/necklink/heading_rad", 10)
            self.pub_status = self.create_publisher(String, "/necklink/status_json", 10)

            self.create_subscription(Imu, "/imu/data_raw", self._cb_imu, 20)
            self.create_subscription(MagneticField, "/imu/mag", self._cb_mag, 20)
            self.create_subscription(Float32MultiArray, "/necklink/chemical", self._cb_chem, 10)
            self.create_subscription(Image, "/camera/image_raw", self._cb_img, 10)

        def _cb_imu(self, msg: Any) -> None:
            g = np.array([msg.angular_velocity.x, msg.angular_velocity.y, msg.angular_velocity.z], dtype=np.float64)
            a = np.array([msg.linear_acceleration.x, msg.linear_acceleration.y, msg.linear_acceleration.z], dtype=np.float64)
            self.adapter.on_imu(g, a)

        def _cb_mag(self, msg: Any) -> None:
            m = np.array([msg.magnetic_field.x, msg.magnetic_field.y, msg.magnetic_field.z], dtype=np.float64)
            self.adapter.on_mag(m)

        def _cb_chem(self, msg: Any) -> None:
            self.adapter.on_chemical(np.asarray(msg.data, dtype=np.float64))

        def _cb_img(self, msg: Any) -> None:
            arr = np.frombuffer(msg.data, dtype=np.uint8)
            if msg.height > 0 and msg.width > 0 and arr.size >= msg.height * msg.width:
                channels = arr.size // (msg.height * msg.width)
                img = arr[: msg.height * msg.width * channels].reshape(msg.height, msg.width, channels)
                gray = img.mean(axis=2) if channels > 1 else img[:, :, 0].astype(np.float64)
            else:
                gray = None
            res = self.adapter.step_camera(gray, dt=0.02)
            tw = Twist()
            tw.linear.x = res["twist"].linear_x
            tw.linear.y = res["twist"].linear_y
            tw.angular.z = res["twist"].angular_z
            self.pub_cmd.publish(tw)

            hdg = Float32()
            hdg.data = float(res["heading_rad"])
            self.pub_hdg.publish(hdg)

            st = String()
            st.data = res["status_json"]
            self.pub_status.publish(st)

    return _NeckLinkNode()
