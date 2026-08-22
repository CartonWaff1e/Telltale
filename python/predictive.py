# Predictive maintenance over the reading history.
#
# Three signals feed one status:
#   1. range   - the value against the calibrated warn/alarm bands
#   2. anomaly - IsolationForest over [value, delta, rolling mean, rolling std, local slope]
#   3. trend   - least-squares fit of the recent window, extrapolated to the alarm band
#
# scikit-learn is installed from python/requirements.txt at app start. If that install
# didn't happen (no network on first boot, say) we fall back to a median/MAD z-score so
# the app keeps working and the UI says which backend is live.

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import numpy as np

import config

log = logging.getLogger(__name__)

try:
    from sklearn.ensemble import IsolationForest

    SKLEARN_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - depends on the board's install
    IsolationForest = None  # type: ignore[assignment]
    SKLEARN_AVAILABLE = False
    log.warning("scikit-learn unavailable (%s); using the robust z-score fallback", _exc)


@dataclass
class Assessment:
    status: str = "OK"                     # OK | WATCH | ALARM
    status_code: int = config.STATUS_OK
    range_state: str = "normal"
    anomaly_score: Optional[float] = None  # < 0 is anomalous for IsolationForest
    is_anomaly: bool = False
    trend_per_hour: Optional[float] = None
    trend_r2: Optional[float] = None
    hours_to_limit: Optional[float] = None
    limit_name: str = ""
    model_ready: bool = False
    n_samples: int = 0
    backend: str = "none"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_hours(hours: float) -> str:
    if hours < 1.0:
        return f"{hours * 60.0:.0f} min"
    if hours < 48.0:
        return f"{hours:.1f} h"
    return f"{hours / 24.0:.1f} days"


class PredictiveMaintenance:
    """One instance per gauge. Not thread-safe; the app calls it from a single thread."""

    def __init__(
        self,
        min_samples: int = config.PDM_MIN_SAMPLES,
        refit_every: int = config.PDM_REFIT_EVERY,
        roll_window: int = config.PDM_ROLL_WINDOW,
        trend_window: int = config.PDM_TREND_WINDOW,
        contamination: float = config.PDM_CONTAMINATION,
    ):
        self.min_samples = max(10, min_samples)
        self.refit_every = max(1, refit_every)
        self.roll_window = max(3, roll_window)
        self.trend_window = max(4, trend_window)
        self.contamination = contamination

        self._times: list[float] = []
        self._values: list[float] = []
        self._model = None
        self._since_fit = 0
        self.backend = "IsolationForest" if SKLEARN_AVAILABLE else "robust-z"

    # -- history ------------------------------------------------------------
    def bootstrap(self, history: list[tuple[float, float]]) -> None:
        """Seed from stored readings (oldest first) without emitting assessments."""
        for ts, value in history[-config.PDM_HISTORY_LIMIT:]:
            if math.isfinite(ts) and math.isfinite(value):
                self._times.append(float(ts))
                self._values.append(float(value))
        self._fit()

    @property
    def n_samples(self) -> int:
        return len(self._values)

    # -- features -----------------------------------------------------------
    def _features_at(self, i: int) -> list[float]:
        values = self._values
        times = self._times
        lo = max(0, i - self.roll_window + 1)
        window = np.asarray(values[lo : i + 1], dtype=float)
        delta = values[i] - values[i - 1] if i > 0 else 0.0
        dt_h = (times[i] - times[i - 1]) / 3600.0 if i > 0 else 0.0
        rate = delta / dt_h if dt_h > 1e-6 else 0.0
        # Clamp the rate: a long gap between readings shouldn't produce a huge feature.
        rate = float(np.clip(rate, -1e6, 1e6))
        return [
            float(values[i]),
            float(delta),
            float(window.mean()),
            float(window.std()),
            rate,
        ]

    def _feature_matrix(self) -> np.ndarray:
        return np.asarray([self._features_at(i) for i in range(len(self._values))], dtype=float)

    def _fit(self) -> None:
        if not SKLEARN_AVAILABLE or len(self._values) < self.min_samples:
            return
        try:
            X = self._feature_matrix()
            model = IsolationForest(
                n_estimators=150,
                max_samples=min(256, len(X)),
                contamination=self.contamination,
                random_state=42,
                n_jobs=1,
            )
            model.fit(X)
            self._model = model
            self._since_fit = 0
            log.info("IsolationForest refitted on %d samples", len(X))
        except Exception as exc:
            log.error("IsolationForest fit failed, keeping previous model: %s", exc)

    # -- anomaly ------------------------------------------------------------
    def _anomaly(self) -> tuple[Optional[float], bool]:
        n = len(self._values)
        if n < self.min_samples:
            return None, False

        if self._model is not None:
            try:
                x = np.asarray([self._features_at(n - 1)], dtype=float)
                score = float(self._model.decision_function(x)[0])
                return score, bool(score < 0.0)
            except Exception as exc:
                log.error("IsolationForest scoring failed: %s", exc)

        # Fallback: robust z-score on the value itself.
        arr = np.asarray(self._values[:-1], dtype=float)
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median)))
        scale = 1.4826 * mad
        if scale < 1e-9:
            return None, False
        z = (self._values[-1] - median) / scale
        # Report it on the same "negative is anomalous" convention as decision_function.
        return float(3.5 - abs(z)), bool(abs(z) > 3.5)

    # -- trend --------------------------------------------------------------
    def _trend(self) -> tuple[Optional[float], Optional[float]]:
        n = len(self._values)
        if n < 4:
            return None, None
        k = min(self.trend_window, n)
        t = np.asarray(self._times[-k:], dtype=float)
        y = np.asarray(self._values[-k:], dtype=float)
        t_h = (t - t[-1]) / 3600.0
        if float(np.ptp(t_h)) < 1e-6:
            return None, None
        try:
            slope, intercept = np.polyfit(t_h, y, 1)
        except Exception:
            return None, None
        pred = slope * t_h + intercept
        ss_res = float(np.sum((y - pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
        return float(slope), float(r2)

    @staticmethod
    def _forecast(value: float, slope: float, limits: config.Limits
                  ) -> tuple[Optional[float], str]:
        """Hours until the current trend carries the value into an alarm band."""
        if slope is None or abs(slope) < 1e-9:
            return None, ""
        if slope > 0 and limits.alarm_high is not None and value < limits.alarm_high:
            return (limits.alarm_high - value) / slope, "alarm_high"
        if slope < 0 and limits.alarm_low is not None and value > limits.alarm_low:
            return (limits.alarm_low - value) / slope, "alarm_low"
        return None, ""

    # -- main entry point ---------------------------------------------------
    def update(self, ts_epoch: float, value: float, limits: config.Limits) -> Assessment:
        self._times.append(float(ts_epoch))
        self._values.append(float(value))
        self._since_fit += 1
        if SKLEARN_AVAILABLE and (
            self._model is None or self._since_fit >= self.refit_every
        ) and len(self._values) >= self.min_samples:
            self._fit()

        assessment = Assessment(
            n_samples=len(self._values),
            backend=self.backend if SKLEARN_AVAILABLE else "robust-z",
            model_ready=self._model is not None or (
                not SKLEARN_AVAILABLE and len(self._values) >= self.min_samples
            ),
        )

        assessment.range_state = limits.range_state(value)
        assessment.anomaly_score, assessment.is_anomaly = self._anomaly()
        slope, r2 = self._trend()
        assessment.trend_per_hour, assessment.trend_r2 = slope, r2

        if slope is not None and r2 is not None and r2 >= config.PDM_TREND_MIN_R2:
            hours, limit_name = self._forecast(value, slope, limits)
            if hours is not None and math.isfinite(hours) and 0 < hours < 24 * 365:
                assessment.hours_to_limit = float(hours)
                assessment.limit_name = limit_name

        reasons: list[str] = []
        unit = f" {limits.unit}".rstrip()
        if assessment.range_state != "normal":
            side = "above" if assessment.range_state.endswith("high") else "below"
            kind = "alarm" if assessment.range_state.startswith("alarm") else "warning"
            bound = limits.bound_for(assessment.range_state)
            if bound is not None:
                reasons.append(
                    f"{value:.2f}{unit} is {side} the {kind} limit of {bound:.2f}{unit}"
                )

        if assessment.is_anomaly:
            reasons.append(
                f"{assessment.backend} flagged this reading as unlike the previous "
                f"{assessment.n_samples - 1}"
            )
        if assessment.hours_to_limit is not None:
            reasons.append(
                f"trend of {slope:+.3f}{unit}/h reaches the "
                f"{assessment.limit_name.replace('_', ' ')} limit in "
                f"{_fmt_hours(assessment.hours_to_limit)}"
            )

        if assessment.range_state.startswith("alarm"):
            assessment.status, assessment.status_code = "ALARM", config.STATUS_ALARM
        elif (
            assessment.range_state.startswith("warn")
            or assessment.is_anomaly
            or (assessment.hours_to_limit is not None
                and assessment.hours_to_limit <= config.PDM_WATCH_HORIZON_H)
        ):
            assessment.status, assessment.status_code = "WATCH", config.STATUS_WATCH
        else:
            assessment.status, assessment.status_code = "OK", config.STATUS_OK

        if not reasons:
            reasons.append("value inside limits, no trend or anomaly detected")
        assessment.reasons = reasons
        return assessment

    def reset(self) -> None:
        self._times.clear()
        self._values.clear()
        self._model = None
        self._since_fit = 0
