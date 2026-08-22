# Data model

SQLite, at `/app/data/gauge_reader.db` on the board, through the `dbstorage_sqlstore` brick.

The filename still says `gauge_reader` from before the project was renamed to Telltale. It
stays that way deliberately — renaming it would orphan every stored calibration and reading
for no benefit.

Three tables: `readings`, `assets`, `events`. Everything is written from a single thread, and
the brick serialises access behind its own lock, so there's no concurrency story to worry
about.

## `readings`

**One row per (capture, channel).** A three-channel asset produces three rows sharing a
timestamp.

| Column | Type | |
| --- | --- | --- |
| `id` | INTEGER PK | autoincrement |
| `ts` | TEXT | ISO-8601 UTC |
| `ts_epoch` | REAL | unix seconds — what you sort and filter on |
| `asset_id` | TEXT | |
| `tag_id` | INTEGER | the tag that triggered this capture |
| `channel` | TEXT | `gauge` \| `vibration` \| `temperature` |
| `value` | REAL | the modelled number, in `unit` |
| `unit` | TEXT | `bar`, `mm`, `C`, `K`, whatever you configured |
| `confidence` | REAL | 0–1. Meaningful for gauge; 1.0 or 0.0 for the sensors |
| `valid` | INTEGER | 1 if this fed the model, 0 if it was rejected |
| `status` | TEXT | `OK` \| `WATCH` \| `ALARM` |
| `range_state` | TEXT | `normal` \| `warn_*` \| `alarm_*` \| `unknown` |
| `anomaly_score` | REAL | negative is anomalous; NULL before the model is ready |
| `is_anomaly` | INTEGER | |
| `trend_per_hour` | REAL | slope of the recent fit |
| `trend_r2` | REAL | quality of that fit |
| `hours_to_limit` | REAL | forecast, NULL when there isn't a credible one |
| `reasons` | TEXT | the human-readable explanations, joined with ` \| ` |
| `detail` | TEXT | JSON, channel-specific — see below |
| `image` | TEXT | base64 JPEG, gauge only, newest rows only |
| `image_type` | TEXT | `image/jpeg` or NULL |

### Why one row per channel

The obvious alternative is one wide row per capture with `gauge_value`, `vib_rms`, `temp_max`
and so on. It was tempting — a single row is easier to eyeball.

It falls apart quickly. Channels are independent: a capture might get a temperature and skip
vibration because the tag hadn't settled. That's a row full of NULLs whose meaning depends on
other columns. Each channel needs its own status, its own anomaly score, its own trend — so
every one of those columns has to be triplicated. And adding a fourth sensor means an
`ALTER TABLE` and touching every query.

One row per channel makes each row self-contained, and the per-channel views the UI wants are
just `WHERE channel = ?`. Adding a channel needs no schema change at all.

### The `detail` blob

Channel-specific extras that don't deserve columns of their own, stored as JSON:

```jsonc
// gauge
{ "angle_deg": 271.5, "fraction": 0.184, "on_scale": true,
  "ambiguous": false, "notes": [] }

// vibration
{ "metric": "rms", "rms_mm": 0.412, "peak_to_peak_mm": 2.0,
  "mean_mm": 341.2, "dominant_hz": 12.0, "samples": 48, "age_s": 0.3 }

// temperature
{ "metric": "max", "min_c": 21.4, "mean_c": 28.9, "max_c": 61.2,
  "ambient_c": 21.4, "hot_x": 18, "hot_y": 11, "age_s": 0.6 }
```

JSON in a column is a compromise. You lose the ability to index or aggregate on those fields in
SQL, which is fine here because nothing does — they're for display and forensics. If you find
yourself wanting `AVG(dominant_hz)`, promote it to a real column.

### Images

Only gauge readings store one: a crop around the dial with the detected needle drawn on it.
It's there so that six weeks later, when a reading looks wrong, you can see what the camera
actually saw.

They're pruned aggressively. After each write, every row except the newest `GAUGE_KEEP_IMAGES`
(default 40) has its `image` set to NULL. The numbers stay forever; the pictures are a rolling
window. A base64 JPEG is a few tens of kilobytes and a board's flash is not infinite.

## `assets`

| Column | Type | |
| --- | --- | --- |
| `asset_id` | TEXT PK | |
| `tag_id` | INTEGER | duplicated out of the blob so it could be indexed |
| `payload` | TEXT | the whole `AssetConfig` as JSON |
| `updated` | TEXT | ISO-8601 UTC |

The config lives as a JSON blob rather than as columns, and that's deliberate. Channel
configuration changes shape often — a new metric, another threshold, a per-channel setting —
and there are never more than a handful of assets, so nothing is gained by normalising it.
Writes use `INSERT ... ON CONFLICT DO UPDATE`, so saving an existing id overwrites cleanly.

## `events`

| Column | Type | |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `ts`, `ts_epoch` | TEXT, REAL | |
| `asset_id` | TEXT | |
| `channel` | TEXT | empty for asset-level events |
| `severity` | TEXT | `info` \| `warning` \| `error` |
| `code` | TEXT | `asset_watch`, `channel_unavailable`, `gauge_alarm`, ... |
| `message` | TEXT | human-readable |

Events are written on **transitions**, not on every reading. A channel that sits in `WATCH` for
three days produces one event, not thousands. Same for sensor availability: one event when a
channel goes dark, one when it comes back.

That keeps the log readable, which is the entire point of having one.

## Migrating from the single-channel version

The first version of this app stored one row per gauge reading with no `channel` column, and
kept calibrations in a `calibrations` table keyed by `gauge_id`. On startup `Store._migrate()`
sorts that out:

1. If `readings` exists without a `channel` column, it's renamed to `readings_v1`. Same for
   `events` → `events_v1` if it lacks `asset_id`. Nothing is dropped — the old data is still
   there if you want it.
2. Fresh tables are created with the new schema.
3. If a `calibrations` table exists and `assets` is empty, each old calibration is converted
   into an asset with the gauge channel enabled and the same id.

Step 3 is the one that matters, because re-clicking a dial calibration is tedious and nobody
wants to do it twice. `AssetConfig.from_dict` and `GaugeCalibration.from_dict` both accept the
old flat shape (`warn_high` at the top level rather than nested under `limits`), so old JSON
loads without a conversion pass.

The migration is idempotent. It checks whether the target table already exists before renaming
and bails out with a warning rather than clobbering anything.

## Poking at it directly

The database is a normal SQLite file. From the board:

```sql
-- what has this asset been doing today
SELECT ts, channel, value, unit, status
FROM readings
WHERE asset_id = 'pump-3' AND ts_epoch > strftime('%s','now') - 86400
ORDER BY ts_epoch DESC;

-- rejected readings, and why
SELECT ts, channel, value, reasons FROM readings WHERE valid = 0 ORDER BY ts_epoch DESC LIMIT 20;

-- how much history does each model actually have
SELECT asset_id, channel, COUNT(*) AS n, MIN(ts), MAX(ts)
FROM readings WHERE valid = 1 GROUP BY asset_id, channel;
```

That last query is the one to run before believing an anomaly score. If `n` is 45, the model
technically works and doesn't know much yet.

## Growth

One capture writes one row per enabled channel, at a floor of 30 seconds apart per asset. Rows
are a few hundred bytes without the image. A three-channel asset captured continuously would
add roughly 8600 rows a day — call it 3 MB. In the intended duty cycle, a rover visiting a
handful of assets a few times a day, it's a rounding error.

There's no automatic retention policy on the numeric data. If you end up running this
continuously for a year, add a `DELETE FROM readings WHERE ts_epoch < ...` on a schedule, or
downsample the old rows into hourly averages.
