# 🎯 Telltale

> *telltale* (n.) — an instrument or marker that reveals a condition you couldn't otherwise
> see: the warning lamps on a dashboard, the ribbons on a sail, the stain that gives away a
> slow leak.

Point a camera at an AprilTag stuck next to a piece of equipment, and this thing reads the
gauge on it, measures how much it's shaking, checks how hot it's running, writes all of it
to a database, and tells you when something's drifting toward trouble.

Old machinery can't report anything about itself. This makes it talk.

It runs entirely on an Arduino UNO Q. No cloud, no network needed after setup.

```
                    ┌─ camera ────────▶ AprilTag ─▶ needle angle ─▶ value ─┐
tag says "this is   │                                                      │
 pump-3, watch all  ├─ VL53L0X ───────▶ 50 Hz distance ─▶ RMS wobble ──────┤
 three channels"    │                                                      │
                    └─ MLX90640 ──────▶ 32×24 thermal ─▶ hot-spot °C ──────┤
                                                                           │
                                          ┌────────────────────────────────┘
                                          ▼
                             limits · IsolationForest · trend forecast
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                     SQLite        Web UI :7000        LED matrix
```

## The idea

One AprilTag identifies one **asset**. When you assign a tag, you pick which of three
channels that asset gets monitored on — gauge, vibration, temperature, or any mix. A boiler
gauge might only need the gauge channel. A pump might want all three. A bearing housing with
no dial at all might just be vibration and temperature.

Each channel keeps its own history and its own anomaly model. The asset's overall status is
whichever channel is unhappiest.

If a sensor isn't plugged in, that channel is quietly skipped and everything else carries on.
That's deliberate: you shouldn't have to own all the hardware to use part of the system.

## Getting started

You need an UNO Q, a USB camera, and some printed 36h11 AprilTags. The two I²C sensors are
optional.

1. Start the app in Arduino App Lab. First launch takes a few minutes — it pulls
   `scikit-learn` and builds the sketch.
2. Open `http://<board-ip>:7000`.
3. Put a tag in view, go to **Assign tag**, give it an id, tick the channels you want, and
   save. For the gauge channel you'll click four points on a frozen frame.
4. Readings start on their own. Watch them on the **Readings** tab.

Full walkthrough in [docs/setup.md](docs/setup.md) and [docs/calibration.md](docs/calibration.md).

## Documentation

| | |
| --- | --- |
| [Setup](docs/setup.md) | Hardware, wiring, printing and mounting tags, first run |
| [Calibration](docs/calibration.md) | Assigning tags and configuring each channel |
| [Architecture](docs/architecture.md) | How the pieces fit together and why |
| [How it works](docs/how-it-works.md) | The needle-reading algorithm, vibration maths, thermal handling |
| [Predictive maintenance](docs/predictive-maintenance.md) | What the model does and how to read it |
| [HTTP API](docs/api.md) | Every endpoint, with request and response shapes |
| [Data model](docs/data-model.md) | Database schema and migrations |
| [Troubleshooting](docs/troubleshooting.md) | Things that go wrong, and what they mean |
| [Development](docs/development.md) | Working on the code, the edit-sync-restart loop, gotchas |

## What's actually been tested

Being straight about this, because half-finished projects that claim to work waste everyone's
time.

**Confirmed working on hardware:** camera capture at 1280×720, AprilTag detection and the
stability gate, gauge calibration through the web UI, SQLite storage, the web UI itself,
`scikit-learn` installing on first boot and the IsolationForest backend coming up.

**Fixed but not yet re-run:** the needle reader. It was crashing on every frame due to a numpy
broadcasting mistake (see [troubleshooting](docs/troubleshooting.md#the-loop-throws-on-every-frame)).
The fix is in, the restart hasn't happened yet.

**Written but never executed:** everything to do with the two I²C sensors — the vibration and
temperature channels, `sensors.py`, and the sketch that drives the VL53L0X and MLX90640. The
sketch has not been compiled since those libraries were added. Treat that half as a first
draft that needs a bring-up session, not as working code.

## Layout

```
app.yaml              app metadata + brick list
python/
  main.py             orchestration, capture loop, HTTP API
  config.py           settings and the asset/channel config model
  vision.py           AprilTag detection and needle reading
  sensors.py          receives MCU sensor summaries over the Bridge
  predictive.py       IsolationForest + trend forecasting
  store.py            SQLite persistence
  requirements.txt    scikit-learn (installed at first launch)
sketch/
  sketch.ino          I²C sensors, Bridge publishing, LED matrix status
  sketch.yaml         pinned Arduino libraries
assets/               web UI (plain HTML/CSS/JS, no build step)
docs/                 you are here
```
