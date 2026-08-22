# Setup

## What you need

**Required**

- Arduino UNO Q
- A USB camera. Anything UVC-class works; the board reports it as a generic UVC device. A
  USB-C hub with USB-A ports is the easy way to attach one.
- AprilTags from the **36h11** family, printed.

**Optional, per channel**

- `vibration` — VL53L0X time-of-flight breakout, I²C address `0x29`
- `temperature` — MLX90640 32×24 thermal array, I²C address `0x33`

Skip either sensor and that channel just never becomes available. The app doesn't care.

## Printing tags

Generate 36h11 tags from [this generator](https://chaitanyantr.github.io/apriltag.html), or in
Python with `cv2.aruco.generateImageMarker` using `cv2.aruco.DICT_APRILTAG_36h11`.

Practical advice, learned the annoying way:

- **Print big.** The tag needs to be at least about 30 pixels across in the camera image
  before the app will trust it (`MIN_TAG_EDGE_PX`), and everything downstream — the dial
  geometry, the needle angle — is derived from the positions of its four corners. A tag that's
  barely detectable gives you a barely usable homography. If you're standing off a metre,
  print it 80–100 mm square.
- **Mount it flat and mount it rigid.** Tape flapping in a draught will wreck the stability
  gate. Foam board or an adhesive metal plate is better than paper on a curved pipe.
- **Keep it in the same plane as the dial face.** This is the one people get wrong. If the tag
  is stuck on the pipe below a gauge that faces a different direction, the tag-space geometry
  doesn't correspond to the dial's geometry any more and your readings skew as you move.
- **Matte, not glossy.** Specular glare across a tag kills detection instantly, and it's
  usually the reason a tag that worked yesterday doesn't today.
- **Give it a quiet border.** The detector needs white space around the black frame. A few
  millimetres is plenty.

Each tag needs a unique id if you're monitoring more than one asset. The app matches assets by
tag id, so two assets sharing an id will confuse it — the first one found wins.

## Wiring the sensors

Both sensors go on the **MCU's I²C bus** — the Qwiic connector. Not the Linux side. The sketch
owns the bus and pushes summaries to Python over the Bridge.

They share the bus happily; the addresses don't collide:

```
UNO Q Qwiic ──┬── VL53L0X   (0x29)
              └── MLX90640  (0x33)
```

The bus runs at 400 kHz. The MLX90640 wants that speed — a full frame is 1664 bytes and at
100 kHz the read would take long enough to disrupt the vibration sampling.

If you're daisy-chaining Qwiic connectors, keep the total cable run short. I²C at 400 kHz over
a long unshielded run next to a motor is asking for trouble, and the failure mode is
intermittent bad frames rather than an obvious break.

### Aiming them

This matters more than the wiring: **the tag says *what* you're measuring, but the sensors
measure *wherever they're pointed*.**

The camera sees the tag and identifies the asset. The VL53L0X measures whatever is in its
cone. The MLX90640 images whatever is in its field of view. Nothing verifies that those are
the same object.

So boresight them. Mount all three so that when the tag comfortably fills the camera frame,
the rangefinder and the thermal array are looking at the part of the machine you actually care
about — the bearing housing, the motor casing, whatever. Then check it once with the live
readouts on the **Assign tag** tab before you trust any thresholds.

The `min_distance_mm` / `max_distance_mm` window on the vibration channel exists as a sanity
check for exactly this. If the rangefinder is reading 1900 mm when the machine should be 300 mm
away, it's pointed past the target, and the reading gets stored as invalid rather than treated
as "very stable machine".

## First run

1. Open the app in Arduino App Lab and start it.
2. **Be patient the first time.** Three slow things happen on first launch: the Docker image
   is pulled if it isn't cached, `uv pip install` fetches `scikit-learn` and `scipy` from
   PyPI, and the sketch gets compiled and flashed. Several minutes is normal. Subsequent
   starts are quick because the requirements install is cached against a hash of
   `requirements.txt`.
3. Watch the logs. You want to see, in roughly this order:

   ```
   Requirements already installed.          (or the install running)
   ======== App is starting =====================
   SQLStore:  Connected to SQLite database: /app/data/gauge_reader.db
   telltale: telltale ready - N asset(s), anomaly backend=IsolationForest
   WebUI:  The application interface is available here:
     - Network URL: http://192.168.x.x:7000
   V4LCamera:  Successfully started usb:GENERAL - UVC
   telltale: camera started at 1280x720
   ```

   `anomaly backend=IsolationForest` means scikit-learn made it. If it says `robust-z` instead,
   the install didn't happen — check network access and see
   [troubleshooting](troubleshooting.md#the-header-says-robust-z-instead-of-isolationforest).

4. Open the network URL in a browser.
5. On the **Assign tag** tab, the two sensor lines under "MCU sensors" tell you whether the
   sketch found your hardware. `ToF ok` / `Thermal ok` means summaries are arriving. `not
   detected` means the I²C probe at boot got no answer at that address.

## Camera notes

The camera opens at 1280×720 by default. The driver may report something like
`FPS set to 30 instead of requested 10` — that's harmless, it just means the webcam wouldn't
honour the requested rate and the capture loop is paced by whatever it does give.

If you need a different resolution or a network camera, use the environment variables in the
[tuning table](../README.md). Higher resolution makes small or distant tags detectable but
costs CPU on every frame; 720p is a reasonable middle.

If the camera is unplugged or busy, the app doesn't die. It logs the error, serves the UI
anyway so you can see what's wrong, and retries every 10 seconds.
