# MCU sensor feed.
#
# The sketch owns the I2C bus and pushes summaries over the Bridge:
#
#   sensor_status(tof_present, thermal_present, tof_fail, thermal_fail)   every 5 s
#   tof_summary(mean_mm, rms_mm, pp_mm, dom_hz, n_valid)                  every 0.5 s
#   thermal_stats(min_c, mean_c, max_c, ambient_c, hot_x, hot_y)          every 1 s
#   thermal_grid(c0 .. c11)                                               every 1 s
#
# Summaries rather than raw streams: the vibration RMS is computed on the MCU where the
# sampling clock is, and the 768-pixel thermal frame is reduced there rather than pushed
# across the Bridge 768 floats at a time.
#
# Nothing here fails when a sensor is missing - a channel that never reports simply reads
# as unavailable, and the app carries on with whatever else is plugged in.

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from arduino.app_utils import Bridge

import config

log = logging.getLogger(__name__)


class SensorHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tof: Optional[dict[str, Any]] = None
        self._tof_at = 0.0
        self._thermal: Optional[dict[str, Any]] = None
        self._thermal_at = 0.0
        self._grid: list[float] = []
        self._grid_at = 0.0
        self._present = {"tof": False, "thermal": False}
        self._failures = {"tof": 0, "thermal": 0}
        self._status_at = 0.0
        self._buses: dict[str, list[str]] = {}
        self._bus_at = 0.0
        self._registered = False

    # ------------------------------------------------------------------ wiring
    def start(self) -> None:
        """Register the Bridge handlers. Safe to call when no sketch is running."""
        if self._registered:
            return
        handlers = {
            "sensor_status": self._on_sensor_status,
            "tof_summary": self._on_tof_summary,
            "thermal_stats": self._on_thermal_stats,
            "thermal_grid": self._on_thermal_grid,
            "i2c_scan": self._on_i2c_scan,
        }
        for name, handler in handlers.items():
            try:
                Bridge.provide(name, handler)
            except Exception as exc:
                log.warning("could not register Bridge handler %r: %s", name, exc)
        self._registered = True
        log.info("sensor hub listening for MCU summaries")

    # ---------------------------------------------------------------- handlers
    def _on_sensor_status(self, tof_present: int, thermal_present: int,
                          tof_fail: int = 0, thermal_fail: int = 0) -> None:
        with self._lock:
            was = dict(self._present)
            self._present = {"tof": bool(tof_present), "thermal": bool(thermal_present)}
            self._failures = {"tof": int(tof_fail), "thermal": int(thermal_fail)}
            self._status_at = time.monotonic()
        if was != self._present:
            log.info(
                "MCU sensors: time-of-flight %s, thermal array %s",
                "present" if self._present["tof"] else "absent",
                "present" if self._present["thermal"] else "absent",
            )

    def _on_i2c_scan(self, bus: int, count: int, a0: int = 0, a1: int = 0,
                     a2: int = 0, a3: int = 0) -> None:
        """Whatever answered on one of the MCU's I2C buses. The sketch reports each bus
        separately, and only re-scans while a configured sensor is still missing."""
        name = "Wire" if bus == 0 else f"Wire{bus}"
        addresses = [f"0x{a:02x}" for a in (a0, a1, a2, a3) if a]
        with self._lock:
            changed = self._buses.get(name) != addresses
            self._buses[name] = addresses
            self._bus_at = time.monotonic()
        if changed:
            log.info(
                "MCU I2C %s: %s%s",
                name,
                ", ".join(addresses) if addresses else "nothing responded",
                f" (+{count - len(addresses)} more)" if count > len(addresses) else "",
            )

    def _on_tof_summary(self, mean_mm: float, rms_mm: float, pp_mm: float,
                        dom_hz: float, n_valid: int) -> None:
        with self._lock:
            self._tof = {
                "mean_mm": float(mean_mm),
                "rms": float(rms_mm),
                "peak_to_peak": float(pp_mm),
                "dominant_hz": float(dom_hz),
                "samples": int(n_valid),
            }
            self._tof_at = time.monotonic()

    def _on_thermal_stats(self, min_c: float, mean_c: float, max_c: float,
                          ambient_c: float, hot_x: int, hot_y: int) -> None:
        with self._lock:
            self._thermal = {
                "min": float(min_c),
                "mean": float(mean_c),
                "max": float(max_c),
                "ambient": float(ambient_c),
                "hot_x": int(hot_x),
                "hot_y": int(hot_y),
            }
            self._thermal_at = time.monotonic()

    def _on_thermal_grid(self, c0: float = 0.0, c1: float = 0.0, c2: float = 0.0,
                         c3: float = 0.0, c4: float = 0.0, c5: float = 0.0,
                         c6: float = 0.0, c7: float = 0.0, c8: float = 0.0,
                         c9: float = 0.0, c10: float = 0.0, c11: float = 0.0) -> None:
        """Coarse 4x3 thermal grid. The sketch doesn't send this yet; the handler is here
        so adding a thermal preview later is a sketch-only change."""
        with self._lock:
            self._grid = [float(c) for c in
                          (c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11)]
            self._grid_at = time.monotonic()

    # ------------------------------------------------------------------ access
    def _fresh(self, stamp: float) -> bool:
        return stamp > 0.0 and (time.monotonic() - stamp) <= config.SENSOR_STALE_S

    def vibration_sample(self) -> Optional[dict[str, Any]]:
        """Latest time-of-flight summary, or None if the sensor isn't reporting."""
        with self._lock:
            if self._tof is None or not self._fresh(self._tof_at):
                return None
            sample = dict(self._tof)
            sample["age_s"] = round(time.monotonic() - self._tof_at, 2)
            return sample

    def temperature_sample(self) -> Optional[dict[str, Any]]:
        """Latest thermal-array summary, or None if the sensor isn't reporting."""
        with self._lock:
            if self._thermal is None or not self._fresh(self._thermal_at):
                return None
            sample = dict(self._thermal)
            sample["age_s"] = round(time.monotonic() - self._thermal_at, 2)
            return sample

    def sample_for(self, channel: str) -> Optional[dict[str, Any]]:
        if channel == config.CHANNEL_VIBRATION:
            return self.vibration_sample()
        if channel == config.CHANNEL_TEMPERATURE:
            return self.temperature_sample()
        return None

    def available(self, channel: str) -> bool:
        return self.sample_for(channel) is not None

    def state(self) -> dict[str, Any]:
        """Sensor availability for the UI."""
        with self._lock:
            mcu_seen = self._fresh(self._status_at) or self._fresh(self._tof_at) or self._fresh(
                self._thermal_at
            )
            return {
                "mcu_reporting": bool(mcu_seen),
                "i2c_buses": dict(self._buses),
                "i2c_scanned": self._bus_at > 0.0,
                "tof": {
                    "detected": self._present["tof"],
                    "reporting": self._fresh(self._tof_at),
                    "read_failures": self._failures["tof"],
                    "latest": dict(self._tof) if self._tof else None,
                },
                "thermal": {
                    "detected": self._present["thermal"],
                    "reporting": self._fresh(self._thermal_at),
                    "read_failures": self._failures["thermal"],
                    "latest": dict(self._thermal) if self._thermal else None,
                    "grid": list(self._grid) if self._fresh(self._grid_at) else [],
                    "grid_shape": [config.THERMAL_GRID_ROWS, config.THERMAL_GRID_COLS],
                },
            }
