# How it works

The interesting parts, in enough detail to modify them.

---

# Reading a needle gauge

## The problem with reading a dial from a photo

If you crop a gauge out of an image and try to find the needle, you immediately hit three
things that break naive approaches:

1. **The camera is never square-on.** A dial photographed at an angle is an ellipse, and the
   needle's apparent angle is not its true angle. Move the camera and every reading shifts.
2. **The dial is covered in dark lines that aren't the needle** — the bezel, the scale ring,
   tick marks, printed numbers, the manufacturer's logo.
3. **The needle has a tail.** Most pointers extend a little past the pivot, so any "find the
   dark line" method finds two answers 180° apart and has to pick.

Hough line transforms handle none of these well. What follows handles all three.

## Tag space

The AprilTag detector gives four corner points in image pixels. Feed those to
`cv2.getPerspectiveTransform` against the unit square and you get a homography **H** that maps
a made-up coordinate system onto the image:

```
tag space                        image space
(0,0) ────── (1,0)                    ╱───────╲
  │            │        ── H ──▶     ╱  tag    ╲
  │    tag     │                    ╱  as seen  ╲
(0,1) ────── (1,1)                 ╱─────────────╲
```

x runs right along the tag's top edge, y runs down its left edge, and one unit is one tag
width. Now here's the trick: **store the dial's geometry in tag space, not image space.**

The centre of the dial isn't "pixel (812, 344)", it's "1.7 tag-widths left and 2.1 up from the
tag's origin". The needle's minimum position isn't an angle in the image, it's an angle in the
tag's frame.

Move the camera and **H** changes, but the tag-space numbers don't. The perspective distortion
is absorbed by the homography for free, and a calibration done from half a metre away still
works from a metre away and thirty degrees off to the side. For something that's eventually
going on a rover, where the approach pose is never twice the same, this is the whole ballgame.

Corner accuracy is what limits this, which is why the detector runs with
`CORNER_REFINE_APRILTAG` and why the docs nag you about printing tags large.

## Sampling in polar coordinates

To find the needle we sample the dial along rays from the centre:

```python
thetas = np.arange(720) * 0.5              # every half degree
radii  = np.linspace(r_in, r_out, n_r)     # across the search annulus

xs = cx + radii * sin(theta)               # tag space
ys = cy - radii * cos(theta)               # 0° = up, clockwise
```

That builds a `(720, n_r, 2)` grid of points **in tag space**. One call to
`cv2.perspectiveTransform` maps the whole grid into image pixels, and one `cv2.remap` samples
the grayscale image at all of them with bilinear interpolation.

Because the grid is built in tag space and *then* projected, the sampling is a perfect circle
on the physical dial even when the dial is an ellipse in the image. There is no un-warping
step, no rectified crop, no second interpolation. It comes out in the geometry for free.

Rays that fall outside the image get masked. If more than 40% of the grid lands off-frame the
read is abandoned — usually it means the gauge is half out of shot, and a partial dial produces
confident nonsense.

## Subtracting the per-radius median

This is the step that deals with the bezel and the scale ring, and it's two lines:

```python
darkness -= np.median(darkness, axis=0, keepdims=True)
np.clip(darkness, 0.0, None, out=darkness)
```

`darkness` is a `(720, n_r)` array — angle by radius. Taking the median down `axis=0` gives,
for each radius, the typical darkness at that distance from the centre *across all angles*.

Anything concentric — a black bezel ring, a printed arc, the scale circle, a shadow gradient —
is dark at **every** angle for a given radius. So it lands in the median and gets subtracted to
zero.

The needle is dark at **one** angle only. Its median contribution is negligible, so it survives
almost untouched.

What's left is an array that is essentially zero everywhere except along the needle. No
thresholding, no edge detection, no parameters to tune. It's the single highest-leverage line
in the whole reader.

## Finding the pointer, not the tail

Averaging what's left across radius gives a profile over angle, smoothed circularly over about
3°. Peaks in that profile are candidate needle directions.

For a needle with a tail there are two strong peaks 180° apart. Two things disambiguate them:

**Reach.** For each ray, what fraction of the sampled radii stay dark?

```python
ray_max      = darkness.max(axis=1)
lit_fraction = (darkness > 0.35 * ray_max[:, None]).mean(axis=1)
```

The pointer runs from the hub out to the scale, so it's dark across nearly the whole annulus.
The tail usually stops just past the pivot, so it's dark only near the inner edge. Candidates
are scored `profile * (0.7 + 0.3 * reach)` — mostly darkness, with a thumb on the scale for
the one that goes the distance.

*(Incidentally, this is where the bug lived that made the whole app do nothing for a day.
`ray_max` was computed with `keepdims=True`, giving shape `(720, 1)`, which broadcast against
the `(720,)` fraction to produce a 720×720 matrix. `np.convolve` refused it and the capture
loop threw on every single frame. See [troubleshooting](troubleshooting.md#the-loop-throws-on-every-frame).)*

**The calibrated arc.** Candidates are checked strongest-first and the first one that lands
inside the dial's real sweep wins. A tail pointing into the dead zone below the minimum mark is
rejected on sight. If nothing at all lands in the arc, the strongest peak is reported anyway
with `on_scale = False` — that's a real state worth surfacing, not an error.

## Sub-degree precision

The angular grid is 0.5° and the peak sits in one bin, so a raw answer is quantised to half a
degree. On a 270° sweep reading 0–10 bar, that's a 0.02 bar step — usually fine, occasionally
visible as a staircase in the chart.

Fitting a parabola through the peak bin and its two neighbours gets the true maximum:

```python
delta = 0.5 * (y0 - y2) / (y0 - 2*y1 + y2)
```

Cheap, and it removes the staircase.

## Confidence

Confidence answers "how much does this peak stand out from everything else on the dial?"

```python
median   = np.median(profile)
mad      = np.median(np.abs(profile - median))
contrast = (peak - median) / (1.4826 * mad + 1e-6)
confidence = clip(contrast / 8, 0, 1)
```

Median and MAD rather than mean and standard deviation, because the needle itself is an outlier
in its own profile and would inflate a standard deviation. The 1.4826 makes MAD comparable to a
standard deviation for normally distributed data. Dividing by 8 is a calibration constant,
chosen so a clean black-on-white gauge lands near 1.0 — adjust it if your dials read
systematically low.

If a second peak elsewhere on the arc comes within 80% of the winner's score, confidence is
halved and the reading is marked ambiguous. That's usually a two-pointer gauge, a hard shadow,
or a reflection.

## Angle to value, and the dead zone

The naive mapping wraps badly. Consider a gauge with minimum at 225° and maximum at 135°,
sweeping 270° clockwise. A needle sitting at 220° is just barely *below* minimum. Wrap it into
`[0, 360)` and it comes out as 355° of travel — reading as nearly full scale instead of empty.
That's a "boiler is fine" reading on an empty boiler.

So the dead zone gets handled explicitly:

```python
delta = (angle - angle_min) % 360
dead_mid = sweep + (360 - sweep) / 2     # middle of the unused arc
if delta > dead_mid:
    delta -= 360                          # it's below minimum, not above maximum
t = delta / sweep
```

A needle in the dead zone is assigned to whichever end it's closer to, and comes out as a small
negative or slightly-over-1.0 fraction. Anything outside −0.02 to 1.02 is flagged `on_scale =
False`: stored so you can see it, kept out of the model.

## The stability gate

A reading is only taken when the tag has been detected for `STABLE_FRAMES` consecutive frames
with its corners moving less than `STABLE_MAX_SHIFT_PX` between them.

This is the "take a still shot" behaviour, and it's doing two jobs. It waits for the camera and
the subject to stop moving, so the frame is sharp and the corners are trustworthy. And it stops
the app hammering the database while someone walks past waving a tag.

Both numbers are worth tuning to your rig. A hand-held camera needs a looser pixel threshold; a
rigidly mounted one can afford a tighter gate and a lower frame count.

---

# Measuring vibration

## Why the maths lives on the Arduino

An RMS is only meaningful if you know the sample interval. Stream raw distances over the Bridge
into Python and your timestamps are at the mercy of serial latency and Linux scheduling. The
MCU has a real clock and nothing else to do, so it does the arithmetic and sends the answer.

## The sample loop

The VL53L0X runs in continuous mode with a 20 ms timing budget, which works out around 50
samples a second. `readRangeContinuousMillimeters()` blocks until a new reading is ready — that
blocking call is what paces the entire sketch loop, which is convenient rather than a problem.

Samples land in a fixed buffer. Once a second the buffer is reduced and cleared:

- **mean** — the stand-off distance, used for the sanity window
- **RMS** of deviations from that mean — the vibration figure
- **peak-to-peak** — max minus min, the alternative metric
- **dominant frequency** — zero crossings of the mean-removed signal, divided by two, divided
  by the window length

Windows with fewer than 10 samples are thrown away rather than published. That happens when a
thermal frame read has eaten into the window, and a short window gives a badly biased RMS.

## Honest limits

Zero-crossing frequency estimation over a 1-second window gives about 1 Hz resolution and
assumes a single dominant mode. With two vibration sources it reports something between them
that corresponds to nothing physical. It's a hint, not a measurement.

An FFT would be better. If you want one, the place to put it is the MCU: buffer two seconds,
transform there, send the top few bins as extra `tof_summary` arguments. The Python side barely
changes.

And the sensor floor is the real ceiling on all of this. ~1 mm quantisation with noise on top
means gross motion only.

---

# Reading temperature

## The refresh-rate trap

The MLX90640 builds a frame from two interleaved subpages, and `getFrame()` **blocks** until
both are ready. That makes the refresh rate setting much more consequential than it looks:

| Refresh rate | `getFrame()` blocks for | Effect on vibration sampling |
| --- | --- | --- |
| 2 Hz | ~1000 ms | Catastrophic — an entire window destroyed |
| 8 Hz | ~250 ms | Bad — a quarter of every window gone |
| **16 Hz** | **~125 ms** | Acceptable — the choice we made |
| 32 Hz | ~62 ms | Less disruptive, noticeably noisier data |

16 Hz is the compromise. The extra per-frame noise at that rate mostly washes out when you're
reducing 768 pixels to a min, a mean and a max anyway.

The read is also *scheduled* to hurt less: it happens immediately after a vibration window is
published, and the window timer is restarted afterwards. So the stall lands at the boundary
between windows instead of chopping one in half.

## Why the coldest pixel is "background"

Adafruit's driver exposes an ambient temperature, but it's the sensor die's own temperature —
which tells you how warm the breakout board is, not how warm the room is. Not useful as a
reference for "is this bearing hotter than its surroundings".

So the coldest pixel in the frame stands in for the background instead. It's a decent proxy in
practice: the coldest thing in a frame containing a hot machine is usually the wall behind it.

That's what makes the `delta_ambient` metric work — hot spot minus background, in kelvin,
largely immune to the room heating up over the course of a day.

It does fail if there's something genuinely cold in shot, like a window on a winter day or a
chilled pipe. If your frame has one of those, use absolute `max` and accept the seasonal drift,
or re-aim to exclude it.
