# Troubleshooting

Most of these are real, in the sense that they actually happened during development rather
than being imagined for the sake of a docs page.

**Read the logs first.** The capture loop catches its own exceptions so one bad frame doesn't
kill the app, which is good for robustness and bad for noticing that something is broken. The
UI keeps working, the preview keeps updating, and the only sign of trouble is a traceback
repeating in the log. If something isn't behaving, look there before anything else.

---

## Nothing is being recorded

### The tag is stable but no readings appear

The UI shows `stable 22/4` — far past the threshold — and the readings table stays empty.

**Look for a repeating traceback in the logs.** If `read_gauge()` throws, the exception
propagates to `loop()`, gets logged, and the iteration is abandoned before anything can be
stored. From the outside it looks like the stability gate isn't firing. It is; the work after it
is dying.

<a id="the-loop-throws-on-every-frame"></a>
The one we hit looked like this, once per frame, forever:

```
ERROR telltale: loop iteration failed: object too deep for desired array
  File "/app/python/vision.py", line 206, in read_gauge
    reach = _circular_smooth(...)
  File "/app/python/vision.py", line 128, in _circular_smooth
    return np.convolve(padded, kernel, mode="same")[k:-k]
ValueError: object too deep for desired array
```

A numpy broadcasting mistake. `ray_max` was built with `keepdims=True`, so it had shape
`(720, 1)`; comparing it against a `(720,)` array broadcast out to a 720×720 matrix, and
`np.convolve` only accepts 1-D input. The fix was dropping `keepdims` and indexing with
`[:, None]` at the point where the extra axis was actually wanted. `_circular_smooth` now
`ravel()`s its input too, so a shape slip degrades instead of taking down the loop.

The general lesson: `ValueError: object too deep for desired array` from numpy almost always
means an accidental outer product from broadcasting a column against a row.

### Other reasons a capture won't fire

- **Cooldown.** Only one capture per asset per `GAUGE_CAPTURE_INTERVAL_S` (default 30 s). The
  hint under the preview counts it down. **Capture now** ignores it.
- **The tag isn't assigned.** Status `UNCALIBRATED` and the chip says "unassigned". A tag with
  no asset is detected but not measured.
- **The tag is too small.** Under `MIN_TAG_EDGE_PX` (28 px) it's ignored by the stability
  tracker entirely. Move closer or print bigger.
- **Nothing could be measured.** If every enabled channel returns "not available", nothing is
  stored. Check the `note` on each channel card — it tells you which one and why.

## Gauge problems

### "the dial could not be sampled"

`read_gauge` gave up. Three causes:

- More than 40% of the sampling grid fell outside the image. The dial is partly out of frame,
  or the geometry points somewhere silly.
- The computed radius came out under 6 pixels — usually a centre and rim click that were
  nearly on top of each other.
- The dial is uniformly flat after median subtraction, so there's no peak at all. Lens cap,
  total glare, or a `needle_dark` setting that's backwards.

### The value is wrong but confident

Re-run **Test read** and compare against your eyes.

| What you see | Cause |
| --- | --- |
| Reads high when it should read low, and vice versa | Min and max clicks swapped |
| Off by a constant amount | Centre click was inaccurate |
| Fine mid-scale, wrong at the ends | Centre is off — the error grows toward the extremes |
| Jumps 180° occasionally | Locking onto the needle's tail. Widen the annulus so the pointer's extra length counts for more |

### Confidence is always low

Contrast, nearly always. Glare on the glass, a dark needle on a dark face, or the annulus
overlapping the printed scale ring.

Try, in order: fix the lighting; lower `r_outer_frac` to stop sampling the scale ring; raise
`r_inner_frac` if there's a big hub. If the gauge genuinely has poor contrast, lower
`GAUGE_MIN_CONFIDENCE` — but understand that you're telling the model to trust worse data, and
you'd be better off putting a lamp on it.

### "more than one strong streak on the dial"

Something else on the face is competing: a second pointer (max-hold needles do this), a hard
shadow, a reflection of the camera, or a bold printed line. Confidence is halved automatically.
If it's a real second pointer, narrow the annulus to a band where only the pointer you want
lives.

## Sensor problems

### The chips say "not detected"

Look at the **I2C buses** field in the sensor panel first. It tells you what answered on each
of the board's three I²C controllers, and that's the actual diagnosis:

| Bus field | Meaning |
| --- | --- |
| `Wire1: 0x29` | Found. If the channel still isn't working, `init()` is failing — usually supply voltage |
| an address you didn't expect | Wrong part, or XSHUT holding the VL53L0X in reset |
| `all empty` | Nothing on any bus — power, an SDA/SCL swap, or a solder bridge |
| `not scanned` | The MCU isn't reporting at all; see below |

**The UNO Q has three I²C buses**, exposed to Arduino as `Wire`, `Wire1` and `Wire2`
(devicetree `i2c@40005800`, `i2c@40008400`, `i2c@46002800`). Which one the Qwiic connector is
wired to isn't documented anywhere obvious, and it is **not** necessarily `Wire`. This bit us:
the first version of the sketch only ever touched `Wire`, so a perfectly good sensor on another
bus looked exactly like a wiring fault.

The sketch now probes all three and binds each sensor to whichever bus answered. The serial
monitor spells it out at boot:

```
I2C scan Wire: nothing responded
I2C scan Wire1: 0x29
I2C scan Wire2: nothing responded
VL53L0X ready on Wire1 - vibration channel active
MLX90640 not found at 0x33 on any bus - temperature disabled
```

Sensors are re-probed every 5 seconds while any configured one is still missing, so plugging
something in after boot now works without a restart. Once everything has been found the
scanning stops.

If a sensor answers but `init()` fails, that's a different message and points at power — a
VL53L0X fed 3.3 V through an onboard regulator can drop below its minimum. Use the breakout's
`3V3`/`3Vo` pin rather than `VIN` where there is one.

Qwiic pinout for hand-soldering, in connector order: **GND (black) · 3.3 V (red) · SDA (blue) ·
SCL (yellow)**. Cheap VL53L0X boards silkscreen theirs `VIN GND SCL SDA` — SCL *before* SDA,
the reverse of most breakouts, which makes a mirrored pair very easy to solder.

### "detected" but "silent"

The sensor answered at boot and then stopped producing summaries. `read_failures` in
`GET /sensors` is the thing to look at.

For the ToF, this is usually I²C timeouts — long cable runs, electrical noise from the machine
you're monitoring, or a marginal supply. For the thermal array, `getFrame()` returning non-zero
usually means the frame wasn't ready in time.

### The whole MCU line says "silent"

No Bridge messages at all. Either the sketch didn't build, didn't flash, or is stuck. Check the
build output, then the serial monitor for `Telltale MCU ready`.

Worth knowing: the LED matrix stays dark for the first 30 seconds by design so it doesn't fight
the boot animation. A blank matrix right after a restart is normal, not a hang.

### Vibration never records but the sensor is fine

The settle timer. A vibration reading is only taken once the tag has been held still for
`settle_s` (1.5 s default). If the camera is hand-held or the rig is drifting, the stability
streak keeps resetting and the settle timer never matures. The channel note says exactly this.

### Vibration readings look implausible

Check the stand-off distance on the Readings tab. If it says 1900 mm and the machine is 300 mm
away, the rangefinder is pointed past the target and measuring the far wall. That's what the
`min_distance_mm` / `max_distance_mm` window is for — readings outside it get stored as invalid
rather than being taken seriously.

Also: a very *low* RMS isn't automatically good news. A machine that's switched off doesn't
vibrate.

### Temperature reads much too low

Emissivity. Bare shiny metal reflects rather than emits and reads far cooler than it is. A
square of matte tape or paint on the spot you're watching solves it.

If it's the *hot spot* that's implausible, check the `hot_x` / `hot_y` coordinates — the array
has a wide field of view and may well have found a lamp, a window or a person instead of your
machine.

## App and platform problems

<a id="the-header-says-robust-z-instead-of-isolationforest"></a>
### The header says "robust-z" instead of IsolationForest

`scikit-learn` didn't install. It comes from `python/requirements.txt` via `uv pip install` at
container start, which needs working network access on first launch.

The app deliberately doesn't fail here — it falls back to a median/MAD z-score and carries on,
because a monitoring system that refuses to start because it couldn't reach PyPI is worse than
one running a simpler detector. But you'll want the real thing.

Look for the install in the startup logs. Once it succeeds, the result is cached against a hash
of `requirements.txt` and you'll see `Requirements already installed.` on subsequent starts.

### Code changes don't take effect

Editing files isn't enough; the app has to be restarted to reload Python. Worse, the tooling
syncs edited files to the board **at the end of a turn**, so restarting immediately after an
edit can run the *previous* version.

The tell is a traceback whose source lines don't match the file you just edited — Python renders
the source from disk but is executing bytecode loaded at startup. If you see your new code in
the traceback but the old error, the process is stale. Restart again.

See [development](development.md#the-edit-sync-restart-loop) for the reliable sequence.

### Everything returns "connection refused" on port 8800

That's the App Lab daemon, not this app. Worth knowing how to tell the two apart, because the
symptom is that all tooling breaks at once while the board seems fine.

The daemon runs **on the board**. From the board itself it'll answer:

```
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8800/v1/apps    # 200 = daemon healthy
```

If that returns 200 but your PC can't reach `localhost:8800`, the daemon is fine and the
**PC-side port forward has dropped**. Reconnect the board in App Lab — reselect it, or replug
the USB cable. Nothing on the board needs restarting.

### The sketch won't compile after adding a library

Almost certainly dependency bloat. `arduino-app-cli lib add` resolves declared dependencies
transitively, and some libraries declare heavy ones they don't actually need.

Adding `Adafruit MLX90640` pulled in **49 libraries**, because its index entry lists
*Adafruit Arcada* as a dependency. Arcada is only used by that library's examples, but the
resolver doesn't know that, and it drags in SdFat, TinyUSB, Zero DMA and a pile of other
SAMD-only code that has no chance of building for Zephyr.

`sketch.yaml` is pinned by hand to the three libraries that are genuinely needed. If you ever
run a library add against this sketch, **check `sketch.yaml` afterwards and trim it back**.

### Web UI loads but shows no data

Check the connection dot next to the title. Red means `/state` is failing — look at the logs
for an exception in an API handler.

If the preview is a grey box saying "waiting for the camera", `/preview.jpg` is returning 503,
which means no frame has been processed yet. Either the camera hasn't opened, or the capture
loop is throwing before it gets to the preview step — back to the top of this page.

## When you're properly stuck

Collect these before digging in:

1. `apps_logs` output, at least 100 lines — the repeating traceback is usually the whole answer
2. `GET /state` — shows camera, sensors, tags, and per-channel notes in one lump
3. `GET /sensors` — detected vs reporting, and the failure counts
4. The serial monitor, if the MCU is involved

Between the log and `/state` there is very little this app can be doing that you can't see.
