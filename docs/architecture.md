# Architecture

## The two halves of the board

The UNO Q is really two computers sharing a PCB. The Linux side (MPU) runs Python, OpenCV and
the web server. The Arduino side (MCU) runs a Zephyr sketch with real-time access to GPIO and
I²C. They talk over the Router Bridge.

The split we chose:

| Runs on the MPU | Runs on the MCU |
| --- | --- |
| Camera capture and AprilTag detection | VL53L0X ranging at ~50 Hz |
| Needle reading | MLX90640 frame reads |
| Anomaly models, trend fitting | Vibration statistics |
| SQLite, web UI, REST API | LED matrix status display |

The dividing line is timing. Anything that needs a steady sample clock lives on the MCU;
anything that needs numpy or a filesystem lives on the MPU.

That's why the vibration RMS is computed on the Arduino side rather than in Python. We could
have streamed 50 raw distance samples a second across the Bridge and done the maths in numpy,
and it would have been nicer code. But then the sample timing depends on Bridge latency and
Linux scheduling, and an RMS is only meaningful if you know when each sample was taken. The
MCU knows. So it does the arithmetic and sends five floats a second instead of fifty.

Same reasoning, more bluntly, for the thermal array: a full MLX90640 frame is 768 floats. Once
a second that's 3 KB across a serial bridge, to compute a max and a mean. The MCU reduces it
to six numbers before sending.

## The capture loop

`App.run(user_loop=...)` calls `GaugeApp.loop()` over and over on the main thread. One
iteration:

1. Grab a frame from the camera. This blocks at the camera's frame rate, which paces
   everything else.
2. Convert to grayscale, run the AprilTag detector.
3. Update the stability tracker — how many consecutive frames has each tag been seen, and how
   far did its corners move between them.
4. Pick a target: the largest visible tag that's assigned to an asset.
5. If that asset has a gauge channel, do a cheap "live" needle read (throttled to about 3 Hz)
   purely so the preview and the UI have something to show.
6. If the tag has been stable for long enough *and* the asset isn't in its cooldown, fire a
   capture.
7. Redraw the preview JPEG, push a status code to the MCU.

Everything is in one thread except the web server, which FastAPI runs on its own. Shared state
sits behind a single `RLock`. There's exactly one writer (the loop) and several readers (API
handlers), which keeps the locking boring.

## What a capture does

```
_capture(frame, tag, asset)
    for each enabled channel:
        _measure(channel)  ──▶  None means "can't right now"
             gauge        read the needle from this frame
             vibration    take the latest ToF summary, if the tag has settled
             temperature  take the latest thermal summary
        if measured:
            _record(...)  ──▶  model.update() ──▶ Assessment ──▶ one SQLite row
    asset status = worst channel status
```

`_measure` returning `None` is the normal path when a sensor isn't there. It's not an error.
The first time it happens for a given asset and channel an event is logged, and when the
sensor comes back another event says so. In between, nothing is written and nothing complains.

A channel that measured something but measured it *badly* is different — a needle outside the
calibrated arc, say, or a distance reading that says the sensor is pointed at the far wall.
Those get stored with `valid = 0` and a `WATCH` status, but they are deliberately **not** fed
to the anomaly model. If you let a misread needle into the training data, the model learns
that misreads are normal, and then it stops flagging them. The gap in the timeline is more
useful than a bad number.

## Configuration model

```
AssetConfig
  asset_id, tag_id, label
  gauge:       GaugeCalibration | None   ── dial geometry, scale, Limits
  vibration:   VibrationConfig   | None   ── metric, settle time, stand-off window, Limits
  temperature: TemperatureConfig | None   ── metric, Limits
```

`Limits` is the same little object for all three: `warn_low`, `warn_high`, `alarm_low`,
`alarm_high`, any of which may be `None` meaning "don't check that side". Making it shared is
what lets one `PredictiveMaintenance` class serve every channel — it never needs to know
whether it's looking at bar, millimetres or degrees.

Configs are stored as JSON blobs in SQLite rather than as columns. Channels get new fields
often enough that a rigid schema would be a nuisance, and there are only ever a handful of
assets.

## One model per channel, not per asset

`self.pdm` is keyed by `(asset_id, channel)`. Three channels on one asset means three
independent IsolationForests, three independent trend fits, three separate histories.

The alternative — one model over a combined feature vector — is tempting and wrong for this
use case. If the thermal sensor is unplugged for a day, a combined model has a hole in its
feature vector and either falls over or silently learns that missing temperature is normal.
Independent models just carry on, and the vibration channel's baseline is untouched by the
temperature channel's outage.

It also makes the alerts legible. "Vibration is anomalous, temperature is fine" is something
you can act on. "The asset's 5-dimensional feature vector is unusual" is not.

## Bridge protocol

MCU → Python, all fire-and-forget (`notify`):

| Message | Args | Rate |
| --- | --- | --- |
| `sensor_status` | `tof_present, thermal_present, tof_failures, thermal_failures` | every 5 s |
| `tof_summary` | `mean_mm, rms_mm, pp_mm, dominant_hz, n_samples` | every 1 s |
| `thermal_stats` | `min_c, mean_c, max_c, ambient_c, hot_x, hot_y` | every 1 s |
| `thermal_grid` | 12 floats (coarse 4×3) | not sent yet |

Python → MCU:

| Message | Args | When |
| --- | --- | --- |
| `gauge_status` | `status_code, percent` | on status change, at least every 5 s |

`thermal_grid` has a handler on the Python side but the sketch doesn't send it. That's
intentional — it means adding a thermal preview to the UI later is a sketch-only change.

Status codes are duplicated as constants in `python/config.py` and `#define`s in
`sketch/sketch.ino`. There's no shared header between the two languages, so if you change one
you must change the other. Both files say so in a comment.

## Why summaries and not a real-time stream

You could argue this whole thing should stream continuously and compute on the fly. It
shouldn't, for a boring reason: the rover this is eventually going on will be parked in front
of each asset for a few seconds, a handful of times a day. The interesting signal is
*day-over-day drift*, not *millisecond-by-millisecond waveform*. A capture every 30 seconds
while parked, with a good summary each time, gives the trend model exactly what it needs and
keeps the database small enough to live on a board.

If you did want waveform-level analysis, the place to add it is the MCU: buffer a couple of
seconds of samples, run an FFT there, and send the top few bins as extra fields on
`tof_summary`. The Python side wouldn't need to change much.
