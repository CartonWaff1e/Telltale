# Working on this

## Where things live

```
python/
  main.py         GaugeApp: the capture loop, channel measurement, the HTTP API
  config.py       settings, plus Limits / GaugeCalibration / VibrationConfig /
                  TemperatureConfig / AssetConfig
  vision.py       TagLocator, read_gauge(), the overlay drawing
  sensors.py      SensorHub — receives MCU summaries over the Bridge
  predictive.py   PredictiveMaintenance and Assessment
  store.py        SQLite: schema, migrations, queries
sketch/
  sketch.ino      I²C sensors, vibration statistics, Bridge publishing, LED matrix
  sketch.yaml     pinned libraries — hand-maintained, see below
assets/           index.html + app.js + style.css, no build step
```

The dependency direction is one-way and worth preserving: `main` imports everything; `vision`,
`store`, `predictive` and `sensors` import only `config`. Nothing imports `main`. If you find
yourself wanting a back-reference, that's usually a sign the logic belongs in `main`.

<a id="the-edit-sync-restart-loop"></a>
## The edit-sync-restart loop

This will bite you, so it goes near the top.

1. Edit files in the local checkout.
2. **Let the turn end.** Edits sync to the board at the end of a turn, not immediately.
3. Restart the app. Python only reloads on restart.
4. Read the logs.

Restarting in the same turn as the edit runs the *old* code. The symptom is confusing: the
traceback shows your *new* source lines but the *old* error, because Python renders source from
the file on disk while executing bytecode loaded at startup. If the line numbers look right but
the error is stale, the process is stale — restart again.

There's no hot reload. `App.run()` blocks forever and nothing watches the filesystem.

## Reading the logs

`apps_logs` gives Python stdout and anything through `logging`. The app configures logging in
`main.py` at INFO with timestamps.

Two things to know:

- **The capture loop swallows its own exceptions.** `loop()` wraps `_tick()` in a try/except so
  one bad frame doesn't kill the app. Failures show up as a repeating traceback, not a crash.
  Silence in the logs is not the same as health.
- `predictive.py` logs at import time if scikit-learn is missing, before `basicConfig` runs.
  That message goes out through logging's last-resort handler and looks different from the
  rest. It's fine.

For the MCU side, use the serial monitor. The sketch prints one line per sensor at boot and
nothing per-reading — deliberately, because printing at 50 Hz would disturb the sample timing
it exists to protect.

## The sketch.yaml trap

`sketch.yaml` is maintained **by hand**. Don't run a library-add against this sketch without
checking what it did afterwards.

Arduino profiles list libraries explicitly and don't resolve dependencies at build time, which
is exactly what we want. But `arduino-app-cli lib add` *does* resolve transitively when it
writes the file. Adding `Adafruit MLX90640` rewrote `sketch.yaml` with 49 libraries, because
its index entry declares *Adafruit Arcada* as a dependency — used only by its examples, and a
gateway to SdFat, TinyUSB and a pile of SAMD-only code that cannot build for Zephyr.

The three that are actually needed:

```yaml
libraries:
  - Arduino_RouterBridge (0.4.3)
  - VL53L0X (1.3.1)              # Pololu, no dependencies
  - Adafruit MLX90640 (1.1.2)
  - Adafruit BusIO (1.17.4)      # the only real MLX90640 dependency
```

`Arduino_LED_Matrix` ships with the `arduino:zephyr` core and needs no entry.

## Adding a channel

The multi-channel structure exists so a fourth sensor is a small change. Roughly:

1. **`config.py`** — add a `FooConfig` dataclass with a `Limits` and a `value_from(sample)`
   that pulls one scalar out of a sensor sample. Add it to `AssetConfig`, to `channels()`, to
   `limits_for()`, and to `to_dict`/`from_dict`. Add the channel name to `CHANNELS`.
2. **`sensors.py`** — a Bridge handler storing the latest sample with a timestamp, and a case in
   `sample_for()`.
3. **`sketch.ino`** — probe for it in `setup()`, sample it in `loop()`, publish a summary, and
   include it in `sensor_status`.
4. **`main.py`** — a `_measure_foo()` returning the standard measurement dict, wired into
   `_measure()`, plus a case in `_unavailable_note()`.
5. **UI** — a block in the assign form and an `extras` case in `renderChannelCards()`.

No database migration. `readings` is keyed by channel name and doesn't care how many exist.

The measurement dict every channel returns:

```python
{
  "value": float,            # the one number that gets modelled
  "unit": str,
  "confidence": float,       # 0..1
  "valid": bool,             # False keeps it out of the model
  "invalid_reason": str,     # shown to the user when valid is False
  "detail": dict,            # channel-specific extras, JSON-serialisable
  "image": str | None,       # base64 JPEG
  "image_type": str | None,
}
```

Returning `None` instead of a dict means "can't measure right now" — the normal path for a
missing sensor. That's different from returning a dict with `valid: False`, which means "I
measured something and don't trust it". The first produces no row; the second produces a row
that's visible but excluded from training.

## Threading

One writer, several readers.

The capture loop owns all mutable state and runs on the main thread. FastAPI handlers run on
the web server's threads and only read. Bridge callbacks in `SensorHub` write to their own
small state behind their own lock.

Everything in `GaugeApp` goes behind `self._lock`, an `RLock` so nested acquisition inside a
single handler is safe. Keep it that way — the moment there are two writers, a lot of `.get()`
chains in `api_state()` become race conditions.

Don't do slow work while holding the lock. Frame encoding and database writes happen outside
it; only the state swap is inside.

## Testing without the hardware you don't have

- **No camera?** The app starts anyway, serves the UI, reports the error in `/state`, and
  retries every 10 seconds.
- **No sensors?** Channels report unavailable. Everything else runs.
- **No MCU sketch?** `Bridge.notify` failures are caught and logged at debug. The gauge channel
  is unaffected.
- **Want to exercise the model without waiting days?** Insert synthetic rows straight into
  `readings` with `valid = 1` and restart — models bootstrap from the database at startup, so
  you get a populated baseline immediately.

## Things worth knowing about the platform

Collected the hard way.

- The Python app runs in `python-apps-base` with Python 3.13, numpy 2.5,
  `opencv-python-headless` 4.13, pandas and pillow already present. `cv2.aruco` **is** included
  with the AprilTag dictionaries, which is lucky — the headless OpenCV wheels don't always ship
  contrib modules.
- `python/requirements.txt` is installed by `run.sh` with `uv pip install` at container start,
  cached against a hash of the file. That's the supported way to add a Python dependency; there
  is no need to build a custom image.
- `SQLStore.execute_sql()` commits, including for writes. `store()` with `create_table=False`
  permits NULLs; with `create_table=True` its type inference rejects `None`. That's why every
  write here passes `create_table=False` against an explicitly created schema.
- Bricks auto-register when constructed and are started by `App.run()`. Don't call `.start()`
  yourself. `SQLStore` is an exception in that it connects lazily, which is why building the
  schema in `Store.__init__` before `App.run()` works.
- `WebUI.expose_api` is a thin wrapper over FastAPI's `add_api_route`, so handler type hints
  drive parsing: a `dict` parameter becomes a JSON body, scalars become query parameters, and
  returning a `Response` bypasses serialisation (that's how `/preview.jpg` works).
- Status codes are duplicated between `config.py` and `sketch.ino` with no shared header.
  Change one, change the other.

## If you pick this up cold

Read [architecture](architecture.md) for the shape, then
[how-it-works](how-it-works.md) for the needle algorithm, which is the only genuinely subtle
part. Then open `main.py` and follow `_tick()` down through `_capture()` and `_record()`. That
path is the whole application; everything else is support.
