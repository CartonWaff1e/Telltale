# Assigning tags and calibrating channels

Everything here happens on the **Assign tag** tab of the web UI.

## The basic flow

1. Get the tag in view of the camera.
2. Give the asset an id (`pump-3`) and optionally a human label ("Coolant pump, north wall").
3. Pick the tag id from the dropdown. It lists every tag currently visible.
4. Tick the channels you want.
5. Fill in each channel's settings.
6. Save.

The asset id is what everything else keys off — database rows, charts, events. Pick something
short and stable. Changing it later creates a *new* asset rather than renaming the old one,
and the history stays with the old id.

## Gauge channel

This is the fiddly one, because the app has to learn the geometry of your particular dial.

### Clicking the four points

Hit **Freeze frame**. You get a still with the detected tag outlined in yellow. Then click, in
this order:

1. **The centre of the dial** — where the needle pivots. Be accurate here; every angle is
   measured from this point, so an error at the centre becomes an error in every reading, and
   it gets worse toward the middle of the scale.
2. **A point on the rim** — anywhere on the outer edge of the dial face. This sets the radius.
3. **The needle tip at the minimum mark** — not where the needle is *now*, where it *would* be
   if it read minimum. Look at the printed scale and click the outer end of the lowest
   graduation.
4. **The needle tip at the maximum mark** — same idea, the top of the scale.

Points 3 and 4 define the arc and its direction. The app assumes the needle sweeps **clockwise**
from minimum to maximum, which is true of essentially every gauge you'll meet. The leftover arc
between max and min becomes the dial's dead zone.

Made a mess? **Clear points** and start the clicking again. You don't need to re-freeze unless
the camera or the gauge moved.

### Filling in the scale

| Field | What it means |
| --- | --- |
| Unit | Free text, shown everywhere. `bar`, `psi`, `°C`, `%`. |
| Value at min / max | The numbers printed at the two ends of the scale. |
| Needle is dark | Nearly always yes — a black needle on a white face. Flip it for an inverted or backlit dial. |
| Warn / alarm low / high | Optional. Leave blank for "don't check that side". |
| Search from / to (× radius) | The annulus the needle is hunted in, as a fraction of the radius. |

The search annulus defaults to 0.25–0.92 and usually wants leaving alone. Two reasons to
change it:

- **Big central hub or a logo in the middle** confusing the reader → raise the inner value.
- **A dark scale ring or numbers near the rim** stealing the peak → lower the outer value.

### Test read

**Always press Test read before saving.** It re-reads the frozen frame with the geometry you
just defined and tells you what value it got, at what angle, with what confidence, plus a crop
of the dial with the detected needle drawn on it.

Compare that number to what your eyes see on the dial. If they agree, save. If they don't,
something's off and it's almost always one of:

| Symptom | Usually means |
| --- | --- |
| Value is mirrored (reads high when it should be low) | Min and max clicks swapped |
| Value is consistently offset by a bit | Centre click was off |
| "needle is outside the calibrated arc" | Min/max points too close together, or the needle really is off-scale |
| "more than one strong streak on the dial" | A second pointer, a shadow, or a bold printed line is competing with the needle |
| Low confidence (< 0.3) | Poor contrast, glare, or the annulus is picking up the scale ring |

Confidence below `GAUGE_MIN_CONFIDENCE` (0.25 by default) means readings get stored but not
fed to the model. If you can't get above that, fix the lighting before you fix the settings —
glare on a gauge glass defeats every algorithm equally.

## Vibration channel

Much simpler, because there's no geometry to learn. The rangefinder measures distance at
~50 Hz and the wobble in that distance *is* the vibration.

Watch the live line at the top of the section while you set thresholds:

```
Live: RMS 0.412 mm, peak-to-peak 2.00 mm, stand-off 340 mm, 12.0 Hz
```

| Field | Guidance |
| --- | --- |
| Metric | `RMS deviation` is the default and the better behaved of the two. `Peak-to-peak` is more intuitive but far more sensitive to a single noisy sample. |
| Settle time | How long the tag must be held still before a vibration reading is trusted. Default 1.5 s. Raise it if your mount takes a while to stop ringing. |
| Warn / alarm above | Set these from the live reading with the machine running normally. |
| Min / max stand-off | Sanity window on distance. Readings outside it are stored as invalid. |

**Getting the thresholds right:** run the machine in its normal healthy state, watch the live
RMS for a minute, and note the range. Set warn maybe 50–100% above the top of that, and alarm
higher again. Then — and this is the bit people skip — come back after a week and look at the
chart. Real baselines are wider than you think, and the trend model needs a few dozen readings
before it has an opinion worth listening to.

There's no low limit on this channel by design. A machine that suddenly stops vibrating has
usually stopped running, which is a different alarm.

### Know what this can and can't see

The VL53L0X quantises distance to about a millimetre and is noisy at that scale. What you get
is a decent measure of **gross** motion: an unbalanced fan, a loose mount, a shaft starting to
wander. What you do **not** get is a bearing signature — that lives in the hundreds of Hz and
micrometres, and no time-of-flight sensor is going to find it.

The frequency number is a zero-crossing count over a one-second window. That gives roughly 1 Hz
resolution, and it only means anything if there's a single dominant mode. If the machine has
several vibration sources, treat the number as a rough hint, not a measurement.

## Temperature channel

The thermal array gives a 32×24 image once a second and the sketch reduces it to min, mean,
max and the coordinates of the hottest pixel. You choose which of those to alarm on.

| Metric | Use it when |
| --- | --- |
| `max` — hottest pixel | Default. Best for bearings, motors, anything with a localised hot spot. |
| `mean` — frame average | The whole object's temperature matters more than any one point. |
| `min` — coldest pixel | Rare. Useful for checking something is staying cold. |
| `delta_ambient` — hottest minus coldest | **The one to reach for in a variable environment.** |

That last one deserves a note. It reports how far the hot spot stands above its own
background, using the coldest pixel in the frame as the reference. If your workshop swings
15 °C between morning and afternoon, an absolute `max` threshold will false-alarm every summer
afternoon and miss a genuine fault every winter morning. `delta_ambient` cancels most of that
out. Its unit is K rather than °C, since it's a difference.

Setting thresholds is the same story as vibration: watch the live readout with the machine
healthy, then set warn and alarm above the normal band, then revisit after a week of real data.

### Caveats worth knowing

- **Emissivity isn't compensated.** The MLX90640 assumes a fairly high emissivity. Bare shiny
  metal reads *much* cooler than it actually is. A patch of matte paint or a strip of
  electrical tape on the spot you're watching fixes this and costs nothing.
- **The array sees a wide field.** The "hottest pixel" might be a lamp, a window, or someone's
  hand passing through. Check the hot-spot x/y coordinates on the Readings tab occasionally to
  make sure it's tracking the thing you meant.
- **It has no idea where the tag is.** See the aiming section in [setup](setup.md#aiming-them).

## Editing an asset later

Saving an asset with an existing id overwrites it. The stored readings are untouched, which is
usually what you want for a small threshold tweak.

It is *not* what you want after re-clicking the dial geometry. Fresh geometry means the values
shift, and the anomaly model will treat the whole new run as anomalous compared to the old
baseline. After a re-calibration, go to **History** and clear that channel so the model starts
clean.

Deleting an asset removes its configuration but keeps its readings in the database. Re-creating
it with the same id picks up the old history — occasionally handy, occasionally surprising.
