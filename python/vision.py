# AprilTag location + analog needle reading.
#
# The tag gives us a homography from "tag space" (the unit square the tag occupies) to
# image pixels. Everything about the dial - centre, radius, the angles of the min and max
# marks - is stored in tag space, so a reading taken from a different distance or a
# different viewing angle still lands on the same needle.
#
# Needle detection samples the dial in polar coordinates *in tag space*, which undoes the
# perspective for free, then looks for the one direction that is dark all the way out.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

import config

_FAMILIES = {
    "36h11": cv2.aruco.DICT_APRILTAG_36h11,
    "36h10": cv2.aruco.DICT_APRILTAG_36h10,
    "25h9": cv2.aruco.DICT_APRILTAG_25h9,
    "16h5": cv2.aruco.DICT_APRILTAG_16h5,
}

# Corner order returned by the ArUco/AprilTag detector: TL, TR, BR, BL.
_TAG_SQUARE = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float32)


@dataclass
class TagDetection:
    tag_id: int
    corners: np.ndarray  # (4, 2) float32, image pixels, TL/TR/BR/BL
    center: tuple[float, float]
    edge_px: float

    @property
    def homography(self) -> np.ndarray:
        """Tag space -> image pixels."""
        return cv2.getPerspectiveTransform(_TAG_SQUARE, self.corners.astype(np.float32))


@dataclass
class GaugeReading:
    angle_deg: float
    value: float
    fraction: float
    on_scale: bool
    confidence: float
    contrast: float
    center_px: tuple[float, float]
    radius_px: float
    tip_px: tuple[float, float]
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)


class TagLocator:
    """Thin wrapper around cv2.aruco configured for an AprilTag family."""

    def __init__(self, family: str = config.TAG_FAMILY):
        if family not in _FAMILIES:
            raise ValueError(f"unsupported AprilTag family {family!r}; pick one of {sorted(_FAMILIES)}")
        self.family = family
        dictionary = cv2.aruco.getPredefinedDictionary(_FAMILIES[family])
        params = cv2.aruco.DetectorParameters()
        # Corner accuracy drives every downstream angle, so pay for the refinement.
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
        params.adaptiveThreshWinSizeMin = 5
        params.adaptiveThreshWinSizeMax = 35
        params.adaptiveThreshWinSizeStep = 6
        self._detector = cv2.aruco.ArucoDetector(dictionary, params)

    def detect(self, gray: np.ndarray) -> list[TagDetection]:
        corners, ids, _ = self._detector.detectMarkers(gray)
        if ids is None or len(ids) == 0:
            return []
        out: list[TagDetection] = []
        for quad, tag_id in zip(corners, ids.flatten()):
            pts = quad.reshape(4, 2).astype(np.float32)
            edges = [float(np.linalg.norm(pts[i] - pts[(i + 1) % 4])) for i in range(4)]
            out.append(
                TagDetection(
                    tag_id=int(tag_id),
                    corners=pts,
                    center=(float(pts[:, 0].mean()), float(pts[:, 1].mean())),
                    edge_px=max(edges),
                )
            )
        out.sort(key=lambda d: d.edge_px, reverse=True)
        return out


def homography_from_corners(corners) -> np.ndarray:
    """Tag space -> image pixels, from four detected corners in TL/TR/BR/BL order."""
    pts = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    return cv2.getPerspectiveTransform(_TAG_SQUARE, pts)


def tag_to_image(homography: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Map (..., 2) tag-space points through the homography into image pixels."""
    pts = np.asarray(points, dtype=np.float32).reshape(1, -1, 2)
    return cv2.perspectiveTransform(pts, homography).reshape(np.asarray(points).shape)


def image_to_tag(homography: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Inverse of tag_to_image - used when turning UI clicks into a calibration."""
    inv = np.linalg.inv(homography)
    pts = np.asarray(points, dtype=np.float32).reshape(1, -1, 2)
    return cv2.perspectiveTransform(pts, inv).reshape(np.asarray(points).shape)


def polar_point(center: tuple[float, float], radius: float, angle_deg: float) -> tuple[float, float]:
    """Tag-space point at `angle_deg` clockwise from up, `radius` from `center`."""
    rad = math.radians(angle_deg)
    return (center[0] + radius * math.sin(rad), center[1] - radius * math.cos(rad))


def _circular_smooth(profile: np.ndarray, span_samples: int) -> np.ndarray:
    profile = np.asarray(profile, dtype=np.float32).ravel()
    k = max(1, int(span_samples))
    if k <= 1:
        return profile
    kernel = np.ones(k, dtype=np.float32) / k
    padded = np.concatenate([profile[-k:], profile, profile[:k]])
    return np.convolve(padded, kernel, mode="same")[k:-k]


def _local_maxima(profile: np.ndarray) -> np.ndarray:
    left = np.roll(profile, 1)
    right = np.roll(profile, -1)
    return np.where((profile >= left) & (profile > right))[0]


def _refine_peak(profile: np.ndarray, index: int, step_deg: float) -> float:
    """Sub-step peak position by fitting a parabola through the three samples."""
    n = len(profile)
    y0, y1, y2 = profile[(index - 1) % n], profile[index], profile[(index + 1) % n]
    denom = y0 - 2.0 * y1 + y2
    delta = 0.0 if abs(denom) < 1e-9 else 0.5 * (y0 - y2) / denom
    delta = float(np.clip(delta, -1.0, 1.0))
    return config.norm360((index + delta) * step_deg)


def read_gauge(gray: np.ndarray, homography: np.ndarray, cal: config.Calibration) -> Optional[GaugeReading]:
    """Locate the needle on a calibrated dial and convert its angle to a value."""
    center = cal.center
    center_px = tuple(float(v) for v in tag_to_image(homography, np.array([center]))[0])
    # Pixel scale of the dial: how long one radius is on screen, measured both ways so a
    # squashed (off-axis) view averages out.
    probe = np.array([polar_point(center, cal.radius, 0.0), polar_point(center, cal.radius, 90.0)])
    probe_px = tag_to_image(homography, probe)
    radius_px = float(
        np.mean([np.linalg.norm(np.array(center_px) - probe_px[i]) for i in range(2)])
    )
    if not math.isfinite(radius_px) or radius_px < 6.0:
        return None

    r_in = cal.radius * cal.r_inner_frac
    r_out = cal.radius * cal.r_outer_frac
    span_px = radius_px * (cal.r_outer_frac - cal.r_inner_frac)
    n_r = int(np.clip(round(span_px), 8, config.MAX_RADIAL_SAMPLES))
    step = config.ANGLE_STEP_DEG
    n_t = int(round(360.0 / step))

    thetas = np.arange(n_t, dtype=np.float32) * step
    radii = np.linspace(r_in, r_out, n_r, dtype=np.float32)
    rad = np.deg2rad(thetas)[:, None]
    xs = center[0] + radii[None, :] * np.sin(rad)
    ys = center[1] - radii[None, :] * np.cos(rad)

    grid = np.stack([xs, ys], axis=-1).astype(np.float32)          # (n_t, n_r, 2)
    grid_px = cv2.perspectiveTransform(grid.reshape(1, -1, 2), homography).reshape(n_t, n_r, 2)
    map_x = np.ascontiguousarray(grid_px[..., 0], dtype=np.float32)
    map_y = np.ascontiguousarray(grid_px[..., 1], dtype=np.float32)

    # Rays that fall outside the frame would be clamped to the border and could fake a
    # dark streak, so mask them out instead.
    h, w = gray.shape[:2]
    inside = (map_x >= 0) & (map_x <= w - 1) & (map_y >= 0) & (map_y <= h - 1)
    if inside.mean() < 0.6:
        return None

    samples = cv2.remap(
        gray, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    ).astype(np.float32)

    darkness = (255.0 - samples) if cal.needle_dark else samples
    darkness[~inside] = 0.0
    # Subtracting the per-radius median removes concentric features - the bezel, the
    # scale ring, printed arcs - and leaves whatever is dark at one angle only.
    darkness -= np.median(darkness, axis=0, keepdims=True)
    np.clip(darkness, 0.0, None, out=darkness)

    profile = _circular_smooth(darkness.mean(axis=1), round(config.SMOOTH_SPAN_DEG / step))
    peak_max = float(profile.max())
    if peak_max <= 1e-6:
        return None

    # How far out along each ray the dark streak survives: the needle tip reaches the
    # scale ring, its tail usually stops at the hub.
    ray_max = darkness.max(axis=1)                                  # (n_t,)
    lit_fraction = (darkness > 0.35 * ray_max[:, None]).mean(axis=1)  # (n_t,)
    reach = np.where(ray_max > 1e-6, lit_fraction, 0.0)
    reach = _circular_smooth(reach.astype(np.float32), round(config.SMOOTH_SPAN_DEG / step))

    candidates = _local_maxima(profile)
    if len(candidates) == 0:
        candidates = np.array([int(np.argmax(profile))])
    scores = profile[candidates] * (0.7 + 0.3 * reach[candidates])
    order = np.argsort(scores)[::-1][:6]
    candidates = candidates[order]
    scores = scores[order]

    notes: list[str] = []
    chosen = None
    for idx, score in zip(candidates, scores):
        angle = _refine_peak(profile, int(idx), step)
        _, t, on_scale = cal.value_from_angle(angle)
        if on_scale:
            chosen = (int(idx), angle, float(score))
            break
    if chosen is None:
        # Nothing landed inside the calibrated arc - report the strongest streak anyway
        # and let the caller see on_scale=False.
        idx = int(candidates[0])
        chosen = (idx, _refine_peak(profile, idx, step), float(scores[0]))
        notes.append("needle is outside the calibrated arc")

    idx, angle_deg, _ = chosen
    value, fraction, on_scale = cal.value_from_angle(angle_deg)

    median = float(np.median(profile))
    mad = float(np.median(np.abs(profile - median)))
    contrast = (peak_max - median) / (1.4826 * mad + 1e-6)
    confidence = float(np.clip(contrast / 8.0, 0.0, 1.0))

    # A rival peak of similar strength somewhere else on the arc means we may have locked
    # onto the tail, a shadow or a second pointer.
    ambiguous = False
    for other, score in zip(candidates, scores):
        gap = config.norm360(int(other) * step - angle_deg)
        if min(gap, 360.0 - gap) < 20.0:
            continue
        _, _, other_on_scale = cal.value_from_angle(int(other) * step)
        if other_on_scale and score > 0.8 * profile[idx]:
            ambiguous = True
            confidence *= 0.5
            notes.append("a second dark streak of similar strength is on the dial")
            break

    tip_tag = polar_point(center, cal.radius * cal.r_outer_frac, angle_deg)
    tip_px = tuple(float(v) for v in tag_to_image(homography, np.array([tip_tag]))[0])

    return GaugeReading(
        angle_deg=float(angle_deg),
        value=float(value),
        fraction=float(fraction),
        on_scale=bool(on_scale),
        confidence=confidence,
        contrast=float(contrast),
        center_px=center_px,
        radius_px=radius_px,
        tip_px=tip_px,
        ambiguous=ambiguous,
        notes=notes,
    )


# --- overlay ----------------------------------------------------------------

_STATUS_COLORS = {
    config.STATUS_OK: (96, 200, 96),
    config.STATUS_WATCH: (40, 190, 240),
    config.STATUS_ALARM: (60, 60, 235),
    config.STATUS_UNCALIBRATED: (200, 160, 60),
    config.STATUS_SCANNING: (190, 190, 190),
    config.STATUS_BOOT: (190, 190, 190),
}


def draw_overlay(
    frame: np.ndarray,
    tags: list[TagDetection],
    cal: Optional[config.Calibration] = None,
    reading: Optional[GaugeReading] = None,
    status: int = config.STATUS_SCANNING,
    banner: str = "",
) -> np.ndarray:
    """Annotated copy of the frame, used for the live preview and the stored dial crop."""
    out = frame.copy()
    color = _STATUS_COLORS.get(status, (190, 190, 190))

    for tag in tags:
        pts = tag.corners.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [pts], True, (255, 190, 40), 2, cv2.LINE_AA)
        cx, cy = int(tag.center[0]), int(tag.center[1])
        cv2.putText(out, f"tag {tag.tag_id}", (cx - 26, cy - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 190, 40), 1, cv2.LINE_AA)

    if cal is not None and reading is not None:
        cx, cy = int(round(reading.center_px[0])), int(round(reading.center_px[1]))
        r = int(round(reading.radius_px))
        cv2.circle(out, (cx, cy), r, color, 2, cv2.LINE_AA)
        cv2.circle(out, (cx, cy), 4, color, -1, cv2.LINE_AA)
        tip = (int(round(reading.tip_px[0])), int(round(reading.tip_px[1])))
        cv2.line(out, (cx, cy), tip, color, 2, cv2.LINE_AA)
        label = f"{reading.value:.2f} {cal.unit}".strip()
        cv2.putText(out, label, (cx - r, cy - r - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)
        cv2.putText(out, f"conf {reading.confidence:.2f}", (cx - r, cy + r + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    if banner:
        cv2.rectangle(out, (0, 0), (out.shape[1], 28), (28, 28, 28), -1)
        cv2.putText(out, banner, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    return out


def crop_dial(frame: np.ndarray, reading: GaugeReading, margin: float = 1.35) -> np.ndarray:
    """Square crop around the dial, for the audit trail stored with each reading."""
    cx, cy = reading.center_px
    half = max(24.0, reading.radius_px * margin)
    h, w = frame.shape[:2]
    x0 = int(max(0, round(cx - half)))
    y0 = int(max(0, round(cy - half)))
    x1 = int(min(w, round(cx + half)))
    y1 = int(min(h, round(cy + half)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return frame
    # imencode wants a contiguous buffer, and a slice is a view.
    return np.ascontiguousarray(frame[y0:y1, x0:x1])
