# Runtime settings and the configuration model.
#
# An *asset* is one thing being monitored, identified by the AprilTag stuck on it. Each
# asset enables any combination of three channels:
#
#   gauge       - needle position read from the camera
#   vibration   - RMS wobble from the time-of-flight sensor
#   temperature - object temperature from the thermal array
#
# Every channel produces one scalar per capture, and every channel gets its own limits and
# its own anomaly model. A channel whose sensor isn't plugged in simply never reports.
#
# Gauge geometry is stored *in the coordinate frame of the tag*: the tag occupies the unit
# square (0,0)-(1,1), x right along its top edge, y down its left edge, one "tag unit" ==
# one tag width. Angles are degrees, 0 = up (-y), increasing clockwise.

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Optional

# --- camera -----------------------------------------------------------------
CAMERA_SOURCE: Optional[str] = os.getenv("GAUGE_CAMERA_SOURCE") or None
CAMERA_WIDTH = int(os.getenv("GAUGE_CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT = int(os.getenv("GAUGE_CAMERA_HEIGHT", "720"))
CAMERA_FPS = int(os.getenv("GAUGE_CAMERA_FPS", "10"))
CAMERA_RETRY_S = 10.0

# --- tag scanning -----------------------------------------------------------
TAG_FAMILY = os.getenv("GAUGE_TAG_FAMILY", "36h11")
STABLE_FRAMES = int(os.getenv("GAUGE_STABLE_FRAMES", "4"))
STABLE_MAX_SHIFT_PX = float(os.getenv("GAUGE_STABLE_SHIFT_PX", "4.0"))
MIN_TAG_EDGE_PX = 28.0
MIN_CAPTURE_INTERVAL_S = float(os.getenv("GAUGE_CAPTURE_INTERVAL_S", "30"))
MIN_CONFIDENCE = float(os.getenv("GAUGE_MIN_CONFIDENCE", "0.25"))

# --- needle detection -------------------------------------------------------
ANGLE_STEP_DEG = 0.5
MAX_RADIAL_SAMPLES = 120
SMOOTH_SPAN_DEG = 3.0

# --- preview ----------------------------------------------------------------
PREVIEW_WIDTH = 640
PREVIEW_QUALITY = 70
PREVIEW_PERIOD_S = 0.15
DIAL_JPEG_QUALITY = 80
KEEP_IMAGES = int(os.getenv("GAUGE_KEEP_IMAGES", "40"))

# --- MCU sensors ------------------------------------------------------------
# A sensor summary older than this is treated as "sensor not reporting".
SENSOR_STALE_S = float(os.getenv("GAUGE_SENSOR_STALE_S", "4.0"))
# How long the tag must have been held before a vibration sample is trusted - gives the
# rig time to stop moving, so we measure the machine's wobble and not our own.
VIBRATION_SETTLE_S = float(os.getenv("GAUGE_VIB_SETTLE_S", "1.5"))
THERMAL_ROWS = 24
THERMAL_COLS = 32
THERMAL_GRID_ROWS = 3   # coarse grid the MCU sends for the UI
THERMAL_GRID_COLS = 4

# --- predictive maintenance -------------------------------------------------
PDM_MIN_SAMPLES = int(os.getenv("GAUGE_PDM_MIN_SAMPLES", "40"))
PDM_REFIT_EVERY = int(os.getenv("GAUGE_PDM_REFIT_EVERY", "25"))
PDM_ROLL_WINDOW = 12
PDM_TREND_WINDOW = 30
PDM_TREND_MIN_R2 = 0.5
PDM_WATCH_HORIZON_H = 24.0
PDM_HISTORY_LIMIT = 2000
PDM_CONTAMINATION = float(os.getenv("GAUGE_PDM_CONTAMINATION", "0.03"))

# --- channels ---------------------------------------------------------------
CHANNEL_GAUGE = "gauge"
CHANNEL_VIBRATION = "vibration"
CHANNEL_TEMPERATURE = "temperature"
CHANNELS = (CHANNEL_GAUGE, CHANNEL_VIBRATION, CHANNEL_TEMPERATURE)

# --- status codes shared with sketch/sketch.ino -----------------------------
STATUS_BOOT = 0
STATUS_SCANNING = 1
STATUS_UNCALIBRATED = 2
STATUS_OK = 3
STATUS_WATCH = 4
STATUS_ALARM = 5

STATUS_NAMES = {
    STATUS_BOOT: "BOOT",
    STATUS_SCANNING: "SCANNING",
    STATUS_UNCALIBRATED: "UNCALIBRATED",
    STATUS_OK: "OK",
    STATUS_WATCH: "WATCH",
    STATUS_ALARM: "ALARM",
}

# Worst-wins ordering when several channels disagree.
STATUS_RANK = {"OK": 0, "WATCH": 1, "ALARM": 2}
STATUS_CODE_FOR = {"OK": STATUS_OK, "WATCH": STATUS_WATCH, "ALARM": STATUS_ALARM}


def norm360(angle: float) -> float:
    """Wrap an angle into [0, 360)."""
    return angle % 360.0


def _opt_float(raw: dict[str, Any], key: str) -> Optional[float]:
    value = raw.get(key)
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _float(raw: dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


@dataclass
class Limits:
    """Warning and alarm bands for one channel. Any bound may be None (not checked)."""

    unit: str = ""
    warn_low: Optional[float] = None
    warn_high: Optional[float] = None
    alarm_low: Optional[float] = None
    alarm_high: Optional[float] = None

    def range_state(self, value: float) -> str:
        if self.alarm_low is not None and value < self.alarm_low:
            return "alarm_low"
        if self.alarm_high is not None and value > self.alarm_high:
            return "alarm_high"
        if self.warn_low is not None and value < self.warn_low:
            return "warn_low"
        if self.warn_high is not None and value > self.warn_high:
            return "warn_high"
        return "normal"

    def bound_for(self, state: str) -> Optional[float]:
        return {
            "alarm_low": self.alarm_low,
            "alarm_high": self.alarm_high,
            "warn_low": self.warn_low,
            "warn_high": self.warn_high,
        }.get(state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "warn_low": self.warn_low,
            "warn_high": self.warn_high,
            "alarm_low": self.alarm_low,
            "alarm_high": self.alarm_high,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], unit: str = "") -> "Limits":
        raw = raw or {}
        return cls(
            unit=str(raw.get("unit") or unit),
            warn_low=_opt_float(raw, "warn_low"),
            warn_high=_opt_float(raw, "warn_high"),
            alarm_low=_opt_float(raw, "alarm_low"),
            alarm_high=_opt_float(raw, "alarm_high"),
        )


@dataclass
class GaugeCalibration:
    """Dial geometry (in tag units / tag-frame degrees) plus its scale."""

    center_x: float
    center_y: float
    radius: float
    angle_min_deg: float
    angle_max_deg: float
    value_min: float
    value_max: float
    unit: str = ""
    r_inner_frac: float = 0.25
    r_outer_frac: float = 0.92
    needle_dark: bool = True
    limits: Limits = field(default_factory=Limits)

    # -- geometry -----------------------------------------------------------
    @property
    def center(self) -> tuple[float, float]:
        return (self.center_x, self.center_y)

    @property
    def sweep_deg(self) -> float:
        """Clockwise travel from the minimum to the maximum mark."""
        sweep = norm360(self.angle_max_deg - self.angle_min_deg)
        return sweep if sweep > 1e-6 else 360.0

    def value_from_angle(self, angle_deg: float) -> tuple[float, float, bool]:
        """(value, fraction of scale, is_on_scale) for a needle angle.

        The arc between the max and min marks is the dial's dead zone; a needle landing
        there reads as slightly under- or over-range depending on which side it sits,
        rather than wrapping around to the other end of the scale.
        """
        sweep = self.sweep_deg
        delta = norm360(angle_deg - self.angle_min_deg)
        dead_mid = sweep + (360.0 - sweep) / 2.0
        if delta > dead_mid:
            delta -= 360.0
        t = delta / sweep
        return self.value_min + t * (self.value_max - self.value_min), t, (-0.02 <= t <= 1.02)

    def angle_from_value(self, value: float) -> float:
        span = self.value_max - self.value_min
        t = 0.0 if abs(span) < 1e-9 else (value - self.value_min) / span
        return norm360(self.angle_min_deg + t * self.sweep_deg)

    def range_state(self, value: float) -> str:
        return self.limits.range_state(value)

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "center_x": self.center_x,
            "center_y": self.center_y,
            "radius": self.radius,
            "angle_min_deg": self.angle_min_deg,
            "angle_max_deg": self.angle_max_deg,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "unit": self.unit,
            "r_inner_frac": self.r_inner_frac,
            "r_outer_frac": self.r_outer_frac,
            "needle_dark": self.needle_dark,
            "limits": self.limits.to_dict(),
            "sweep_deg": self.sweep_deg,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GaugeCalibration":
        unit = str(raw.get("unit") or "")
        # Accept both the nested {"limits": {...}} form and the flat warn_/alarm_ keys
        # written by the first version of this app.
        limits_raw = raw.get("limits") if isinstance(raw.get("limits"), dict) else raw
        return cls(
            center_x=_float(raw, "center_x", 0.5),
            center_y=_float(raw, "center_y", -1.0),
            radius=_float(raw, "radius", 1.0),
            angle_min_deg=norm360(_float(raw, "angle_min_deg", 225.0)),
            angle_max_deg=norm360(_float(raw, "angle_max_deg", 135.0)),
            value_min=_float(raw, "value_min", 0.0),
            value_max=_float(raw, "value_max", 100.0),
            unit=unit,
            r_inner_frac=max(0.02, min(0.9, _float(raw, "r_inner_frac", 0.25))),
            r_outer_frac=max(0.1, min(1.4, _float(raw, "r_outer_frac", 0.92))),
            needle_dark=bool(raw.get("needle_dark", True)),
            limits=Limits.from_dict(limits_raw, unit),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.radius <= 1e-3:
            problems.append("dial radius is zero - click the centre and the rim further apart")
        if abs(self.value_max - self.value_min) < 1e-9:
            problems.append("value_min and value_max are equal")
        if self.r_outer_frac <= self.r_inner_frac:
            problems.append("r_outer_frac must be greater than r_inner_frac")
        if self.sweep_deg < 15.0:
            problems.append("the min and max needle positions are less than 15 apart")
        if not math.isfinite(self.center_x) or not math.isfinite(self.center_y):
            problems.append("dial centre is not a finite point")
        return problems


# The first release called this Calibration; keep the old name working.
Calibration = GaugeCalibration


@dataclass
class VibrationConfig:
    """Vibration channel: RMS deviation of the time-of-flight distance, in mm."""

    limits: Limits = field(default_factory=lambda: Limits(unit="mm"))
    # Which number from the MCU summary gets modelled and alarmed on.
    metric: str = "rms"                     # rms | peak_to_peak
    settle_s: float = VIBRATION_SETTLE_S
    # Distance readings outside this window are the wrong target, not the machine.
    min_distance_mm: Optional[float] = 30.0
    max_distance_mm: Optional[float] = 2000.0

    METRICS = ("rms", "peak_to_peak")

    def value_from(self, sample: dict[str, Any]) -> Optional[float]:
        value = sample.get("rms" if self.metric == "rms" else "peak_to_peak")
        return None if value is None else float(value)

    def distance_ok(self, mean_mm: Optional[float]) -> bool:
        if mean_mm is None:
            return False
        if self.min_distance_mm is not None and mean_mm < self.min_distance_mm:
            return False
        if self.max_distance_mm is not None and mean_mm > self.max_distance_mm:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_dict(),
            "metric": self.metric,
            "settle_s": self.settle_s,
            "min_distance_mm": self.min_distance_mm,
            "max_distance_mm": self.max_distance_mm,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VibrationConfig":
        raw = raw or {}
        metric = str(raw.get("metric") or "rms")
        return cls(
            limits=Limits.from_dict(raw.get("limits") or {}, "mm"),
            metric=metric if metric in cls.METRICS else "rms",
            settle_s=_float(raw, "settle_s", VIBRATION_SETTLE_S),
            min_distance_mm=_opt_float(raw, "min_distance_mm"),
            max_distance_mm=_opt_float(raw, "max_distance_mm"),
        )


@dataclass
class TemperatureConfig:
    """Temperature channel: one statistic pulled out of the 32x24 thermal frame."""

    limits: Limits = field(default_factory=lambda: Limits(unit="C"))
    metric: str = "max"                     # max | mean | min | delta_ambient

    METRICS = ("max", "mean", "min", "delta_ambient")

    def value_from(self, sample: dict[str, Any]) -> Optional[float]:
        if self.metric == "delta_ambient":
            hot, ambient = sample.get("max"), sample.get("ambient")
            if hot is None or ambient is None:
                return None
            return float(hot) - float(ambient)
        value = sample.get(self.metric)
        return None if value is None else float(value)

    def to_dict(self) -> dict[str, Any]:
        return {"limits": self.limits.to_dict(), "metric": self.metric}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TemperatureConfig":
        raw = raw or {}
        metric = str(raw.get("metric") or "max")
        return cls(
            limits=Limits.from_dict(raw.get("limits") or {}, "C"),
            metric=metric if metric in cls.METRICS else "max",
        )


@dataclass
class AssetConfig:
    """One tagged thing and the channels it is monitored on."""

    asset_id: str
    tag_id: int
    label: str = ""
    gauge: Optional[GaugeCalibration] = None
    vibration: Optional[VibrationConfig] = None
    temperature: Optional[TemperatureConfig] = None
    updated: str = ""

    def channels(self) -> list[str]:
        enabled = []
        if self.gauge is not None:
            enabled.append(CHANNEL_GAUGE)
        if self.vibration is not None:
            enabled.append(CHANNEL_VIBRATION)
        if self.temperature is not None:
            enabled.append(CHANNEL_TEMPERATURE)
        return enabled

    def limits_for(self, channel: str) -> Limits:
        if channel == CHANNEL_GAUGE and self.gauge is not None:
            return self.gauge.limits
        if channel == CHANNEL_VIBRATION and self.vibration is not None:
            return self.vibration.limits
        if channel == CHANNEL_TEMPERATURE and self.temperature is not None:
            return self.temperature.limits
        return Limits()

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "tag_id": self.tag_id,
            "label": self.label,
            "channels": self.channels(),
            "gauge": self.gauge.to_dict() if self.gauge else None,
            "vibration": self.vibration.to_dict() if self.vibration else None,
            "temperature": self.temperature.to_dict() if self.temperature else None,
            "updated": self.updated,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetConfig":
        # Accept the v1 shape too, where the whole record *was* the gauge calibration.
        asset_id = raw.get("asset_id") or raw.get("gauge_id") or "asset"
        gauge_raw = raw.get("gauge")
        if gauge_raw is None and "center_x" in raw:
            gauge_raw = raw
        return cls(
            asset_id=str(asset_id),
            tag_id=int(raw.get("tag_id", 0) or 0),
            label=str(raw.get("label") or ""),
            gauge=GaugeCalibration.from_dict(gauge_raw) if gauge_raw else None,
            vibration=VibrationConfig.from_dict(raw["vibration"]) if raw.get("vibration") else None,
            temperature=(
                TemperatureConfig.from_dict(raw["temperature"]) if raw.get("temperature") else None
            ),
            updated=str(raw.get("updated") or ""),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.asset_id.strip():
            problems.append("asset id is empty")
        if not self.channels():
            problems.append("enable at least one channel (gauge, vibration or temperature)")
        if self.gauge is not None:
            problems.extend(self.gauge.validate())
        return problems
