# HTTP API

Everything the web UI does, it does through these. They're plain REST on port 7000 with **no
`/api` prefix** — the WebUI brick's `api_path_prefix` is left empty, so a route registered as
`/state` really is `http://<board>:7000/state`.

Handy for scripting: nothing here needs authentication, so `curl` and a shell loop will get you
a long way if you want to pull readings into something else.

---

## Status and live data

### `GET /state`

The big one. The UI polls this once a second and renders almost everything from it.

```jsonc
{
  "status": "OK",                    // BOOT|SCANNING|UNCALIBRATED|OK|WATCH|ALARM
  "status_code": 3,
  "camera":  { "ok": true, "error": null, "width": 1280, "height": 720 },
  "sensors": { /* same shape as GET /sensors */ },
  "tags": [
    { "id": 3, "edge_px": 118.4, "stable": 22, "assigned": true }
  ],
  "active_asset": "pump-3",
  "asset":        { /* full AssetConfig */ },
  "asset_status": "WATCH",
  "channels": {
    "gauge": {
      "latest":     { /* the last stored reading, minus the image blob */ },
      "assessment": { /* see below */ },
      "limits":     { "unit": "bar", "warn_high": 2.0, "alarm_high": 2.5, ... },
      "note":       "",              // why this channel was skipped, if it was
      "sensor_ok":  true
    },
    "vibration":   { ... },
    "temperature": { ... }
  },
  "live_gauge": { "value": 1.84, "angle_deg": 271.5, "confidence": 0.72,
                  "on_scale": true, "ambiguous": false, "notes": [] },
  "sklearn": true,
  "anomaly_backend": "IsolationForest",
  "capture_interval_s": 30.0,
  "next_capture_in_s": 12.4,
  "stable_frames_required": 4,
  "assets": ["pump-3"],
  "uptime_s": 1843.2
}
```

`live_gauge` is a continuously updated read used for the preview — it is **not** stored. The
stored value is `channels.gauge.latest`.

An `assessment` looks like this:

```jsonc
{
  "status": "WATCH", "status_code": 4,
  "range_state": "warn_high",        // normal|warn_low|warn_high|alarm_low|alarm_high|unknown
  "anomaly_score": -0.043,           // negative is anomalous; null before the model is ready
  "is_anomaly": true,
  "trend_per_hour": 0.031, "trend_r2": 0.78,
  "hours_to_limit": 18.4, "limit_name": "alarm_high",
  "model_ready": true, "n_samples": 137, "backend": "IsolationForest",
  "reasons": ["2.34 bar is above the warning limit of 2.00 bar", "..."]
}
```

### `GET /preview.jpg`

The annotated camera preview as a JPEG: tag outlines, the dial circle, the detected needle, a
status banner. Returns **503** with an empty body when no frame has been processed yet — the UI
treats that as "retry shortly", not as an error.

Sent with `Cache-Control: no-store`. The UI refreshes it about five times a second by
appending a cache-busting query string.

### `GET /sensors`

Just the MCU sensor block, if that's all you need.

```jsonc
{
  "mcu_reporting": true,
  "tof": {
    "detected": true, "reporting": true, "read_failures": 0,
    "latest": { "mean_mm": 341.2, "rms": 0.41, "peak_to_peak": 2.0,
                "dominant_hz": 12.0, "samples": 48 }
  },
  "thermal": {
    "detected": false, "reporting": false, "read_failures": 0,
    "latest": null, "grid": [], "grid_shape": [3, 4]
  }
}
```

`detected` is what the I²C probe found at boot. `reporting` is whether a summary has arrived
recently (within `GAUGE_SENSOR_STALE_S`). Detected-but-not-reporting usually means the sensor
is failing its reads — check `read_failures`.

---

## Assets

### `GET /assets`

`{ "assets": [ AssetConfig, ... ] }`

### `POST /asset`

Create or overwrite. Same id overwrites; readings are kept.

```jsonc
{
  "asset_id": "pump-3",
  "label": "Coolant pump, north wall",
  "tag_id": 3,

  "gauge": {                          // omit or null to disable the channel
    "enabled": true,
    "tag_corners": [[x,y],[x,y],[x,y],[x,y]],   // from /calibration_frame
    "points": {                                  // full-resolution image pixels
      "center":  [812, 344],
      "rim":     [905, 344],
      "min_tip": [742, 410],
      "max_tip": [882, 410]
    },
    "unit": "bar",
    "value_min": 0, "value_max": 10,
    "needle_dark": true,
    "r_inner_frac": 0.25, "r_outer_frac": 0.92,
    "limits": { "unit": "bar", "warn_high": 8, "alarm_high": 9 }
  },

  "vibration": {
    "enabled": true,
    "metric": "rms",                 // rms | peak_to_peak
    "settle_s": 1.5,
    "min_distance_mm": 30, "max_distance_mm": 2000,
    "limits": { "unit": "mm", "warn_high": 0.8, "alarm_high": 1.5 }
  },

  "temperature": {
    "enabled": true,
    "metric": "max",                 // max | mean | min | delta_ambient
    "limits": { "unit": "C", "warn_high": 60, "alarm_high": 75 }
  }
}
```

The server converts those four clicked points into tag-space geometry using the inverse
homography, so the calibration survives the camera moving afterwards.

Returns `{"ok": true, "asset": {...}}`, or `{"ok": false, "error": "..."}` with a readable
explanation — "dial radius is zero", "enable at least one channel", and so on. Validation
failures come back as `ok: false` with HTTP 200, not as a 4xx.

### `POST /asset/preview`

Dry-run gauge geometry against the current frame without saving. Takes the same `gauge` object
(either wrapped in `{"gauge": {...}}` or bare).

```jsonc
{
  "ok": true,
  "gauge": { /* resolved tag-space geometry */ },
  "reading": { "value": 1.84, "angle_deg": 271.5, "confidence": 0.72,
               "on_scale": true, "ambiguous": false, "notes": [] },
  "image": "<base64 jpeg of the dial with the needle drawn>",
  "image_type": "image/jpeg"
}
```

This is what the **Test read** button calls. Use it before saving.

### `POST /asset/delete`

`{"asset_id": "pump-3"}` → `{"ok": true}`. Removes the configuration, keeps the readings.

---

## Readings and history

### `POST /capture`

`{}` → `{"ok": true, "asset_id": "pump-3"}`

Requests an immediate capture, bypassing the stability gate and the cooldown. It's a flag the
loop picks up on its next iteration, so the response returns before the reading is taken.

### `GET /readings`

| Param | Default | |
| --- | --- | --- |
| `limit` | 25 | clamped to 1–500 |
| `asset_id` | all | |
| `channel` | all | `gauge` \| `vibration` \| `temperature` |

Newest first. The base64 image blob is stripped; use `/reading_image` for that.

### `GET /reading_image?id=<row id>`

The stored dial crop as a JPEG. **404** if that row has no image — only gauge readings have
one, and only the newest `GAUGE_KEEP_IMAGES` rows keep theirs.

### `GET /series`

| Param | Default | |
| --- | --- | --- |
| `asset_id` | active asset | |
| `channel` | `gauge` | |
| `hours` | 24 | |

Oldest first, for charting, with the limit bands alongside:

```jsonc
{
  "asset_id": "pump-3", "channel": "gauge", "unit": "bar",
  "points": [ { "ts_epoch": 1755..., "value": 1.84, "status": "OK",
                "range_state": "normal", "is_anomaly": 0,
                "confidence": 0.72, "valid": 1 } ],
  "bands": { "warn_low": null, "warn_high": 8, "alarm_low": null, "alarm_high": 9 }
}
```

### `GET /events?limit=30`

Status transitions, channel availability changes, calibration saves, cleared history. Newest
first.

### `POST /reset_history`

```jsonc
{ "asset_id": "pump-3", "channel": "vibration" }   // channel optional; omit for all
```

Deletes the readings **and** resets the in-memory model for those channels. Returns
`{"ok": true, "deleted": 412}`.

Do this after re-calibrating a gauge. New geometry means new values, and the old baseline will
make every new reading look anomalous.

---

## Scripting notes

Pull the last day of vibration readings as JSON:

```bash
curl -s "http://192.168.1.50:7000/series?channel=vibration&hours=24" | jq '.points'
```

Force a reading and then look at it:

```bash
curl -s -X POST http://192.168.1.50:7000/capture -H 'Content-Type: application/json' -d '{}'
sleep 2
curl -s "http://192.168.1.50:7000/readings?limit=3" | jq
```

Two things to be aware of if you're building on this. There's no authentication — anyone who
can reach the port can rewrite your calibrations, so don't expose it to a network you don't
trust. And the whole thing is one FastAPI app sharing a lock with the capture loop, so hammering
it with concurrent requests will slow down frame processing. Poll at a human rate.
