# Telltale - scan for an AprilTag, then measure whatever that tag is assigned to.
#
# A tag identifies an *asset*, and an asset enables any combination of three channels:
#
#   gauge       camera  -> needle angle -> engineering value
#   vibration   MCU     -> time-of-flight RMS wobble (mm)
#   temperature MCU     -> thermal-array statistic (C)
#
# Each channel keeps its own history, its own limits and its own IsolationForest. The
# asset's status is the worst of them. A channel whose sensor isn't plugged in is simply
# skipped - the others carry on.

from __future__ import annotations

import base64
import json
import logging
import math
import threading
import time
from typing import Any, Optional

import cv2
import numpy as np
from fastapi import Response

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI
from arduino.app_peripherals.camera import Camera

import config
import predictive
import sensors as sensors_mod
import store
import vision

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("telltale")

SEVERITY_FOR = {"OK": "info", "WATCH": "warning", "ALARM": "error"}


class GaugeApp:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at = time.monotonic()

        self.store = store.Store()
        self.locator = vision.TagLocator(config.TAG_FAMILY)
        self.assets: dict[str, config.AssetConfig] = self.store.load_assets()
        self.sensors = sensors_mod.SensorHub()
        self.sensors.start()

        # One predictive model per (asset, channel).
        self.pdm: dict[tuple[str, str], predictive.PredictiveMaintenance] = {}
        for asset in self.assets.values():
            for channel in asset.channels():
                self._ensure_pdm(asset.asset_id, channel)

        self.camera: Optional[Any] = None
        self.camera_error: Optional[str] = None
        self._camera_retry_at = 0.0

        # Live state, guarded by self._lock.
        self._raw_frame: Optional[np.ndarray] = None
        self._raw_shape = (0, 0)
        self._preview_jpeg: Optional[bytes] = None
        self._preview_at = 0.0
        self._tags: list[vision.TagDetection] = []
        self._stability: dict[int, dict[str, Any]] = {}
        self._live_reading: Optional[vision.GaugeReading] = None
        self._live_at = 0.0
        self._active_asset: Optional[str] = None
        self._latest: dict[str, dict[str, dict[str, Any]]] = {}
        self._assessment: dict[str, dict[str, predictive.Assessment]] = {}
        self._channel_note: dict[str, dict[str, str]] = {}
        self._asset_status: dict[str, str] = {}
        self._sensor_missing: set[tuple[str, str]] = set()
        self._last_capture: dict[str, float] = {}
        self._manual_capture = False
        self._status = config.STATUS_BOOT
        self._notified_status: Optional[int] = None
        self._notified_at = 0.0

        self.ui = WebUI()
        self._register_api()
        log.info(
            "telltale ready - %d asset(s), anomaly backend=%s",
            len(self.assets),
            "IsolationForest" if predictive.SKLEARN_AVAILABLE else "robust-z (scikit-learn missing)",
        )

    # ------------------------------------------------------------------ camera
    def _open_camera(self) -> None:
        now = time.monotonic()
        if now < self._camera_retry_at:
            return
        self._camera_retry_at = now + config.CAMERA_RETRY_S
        try:
            camera = Camera(
                source=config.CAMERA_SOURCE,
                resolution=(config.CAMERA_WIDTH, config.CAMERA_HEIGHT),
                fps=config.CAMERA_FPS,
            )
            camera.start()
            self.camera = camera
            self.camera_error = None
            log.info("camera started at %dx%d", config.CAMERA_WIDTH, config.CAMERA_HEIGHT)
        except Exception as exc:
            self.camera = None
            self.camera_error = str(exc)
            log.error("camera unavailable, retrying in %.0fs: %s", config.CAMERA_RETRY_S, exc)

    # ------------------------------------------------------------- bookkeeping
    def _ensure_pdm(self, asset_id: str, channel: str) -> predictive.PredictiveMaintenance:
        key = (asset_id, channel)
        model = self.pdm.get(key)
        if model is None:
            model = predictive.PredictiveMaintenance()
            history = self.store.history_values(asset_id, channel)
            if history:
                model.bootstrap(history)
                log.info("seeded %s/%s model with %d readings", asset_id, channel, len(history))
            self.pdm[key] = model
        return model

    def _asset_for_tag(self, tag_id: int) -> Optional[config.AssetConfig]:
        for asset in self.assets.values():
            if asset.tag_id == tag_id:
                return asset
        return None

    def _track_stability(self, tags: list[vision.TagDetection]) -> None:
        now = time.monotonic()
        seen = set()
        for tag in tags:
            if tag.edge_px < config.MIN_TAG_EDGE_PX:
                continue
            seen.add(tag.tag_id)
            prev = self._stability.get(tag.tag_id)
            if prev is not None:
                shift = float(np.max(np.linalg.norm(tag.corners - prev["corners"], axis=1)))
                holding = shift <= config.STABLE_MAX_SHIFT_PX
                count = prev["count"] + 1 if holding else 1
                since = prev["since"] if holding else now
            else:
                shift, count, since = 0.0, 1, now
            self._stability[tag.tag_id] = {
                "corners": tag.corners.copy(),
                "count": count,
                "shift": shift,
                "since": since,
            }
        for tag_id in list(self._stability):
            if tag_id not in seen:
                del self._stability[tag_id]

    def _held_for(self, tag_id: int) -> float:
        entry = self._stability.get(tag_id)
        return 0.0 if entry is None else time.monotonic() - entry["since"]

    # ------------------------------------------------------------------- loop
    def loop(self) -> None:
        try:
            self._tick()
        except Exception as exc:
            log.exception("loop iteration failed: %s", exc)
            time.sleep(1.0)

    def _tick(self) -> None:
        if self.camera is None:
            self._open_camera()
            with self._lock:
                self._status = config.STATUS_SCANNING
            time.sleep(0.5)
            return

        frame = self.camera.capture()
        if frame is None or getattr(frame, "size", 0) == 0:
            time.sleep(0.1)
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tags = self.locator.detect(gray)

        with self._lock:
            self._raw_frame = frame
            self._raw_shape = (frame.shape[1], frame.shape[0])
            self._tags = tags
            self._track_stability(tags)

            target: Optional[vision.TagDetection] = None
            asset: Optional[config.AssetConfig] = None
            for tag in tags:  # already sorted largest-first
                candidate = self._asset_for_tag(tag.tag_id)
                if candidate is not None:
                    target, asset = tag, candidate
                    break
            if target is None and tags:
                target = tags[0]
            if asset is not None:
                self._active_asset = asset.asset_id

        now = time.monotonic()
        reading: Optional[vision.GaugeReading] = None
        gauge_cal = asset.gauge if asset else None

        if target is not None and gauge_cal is not None:
            homography = target.homography
            if now - self._live_at >= 0.3:
                reading = vision.read_gauge(gray, homography, gauge_cal)
                with self._lock:
                    self._live_reading = reading
                    self._live_at = now
            else:
                with self._lock:
                    reading = self._live_reading
        else:
            with self._lock:
                self._live_reading = None

        manual = self._take_manual_flag()
        if target is not None and asset is not None:
            stable = self._stability.get(target.tag_id, {}).get("count", 0)
            due = now - self._last_capture.get(asset.asset_id, -1e9) >= config.MIN_CAPTURE_INTERVAL_S
            if manual or (stable >= config.STABLE_FRAMES and due):
                self._capture(frame, gray, target, asset, manual)

        self._update_status(target, asset)
        self._update_preview(frame, tags, asset, reading)
        self._notify_mcu()

    def _take_manual_flag(self) -> bool:
        with self._lock:
            manual, self._manual_capture = self._manual_capture, False
        return manual

    def _update_status(self, target: Optional[vision.TagDetection],
                       asset: Optional[config.AssetConfig]) -> None:
        with self._lock:
            if target is None:
                self._status = config.STATUS_SCANNING
            elif asset is None:
                self._status = config.STATUS_UNCALIBRATED
            else:
                status = self._asset_status.get(asset.asset_id, "OK")
                self._status = config.STATUS_CODE_FOR.get(status, config.STATUS_OK)

    # ---------------------------------------------------------------- capture
    def _capture(self, frame: np.ndarray, gray: np.ndarray, tag: vision.TagDetection,
                 asset: config.AssetConfig, manual: bool) -> None:
        ts, epoch = store.utc_now()
        statuses: list[str] = []
        notes: dict[str, str] = {}
        stored_any = False

        for channel in asset.channels():
            measurement = self._measure(channel, asset, tag, frame, gray)
            if measurement is None:
                notes[channel] = self._unavailable_note(channel, asset)
                self._flag_sensor_missing(asset, channel)
                continue
            self._clear_sensor_missing(asset, channel)
            notes[channel] = ""
            statuses.append(self._record(ts, epoch, asset, channel, tag, measurement))
            stored_any = True

        overall = "OK"
        for status in statuses:
            if config.STATUS_RANK[status] > config.STATUS_RANK[overall]:
                overall = status

        with self._lock:
            self._channel_note[asset.asset_id] = notes
            if stored_any:
                self._last_capture[asset.asset_id] = time.monotonic()
                previous = self._asset_status.get(asset.asset_id)
                self._asset_status[asset.asset_id] = overall
            else:
                previous = None
                # Nothing could be measured; back off so we don't spin on every frame.
                self._last_capture[asset.asset_id] = time.monotonic()

        if stored_any and previous is not None and previous != overall:
            self.store.add_event(
                asset.asset_id, SEVERITY_FOR[overall], f"asset_{overall.lower()}",
                f"{asset.asset_id} moved from {previous} to {overall}",
            )
        if manual and not stored_any:
            log.warning("manual capture on %s measured nothing: %s", asset.asset_id, notes)

    def _measure(self, channel: str, asset: config.AssetConfig, tag: vision.TagDetection,
                 frame: np.ndarray, gray: np.ndarray) -> Optional[dict[str, Any]]:
        """One channel's raw measurement, or None if it can't be taken right now."""
        if channel == config.CHANNEL_GAUGE:
            return self._measure_gauge(asset, tag, frame, gray)
        if channel == config.CHANNEL_VIBRATION:
            return self._measure_vibration(asset, tag)
        if channel == config.CHANNEL_TEMPERATURE:
            return self._measure_temperature(asset)
        return None

    def _measure_gauge(self, asset: config.AssetConfig, tag: vision.TagDetection,
                       frame: np.ndarray, gray: np.ndarray) -> Optional[dict[str, Any]]:
        cal = asset.gauge
        if cal is None:
            return None
        reading = vision.read_gauge(gray, tag.homography, cal)
        if reading is None:
            return None

        valid = reading.on_scale and reading.confidence >= config.MIN_CONFIDENCE
        reason = ""
        if not reading.on_scale:
            reason = "needle outside the calibrated arc"
        elif not valid:
            reason = (f"confidence {reading.confidence:.2f} below "
                      f"{config.MIN_CONFIDENCE:.2f}")

        overlay = vision.draw_overlay(
            frame, [tag], cal, reading, config.STATUS_OK,
            banner=f"{asset.asset_id}  {reading.value:.2f} {cal.unit}".strip(),
        )
        crop = vision.crop_dial(overlay, reading)
        ok, encoded = cv2.imencode(
            ".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), config.DIAL_JPEG_QUALITY]
        )
        image_b64 = base64.b64encode(encoded.tobytes()).decode("ascii") if ok else None

        return {
            "value": float(reading.value),
            "unit": cal.unit,
            "confidence": float(reading.confidence),
            "valid": valid,
            "invalid_reason": reason,
            "detail": {
                "angle_deg": round(reading.angle_deg, 2),
                "fraction": round(reading.fraction, 4),
                "on_scale": reading.on_scale,
                "ambiguous": reading.ambiguous,
                "notes": reading.notes,
            },
            "image": image_b64,
            "image_type": "image/jpeg" if image_b64 else None,
        }

    def _measure_vibration(self, asset: config.AssetConfig,
                           tag: vision.TagDetection) -> Optional[dict[str, Any]]:
        cfg = asset.vibration
        if cfg is None:
            return None
        sample = self.sensors.vibration_sample()
        if sample is None:
            return None
        # Measure the machine's wobble, not the rig still settling into place.
        if self._held_for(tag.tag_id) < cfg.settle_s:
            return None
        value = cfg.value_from(sample)
        if value is None or not math.isfinite(value):
            return None

        in_range = cfg.distance_ok(sample.get("mean_mm"))
        return {
            "value": float(value),
            "unit": cfg.limits.unit or "mm",
            "confidence": 1.0 if in_range else 0.0,
            "valid": in_range and sample.get("samples", 0) > 0,
            "invalid_reason": "" if in_range else (
                f"stand-off {sample.get('mean_mm')} mm is outside the expected "
                f"{cfg.min_distance_mm}-{cfg.max_distance_mm} mm window"
            ),
            "detail": {
                "metric": cfg.metric,
                "rms_mm": round(float(sample["rms"]), 4),
                "peak_to_peak_mm": round(float(sample["peak_to_peak"]), 4),
                "mean_mm": round(float(sample["mean_mm"]), 2),
                "dominant_hz": round(float(sample["dominant_hz"]), 2),
                "samples": int(sample["samples"]),
                "age_s": sample.get("age_s"),
            },
            "image": None,
            "image_type": None,
        }

    def _measure_temperature(self, asset: config.AssetConfig) -> Optional[dict[str, Any]]:
        cfg = asset.temperature
        if cfg is None:
            return None
        sample = self.sensors.temperature_sample()
        if sample is None:
            return None
        value = cfg.value_from(sample)
        if value is None or not math.isfinite(value):
            return None
        return {
            "value": float(value),
            "unit": cfg.limits.unit or "C",
            "confidence": 1.0,
            "valid": True,
            "invalid_reason": "",
            "detail": {
                "metric": cfg.metric,
                "min_c": round(float(sample["min"]), 2),
                "mean_c": round(float(sample["mean"]), 2),
                "max_c": round(float(sample["max"]), 2),
                "ambient_c": round(float(sample["ambient"]), 2),
                "hot_x": sample["hot_x"],
                "hot_y": sample["hot_y"],
                "age_s": sample.get("age_s"),
            },
            "image": None,
            "image_type": None,
        }

    def _unavailable_note(self, channel: str, asset: config.AssetConfig) -> str:
        state = self.sensors.state()
        if channel == config.CHANNEL_GAUGE:
            return "the dial could not be sampled - is the whole gauge in frame?"
        if channel == config.CHANNEL_VIBRATION:
            if not state["tof"]["detected"]:
                return "no time-of-flight sensor detected on the MCU I2C bus"
            if not state["tof"]["reporting"]:
                return "the time-of-flight sensor stopped reporting"
            cfg = asset.vibration
            settle = cfg.settle_s if cfg else config.VIBRATION_SETTLE_S
            return f"waiting for the tag to be held still for {settle:.1f} s"
        if channel == config.CHANNEL_TEMPERATURE:
            if not state["thermal"]["detected"]:
                return "no thermal array detected on the MCU I2C bus"
            return "the thermal array stopped reporting"
        return "unavailable"

    def _flag_sensor_missing(self, asset: config.AssetConfig, channel: str) -> None:
        key = (asset.asset_id, channel)
        if key in self._sensor_missing:
            return
        self._sensor_missing.add(key)
        self.store.add_event(
            asset.asset_id, "warning", "channel_unavailable",
            f"{channel} channel skipped: {self._unavailable_note(channel, asset)}",
            channel=channel,
        )

    def _clear_sensor_missing(self, asset: config.AssetConfig, channel: str) -> None:
        key = (asset.asset_id, channel)
        if key in self._sensor_missing:
            self._sensor_missing.discard(key)
            self.store.add_event(
                asset.asset_id, "info", "channel_available",
                f"{channel} channel is reporting again", channel=channel,
            )

    def _record(self, ts: str, epoch: float, asset: config.AssetConfig, channel: str,
                tag: vision.TagDetection, measurement: dict[str, Any]) -> str:
        limits = asset.limits_for(channel)
        model = self._ensure_pdm(asset.asset_id, channel)
        value = measurement["value"]

        if measurement["valid"]:
            assessment = model.update(epoch, value, limits)
        else:
            # A measurement we can't trust must not train the anomaly model or drag the
            # trend around; it is still stored so the gap is visible.
            assessment = predictive.Assessment(
                status="WATCH",
                status_code=config.STATUS_WATCH,
                range_state="unknown",
                n_samples=model.n_samples,
                backend=model.backend,
                reasons=[f"reading rejected: {measurement['invalid_reason']}"],
            )

        row = {
            "ts": ts,
            "ts_epoch": epoch,
            "asset_id": asset.asset_id,
            "tag_id": int(tag.tag_id),
            "channel": channel,
            "value": float(value),
            "unit": measurement["unit"],
            "confidence": float(measurement["confidence"]),
            "valid": 1 if measurement["valid"] else 0,
            "status": assessment.status,
            "range_state": assessment.range_state,
            "anomaly_score": assessment.anomaly_score,
            "is_anomaly": 1 if assessment.is_anomaly else 0,
            "trend_per_hour": assessment.trend_per_hour,
            "trend_r2": assessment.trend_r2,
            "hours_to_limit": assessment.hours_to_limit,
            "reasons": " | ".join(assessment.reasons),
            "detail": json.dumps(measurement["detail"]),
            "image": measurement.get("image"),
            "image_type": measurement.get("image_type"),
        }
        self.store.add_reading(row)
        if measurement.get("image"):
            self.store.prune_images()

        previous = self._assessment.get(asset.asset_id, {}).get(channel)
        if previous is None or previous.status != assessment.status:
            self.store.add_event(
                asset.asset_id, SEVERITY_FOR[assessment.status],
                f"{channel}_{assessment.status.lower()}",
                f"{channel} {assessment.status}: {assessment.reasons[0]}", channel=channel,
            )

        with self._lock:
            self._assessment.setdefault(asset.asset_id, {})[channel] = assessment
            public = {k: v for k, v in row.items() if k != "image"}
            public["has_image"] = bool(measurement.get("image"))
            public["detail"] = measurement["detail"]
            self._latest.setdefault(asset.asset_id, {})[channel] = public

        log.info(
            "%s/%s: %.3f %s (%s%s)",
            asset.asset_id, channel, value, measurement["unit"], assessment.status,
            "" if measurement["valid"] else ", rejected",
        )
        return assessment.status

    # ---------------------------------------------------------------- preview
    def _update_preview(self, frame: np.ndarray, tags: list[vision.TagDetection],
                        asset: Optional[config.AssetConfig],
                        reading: Optional[vision.GaugeReading]) -> None:
        now = time.monotonic()
        if now - self._preview_at < config.PREVIEW_PERIOD_S:
            return
        with self._lock:
            status = self._status
        cal = asset.gauge if asset else None

        if asset is not None:
            channels = "+".join(asset.channels())
            banner = f"{asset.asset_id} [{channels}]"
            if cal is not None and reading is not None:
                banner += f"  {reading.value:.2f} {cal.unit}".rstrip()
        elif tags:
            banner = f"tag {tags[0].tag_id} is not assigned to an asset yet"
        else:
            banner = "scanning for AprilTag..."

        annotated = vision.draw_overlay(frame, tags, cal, reading, status, banner)
        if annotated.shape[1] > config.PREVIEW_WIDTH:
            scale = config.PREVIEW_WIDTH / annotated.shape[1]
            annotated = cv2.resize(
                annotated, (config.PREVIEW_WIDTH, int(annotated.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), config.PREVIEW_QUALITY]
        )
        if ok:
            with self._lock:
                self._preview_jpeg = encoded.tobytes()
                self._preview_at = now

    # -------------------------------------------------------------------- MCU
    def _notify_mcu(self) -> None:
        """Mirror the status on the LED matrix. The sketch is optional."""
        with self._lock:
            status = self._status
            asset_id = self._active_asset
            gauge_latest = (self._latest.get(asset_id) or {}).get(config.CHANNEL_GAUGE)
        now = time.monotonic()
        if status == self._notified_status and now - self._notified_at < 5.0:
            return
        percent = 0
        if gauge_latest:
            fraction = (gauge_latest.get("detail") or {}).get("fraction")
            if fraction is not None:
                percent = int(np.clip(float(fraction) * 100.0, 0, 100))
        try:
            Bridge.notify("gauge_status", int(status), int(percent))
            self._notified_status = status
            self._notified_at = now
        except Exception as exc:
            log.debug("MCU notify skipped: %s", exc)

    # -------------------------------------------------------------------- API
    def _register_api(self) -> None:
        ui = self.ui
        ui.expose_api("GET", "/state", self.api_state)
        ui.expose_api("GET", "/preview.jpg", self.api_preview)
        ui.expose_api("GET", "/sensors", self.api_sensors)
        ui.expose_api("GET", "/calibration_frame", self.api_calibration_frame)
        ui.expose_api("GET", "/assets", self.api_assets)
        ui.expose_api("POST", "/asset", self.api_save_asset)
        ui.expose_api("POST", "/asset/preview", self.api_preview_gauge)
        ui.expose_api("POST", "/asset/delete", self.api_delete_asset)
        ui.expose_api("POST", "/capture", self.api_capture)
        ui.expose_api("GET", "/readings", self.api_readings)
        ui.expose_api("GET", "/reading_image", self.api_reading_image)
        ui.expose_api("GET", "/series", self.api_series)
        ui.expose_api("GET", "/events", self.api_events)
        ui.expose_api("POST", "/reset_history", self.api_reset_history)

    def api_state(self) -> dict[str, Any]:
        sensor_state = self.sensors.state()
        with self._lock:
            asset_id = self._active_asset
            asset = self.assets.get(asset_id) if asset_id else None
            live = self._live_reading
            tags = [
                {
                    "id": t.tag_id,
                    "edge_px": round(t.edge_px, 1),
                    "stable": self._stability.get(t.tag_id, {}).get("count", 0),
                    "assigned": self._asset_for_tag(t.tag_id) is not None,
                }
                for t in self._tags
            ]
            next_in = None
            if asset_id and asset_id in self._last_capture:
                elapsed = time.monotonic() - self._last_capture[asset_id]
                next_in = max(0.0, config.MIN_CAPTURE_INTERVAL_S - elapsed)

            channels = {}
            for channel in (asset.channels() if asset else []):
                assessment = self._assessment.get(asset_id, {}).get(channel)
                channels[channel] = {
                    "latest": self._latest.get(asset_id, {}).get(channel),
                    "assessment": assessment.to_dict() if assessment else None,
                    "limits": asset.limits_for(channel).to_dict(),
                    "note": self._channel_note.get(asset_id, {}).get(channel, ""),
                    "sensor_ok": (
                        True if channel == config.CHANNEL_GAUGE
                        else sensor_state["tof" if channel == config.CHANNEL_VIBRATION
                                           else "thermal"]["reporting"]
                    ),
                }

            return {
                "status": config.STATUS_NAMES.get(self._status, "?"),
                "status_code": self._status,
                "camera": {
                    "ok": self.camera is not None,
                    "error": self.camera_error,
                    "width": self._raw_shape[0],
                    "height": self._raw_shape[1],
                },
                "sensors": sensor_state,
                "tags": tags,
                "active_asset": asset_id,
                "asset": asset.to_dict() if asset else None,
                "asset_status": self._asset_status.get(asset_id) if asset_id else None,
                "channels": channels,
                "live_gauge": (
                    {
                        "value": round(live.value, 4),
                        "angle_deg": round(live.angle_deg, 2),
                        "confidence": round(live.confidence, 3),
                        "on_scale": live.on_scale,
                        "ambiguous": live.ambiguous,
                        "notes": live.notes,
                    }
                    if live else None
                ),
                "sklearn": predictive.SKLEARN_AVAILABLE,
                "anomaly_backend": "IsolationForest" if predictive.SKLEARN_AVAILABLE else "robust-z",
                "min_confidence": config.MIN_CONFIDENCE,
                "capture_interval_s": config.MIN_CAPTURE_INTERVAL_S,
                "next_capture_in_s": round(next_in, 1) if next_in is not None else None,
                "stable_frames_required": config.STABLE_FRAMES,
                "assets": sorted(self.assets),
                "uptime_s": round(time.monotonic() - self.started_at, 1),
            }

    def api_sensors(self) -> dict[str, Any]:
        return self.sensors.state()

    def api_preview(self) -> Response:
        with self._lock:
            buf = self._preview_jpeg
        if buf is None:
            return Response(status_code=503, content=b"", media_type="image/jpeg")
        return Response(content=buf, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    def api_calibration_frame(self) -> dict[str, Any]:
        """Full-resolution still plus tag corners, for the click-to-calibrate flow."""
        with self._lock:
            frame = None if self._raw_frame is None else self._raw_frame.copy()
            tags = list(self._tags)
        if frame is None:
            return {"ok": False, "error": "no camera frame yet"}
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            return {"ok": False, "error": "could not encode the frame"}
        return {
            "ok": True,
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "image": base64.b64encode(encoded.tobytes()).decode("ascii"),
            "image_type": "image/jpeg",
            "tags": [
                {"id": t.tag_id, "corners": t.corners.tolist(), "edge_px": round(t.edge_px, 1)}
                for t in tags
            ],
        }

    def api_assets(self) -> dict[str, Any]:
        with self._lock:
            return {"assets": [a.to_dict() for a in self.assets.values()]}

    # -- building an asset from the UI payload ------------------------------
    def _gauge_from_payload(self, raw: dict) -> tuple[Optional[config.GaugeCalibration], Optional[str]]:
        """Turn four clicked image points into tag-space dial geometry."""
        try:
            homography = vision.homography_from_corners(raw["tag_corners"])
            points = raw["points"]
            clicked = np.asarray(
                [points["center"], points["rim"], points["min_tip"], points["max_tip"]],
                dtype=np.float32,
            ).reshape(4, 2)
        except Exception as exc:
            return None, f"malformed gauge geometry: {exc}"

        tag_pts = vision.image_to_tag(homography, clicked)
        center = (float(tag_pts[0][0]), float(tag_pts[0][1]))

        def angle_of(point: np.ndarray) -> float:
            dx = float(point[0]) - center[0]
            dy = float(point[1]) - center[1]
            return config.norm360(math.degrees(math.atan2(dx, -dy)))

        merged = dict(raw)
        merged.update(
            {
                "center_x": center[0],
                "center_y": center[1],
                "radius": float(np.linalg.norm(tag_pts[1] - tag_pts[0])),
                "angle_min_deg": angle_of(tag_pts[2]),
                "angle_max_deg": angle_of(tag_pts[3]),
            }
        )
        cal = config.GaugeCalibration.from_dict(merged)
        problems = cal.validate()
        return (None, "; ".join(problems)) if problems else (cal, None)

    def _asset_from_payload(self, payload: dict) -> tuple[Optional[config.AssetConfig], Optional[str]]:
        try:
            asset_id = str(payload.get("asset_id", "")).strip()
            tag_id = int(payload.get("tag_id"))
        except Exception as exc:
            return None, f"malformed asset payload: {exc}"

        gauge = None
        gauge_raw = payload.get("gauge")
        if gauge_raw and gauge_raw.get("enabled", True):
            gauge, error = self._gauge_from_payload(gauge_raw)
            if gauge is None:
                return None, error

        vibration = None
        vib_raw = payload.get("vibration")
        if vib_raw and vib_raw.get("enabled", True):
            vibration = config.VibrationConfig.from_dict(vib_raw)

        temperature = None
        temp_raw = payload.get("temperature")
        if temp_raw and temp_raw.get("enabled", True):
            temperature = config.TemperatureConfig.from_dict(temp_raw)

        asset = config.AssetConfig(
            asset_id=asset_id,
            tag_id=tag_id,
            label=str(payload.get("label") or ""),
            gauge=gauge,
            vibration=vibration,
            temperature=temperature,
        )
        problems = asset.validate()
        return (None, "; ".join(problems)) if problems else (asset, None)

    def api_preview_gauge(self, payload: dict) -> dict[str, Any]:
        """Dry-run gauge geometry against the current frame without saving it."""
        gauge_raw = payload.get("gauge") or payload
        cal, error = self._gauge_from_payload(gauge_raw)
        if cal is None:
            return {"ok": False, "error": error}
        with self._lock:
            frame = None if self._raw_frame is None else self._raw_frame.copy()
        if frame is None:
            return {"ok": False, "error": "no camera frame yet"}
        try:
            homography = vision.homography_from_corners(gauge_raw["tag_corners"])
        except Exception as exc:
            return {"ok": False, "error": f"bad tag corners: {exc}"}
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        reading = vision.read_gauge(gray, homography, cal)
        if reading is None:
            return {"ok": False, "error": "the dial could not be sampled with this geometry",
                    "gauge": cal.to_dict()}
        overlay = vision.draw_overlay(frame, [], cal, reading, config.STATUS_OK)
        crop = vision.crop_dial(overlay, reading)
        ok, encoded = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return {
            "ok": True,
            "gauge": cal.to_dict(),
            "reading": {
                "value": round(reading.value, 4),
                "angle_deg": round(reading.angle_deg, 2),
                "confidence": round(reading.confidence, 3),
                "on_scale": reading.on_scale,
                "ambiguous": reading.ambiguous,
                "notes": reading.notes,
            },
            "image": base64.b64encode(encoded.tobytes()).decode("ascii") if ok else None,
            "image_type": "image/jpeg",
        }

    def api_save_asset(self, payload: dict) -> dict[str, Any]:
        asset, error = self._asset_from_payload(payload)
        if asset is None:
            return {"ok": False, "error": error}
        self.store.save_asset(asset)
        with self._lock:
            self.assets[asset.asset_id] = asset
            self._active_asset = asset.asset_id
            self._last_capture.pop(asset.asset_id, None)
        for channel in asset.channels():
            self._ensure_pdm(asset.asset_id, channel)
        self.store.add_event(
            asset.asset_id, "info", "asset_saved",
            f"tag {asset.tag_id} assigned to {', '.join(asset.channels())}",
        )
        log.info("asset %s saved (tag %d, channels %s)",
                 asset.asset_id, asset.tag_id, asset.channels())
        return {"ok": True, "asset": asset.to_dict()}

    def api_delete_asset(self, payload: dict) -> dict[str, Any]:
        asset_id = str(payload.get("asset_id", ""))
        if asset_id not in self.assets:
            return {"ok": False, "error": f"no asset named {asset_id!r}"}
        self.store.delete_asset(asset_id)
        with self._lock:
            self.assets.pop(asset_id, None)
            self._assessment.pop(asset_id, None)
            self._latest.pop(asset_id, None)
            self._asset_status.pop(asset_id, None)
            if self._active_asset == asset_id:
                self._active_asset = None
        for channel in config.CHANNELS:
            self.pdm.pop((asset_id, channel), None)
        return {"ok": True}

    def api_capture(self) -> dict[str, Any]:
        with self._lock:
            self._manual_capture = True
            asset_id = self._active_asset
        return {"ok": True, "asset_id": asset_id}

    def api_readings(self, limit: int = 25, asset_id: str = "",
                     channel: str = "") -> dict[str, Any]:
        limit = int(np.clip(limit, 1, 500))
        return {
            "readings": self.store.recent_readings(asset_id or None, channel or None, limit)
        }

    def api_reading_image(self, id: int) -> Response:
        row = self.store.reading_image(int(id))
        if not row or not row.get("image"):
            return Response(status_code=404, content=b"", media_type="image/jpeg")
        return Response(content=base64.b64decode(row["image"]), media_type="image/jpeg")

    def api_series(self, asset_id: str = "", channel: str = config.CHANNEL_GAUGE,
                   hours: float = 24.0) -> dict[str, Any]:
        with self._lock:
            asset_id = asset_id or self._active_asset or ""
            asset = self.assets.get(asset_id)
        if not asset_id or asset is None:
            return {"asset_id": None, "channel": channel, "points": [], "bands": {}}
        limits = asset.limits_for(channel)
        since = time.time() - max(0.1, float(hours)) * 3600.0
        return {
            "asset_id": asset_id,
            "channel": channel,
            "unit": limits.unit,
            "points": self.store.series(asset_id, channel, since),
            "bands": {
                "warn_low": limits.warn_low,
                "warn_high": limits.warn_high,
                "alarm_low": limits.alarm_low,
                "alarm_high": limits.alarm_high,
            },
        }

    def api_events(self, limit: int = 30) -> dict[str, Any]:
        return {"events": self.store.recent_events(int(np.clip(limit, 1, 200)))}

    def api_reset_history(self, payload: dict) -> dict[str, Any]:
        asset_id = str(payload.get("asset_id", ""))
        channel = str(payload.get("channel", "")) or None
        if not asset_id:
            return {"ok": False, "error": "asset_id is required"}
        deleted = self.store.clear_readings(asset_id, channel)
        for name in ([channel] if channel else list(config.CHANNELS)):
            model = self.pdm.get((asset_id, name))
            if model:
                model.reset()
            with self._lock:
                self._latest.get(asset_id, {}).pop(name, None)
                self._assessment.get(asset_id, {}).pop(name, None)
        self.store.add_event(asset_id, "info", "history_cleared",
                             f"{deleted} reading(s) deleted", channel=channel or "")
        return {"ok": True, "deleted": deleted}


gauge_app = GaugeApp()

# App.run() starts the registered bricks (Web UI, SQL store) and then calls loop()
# repeatedly on the main thread. Nothing after this line executes.
App.run(user_loop=gauge_app.loop)
