# Predictive maintenance

Every channel of every asset gets its own instance of `PredictiveMaintenance`. It sees one
number per capture and produces a verdict.

Three signals feed that verdict, and they answer genuinely different questions:

| Signal | Question it answers |
| --- | --- |
| **Range** | Is this value outside the limits I set? |
| **Anomaly** | Is this value unlike what this channel normally does? |
| **Trend** | Where is this heading, and when does it get there? |

Range catches the thing you already knew to look for. Anomaly catches the thing you didn't.
Trend catches the thing that hasn't happened yet.

## Range

The simplest and the most reliable. Four optional bounds — `warn_low`, `warn_high`,
`alarm_low`, `alarm_high` — checked in order of severity, alarms before warnings. Any bound left
as `None` isn't checked.

Don't skip this because the model sounds cleverer. A hard limit is the only part of the system
that works on day one with zero history, and it's the part you can explain to someone else.

## Anomaly detection

`IsolationForest` from scikit-learn, over five features per reading:

```python
[ value,                 # where it is
  delta,                 # change since the last reading
  rolling_mean(12),      # recent baseline
  rolling_std(12),       # recent volatility
  rate_per_hour ]        # change scaled by elapsed time
```

Only the first is the raw measurement. The rest give the model a sense of dynamics — a value
that's fine in isolation but arrived by jumping 30% in ten minutes is a different animal from
one that drifted there over a week.

`rate_per_hour` is clamped, because a long gap between captures otherwise divides a normal
change by a tiny elapsed time and produces a feature in the millions that dominates every split
in every tree.

**Why IsolationForest.** It's unsupervised, which matters because nobody is going to hand-label
faults on your pump. It doesn't assume the data is normally distributed, which sensor data
mostly isn't. It's tree-based, so the features don't need scaling — handy when they're a mix of
absolute values, deltas and rates. And it's fast enough to refit on a board without anyone
noticing.

**Timing.** Nothing happens until 40 readings exist (`PDM_MIN_SAMPLES`). After that it refits
every 25 new readings, on up to 2000 rows of history, with `max_samples` capped at 256. On the
UNO Q a refit is a few hundred milliseconds, done inline in the capture loop. At one capture
every 30 seconds nobody sees it.

**Reading the score.** `decision_function` returns a signed number. Negative means anomalous,
and further from zero means more confident either way. `contamination` is 0.03, which tells the
model to expect about 3% of readings to be odd. Turn it down if you're getting noise, up if
genuine problems are slipping through — but see the honest bit below first.

**Without scikit-learn** the app falls back to a robust z-score: median and MAD over the
history, flagging anything beyond 3.5 sigma. Cruder, no notion of dynamics, but it works with
numpy alone and it's better than nothing. The web UI header always tells you which one is live.

## Trend and forecast

Least squares over the last 30 readings, fitting value against time in hours:

```
slope, intercept = np.polyfit(t_hours, values, 1)
```

R² comes along with it. If R² is below 0.5 the trend is treated as noise and no forecast is
made — a "trend" fitted through scatter is worse than no trend, because it produces confident
predictions from nothing.

When the fit is good enough, the line is extrapolated to whichever alarm limit it's heading
toward:

```
hours_to_limit = (alarm_high - current_value) / slope
```

Sanity bounds apply: the answer must be positive, finite, and less than a year. A forecast of
"11 months" is not actionable and just clutters the UI.

This is deliberately a straight line. Not exponential, not seasonal, not ARIMA. With 30 points
of noisy sensor data, a linear fit is about the most complex model the data can actually
support, and the failure mode of a straight line — being a bit early or late — is much friendlier
than the failure mode of an overfitted curve.

## Combining into a status

```
ALARM   value is outside an alarm band
WATCH   value is outside a warning band
        OR the anomaly detector flagged it
        OR the trend hits an alarm limit within 24 hours
OK      none of the above
```

An asset with several channels takes the worst status among them. That rolls up to the LED
matrix and the header pill.

Every assessment carries a `reasons` list in plain English, which is what the UI shows and what
gets written into the events table:

```
2.34 bar is above the warning limit of 2.00 bar
IsolationForest flagged this reading as unlike the previous 137
trend of +0.031 bar/h reaches the alarm high limit in 18.4 h
```

Debugging "why did this alarm" from a boolean is miserable. From a sentence it's obvious.

## What gets into the model, and what doesn't

Readings the app doesn't trust are **stored but not modelled**:

- a needle outside the calibrated arc
- gauge confidence below `GAUGE_MIN_CONFIDENCE`
- a vibration stand-off outside the configured distance window

They go into the database with `valid = 0`, a `WATCH` status, and a reason. They appear in the
table and in the events log. They never touch `model.update()`.

This matters more than it sounds. Feed misreads into the training data and the model learns
that misreads are normal, and then it stops flagging them — which is exactly backwards. A gap
in the timeline is honest. A polluted baseline is not, and it's very hard to notice.

## Being realistic about all this

Some things worth saying out loud, because "AI-powered predictive maintenance" invites more
confidence than the situation deserves.

**Forty readings is not a baseline.** It's the point at which the model is allowed to have an
opinion, not the point at which that opinion is worth much. At one capture every 30 seconds
while parked in front of a machine a few times a day, expect a week or two before the anomaly
scores mean anything. The UI says "warming up" until the first fit, but the first fit is not
maturity.

**Anomalous is not the same as bad.** The model flags *unusual*. Someone cleaning the gauge
glass is unusual. Sun through a window hitting the thermal array is unusual. First cold start
after a shutdown is unusual. Expect false positives early and treat the first month as
tuning rather than monitoring.

**Everything assumes the sensors stay pointed at the same thing.** Move the mount and every
channel's baseline is invalid. There's no drift detection for that. If you re-aim or
re-calibrate, clear the affected channel's history and let it relearn.

**Sampling is sparse and irregular by design.** These are spot checks, not continuous
monitoring. A fault that develops and destroys a bearing between two visits is invisible to
this system. It's built to catch slow drift, which is what most of maintenance actually is —
but it is not a protection system and it should never be the only thing standing between a
machine and a failure.
