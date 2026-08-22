# SQLite persistence for readings, asset configs and events.
#
# One row per (capture, channel): a gauge reading, a vibration reading and a temperature
# reading taken at the same moment are three rows sharing a timestamp. That keeps every
# channel on its own timeline and means adding a fourth sensor later needs no schema work.
#
# Anything that embeds a user-supplied value goes through execute_sql with bound
# parameters rather than a formatted condition string.

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from arduino.app_bricks.dbstorage_sqlstore import SQLStore

import config

log = logging.getLogger(__name__)

READINGS_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "ts": "TEXT",
    "ts_epoch": "REAL",
    "asset_id": "TEXT",
    "tag_id": "INTEGER",
    "channel": "TEXT",
    "value": "REAL",
    "unit": "TEXT",
    "confidence": "REAL",
    "valid": "INTEGER",
    "status": "TEXT",
    "range_state": "TEXT",
    "anomaly_score": "REAL",
    "is_anomaly": "INTEGER",
    "trend_per_hour": "REAL",
    "trend_r2": "REAL",
    "hours_to_limit": "REAL",
    "reasons": "TEXT",
    "detail": "TEXT",
    "image": "TEXT",
    "image_type": "TEXT",
}

ASSET_COLUMNS = {
    "asset_id": "TEXT PRIMARY KEY",
    "tag_id": "INTEGER",
    "payload": "TEXT",
    "updated": "TEXT",
}

EVENT_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "ts": "TEXT",
    "ts_epoch": "REAL",
    "asset_id": "TEXT",
    "channel": "TEXT",
    "severity": "TEXT",
    "code": "TEXT",
    "message": "TEXT",
}


def utc_now() -> tuple[str, float]:
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


class Store:
    # The filename deliberately still says gauge_reader: the project was renamed to
    # Telltale, but renaming the file would orphan every existing calibration and reading.
    def __init__(self, database_name: str = "gauge_reader.db"):
        self.db = SQLStore(database_name)
        # create_table() opens the connection on demand and SQLStore.start() is a no-op
        # once connected, so building the schema here is safe before App.run().
        self._migrate()
        self.db.create_table("readings", READINGS_COLUMNS)
        self.db.create_table("assets", ASSET_COLUMNS)
        self.db.create_table("events", EVENT_COLUMNS)
        self._migrate_calibrations()

    # -- schema -------------------------------------------------------------
    def _columns(self, table: str) -> list[str]:
        try:
            rows = self.db.execute_sql(f"PRAGMA table_info({table})") or []
        except Exception:
            return []
        return [str(r.get("name")) for r in rows]

    def _migrate(self) -> None:
        """Move v1 tables aside so the multi-channel schema can be created clean.

        v1 stored one row per gauge reading with no channel column, and events keyed by
        gauge_id. The old rows are kept under a _v1 name rather than dropped.
        """
        def rename(table: str, marker: str) -> None:
            columns = self._columns(table)
            if not columns or marker in columns:
                return
            target = f"{table}_v1"
            if self._columns(target):
                log.warning("%s already exists; leaving %s alone", target, table)
                return
            try:
                self.db.execute_sql(f"ALTER TABLE {table} RENAME TO {target}")
                log.info("migrated v1 %r table aside to %r", table, target)
            except Exception as exc:
                log.error("could not migrate %s aside: %s", table, exc)

        rename("readings", "channel")
        rename("events", "asset_id")

    def _migrate_calibrations(self) -> None:
        """Carry v1 gauge calibrations over into the assets table."""
        if not self._columns("calibrations"):
            return
        existing = self.db.execute_sql("SELECT COUNT(*) AS n FROM assets") or [{"n": 0}]
        if int(existing[0].get("n", 0)) > 0:
            return
        rows = self.db.execute_sql("SELECT gauge_id, tag_id, payload FROM calibrations") or []
        for row in rows:
            try:
                raw = json.loads(row["payload"])
                raw.setdefault("asset_id", row["gauge_id"])
                raw.setdefault("tag_id", row["tag_id"])
                asset = config.AssetConfig.from_dict(raw)
                self.save_asset(asset)
                log.info("migrated v1 calibration %r to an asset", asset.asset_id)
            except Exception as exc:
                log.error("could not migrate calibration %s: %s", row.get("gauge_id"), exc)

    # -- readings -----------------------------------------------------------
    def add_reading(self, row: dict[str, Any]) -> None:
        payload = {k: row.get(k) for k in READINGS_COLUMNS if k != "id"}
        # create_table=False: the schema exists and this path allows NULLs, which the
        # brick's type inference would reject.
        self.db.store("readings", payload, create_table=False)

    def recent_readings(self, asset_id: Optional[str] = None, channel: Optional[str] = None,
                        limit: int = 25) -> list[dict[str, Any]]:
        cols = ", ".join(c for c in READINGS_COLUMNS if c != "image")
        where, args = [], []
        if asset_id:
            where.append("asset_id = ?")
            args.append(asset_id)
        if channel:
            where.append("channel = ?")
            args.append(channel)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        args.append(int(limit))
        return self.db.execute_sql(
            f"SELECT {cols} FROM readings{clause} ORDER BY ts_epoch DESC LIMIT ?", tuple(args)
        ) or []

    def series(self, asset_id: str, channel: str, since_epoch: float,
               limit: int = 1000) -> list[dict[str, Any]]:
        return self.db.execute_sql(
            "SELECT ts_epoch, value, status, range_state, is_anomaly, confidence, valid "
            "FROM readings WHERE asset_id = ? AND channel = ? AND ts_epoch >= ? "
            "ORDER BY ts_epoch ASC LIMIT ?",
            (asset_id, channel, float(since_epoch), int(limit)),
        ) or []

    def history_values(self, asset_id: str, channel: str,
                       limit: int = config.PDM_HISTORY_LIMIT) -> list[tuple[float, float]]:
        """(ts_epoch, value) oldest-first, for seeding a channel's model at boot."""
        rows = self.db.execute_sql(
            "SELECT ts_epoch, value FROM readings "
            "WHERE asset_id = ? AND channel = ? AND value IS NOT NULL AND valid = 1 "
            "ORDER BY ts_epoch DESC LIMIT ?",
            (asset_id, channel, int(limit)),
        ) or []
        return [(float(r["ts_epoch"]), float(r["value"])) for r in reversed(rows)]

    def reading_image(self, reading_id: int) -> Optional[dict[str, Any]]:
        rows = self.db.execute_sql(
            "SELECT image, image_type FROM readings WHERE id = ?", (int(reading_id),)
        ) or []
        return rows[0] if rows else None

    def prune_images(self, keep: int = config.KEEP_IMAGES) -> None:
        try:
            self.db.execute_sql(
                "UPDATE readings SET image = NULL WHERE image IS NOT NULL AND id NOT IN "
                "(SELECT id FROM readings WHERE image IS NOT NULL "
                " ORDER BY ts_epoch DESC LIMIT ?)",
                (int(keep),),
            )
        except Exception as exc:  # pruning must never take the app down
            log.warning("could not prune stored images: %s", exc)

    def clear_readings(self, asset_id: str, channel: Optional[str] = None) -> int:
        if channel:
            sql_count = "SELECT COUNT(*) AS n FROM readings WHERE asset_id = ? AND channel = ?"
            sql_del = "DELETE FROM readings WHERE asset_id = ? AND channel = ?"
            args: tuple = (asset_id, channel)
        else:
            sql_count = "SELECT COUNT(*) AS n FROM readings WHERE asset_id = ?"
            sql_del = "DELETE FROM readings WHERE asset_id = ?"
            args = (asset_id,)
        rows = self.db.execute_sql(sql_count, args) or [{"n": 0}]
        self.db.execute_sql(sql_del, args)
        return int(rows[0].get("n", 0))

    # -- assets -------------------------------------------------------------
    def save_asset(self, asset: config.AssetConfig) -> None:
        ts, _ = utc_now()
        asset.updated = ts
        self.db.execute_sql(
            "INSERT INTO assets (asset_id, tag_id, payload, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET tag_id=excluded.tag_id, "
            "payload=excluded.payload, updated=excluded.updated",
            (asset.asset_id, int(asset.tag_id), asset.to_json(), ts),
        )

    def load_assets(self) -> dict[str, config.AssetConfig]:
        rows = self.db.execute_sql("SELECT asset_id, payload FROM assets") or []
        out: dict[str, config.AssetConfig] = {}
        for row in rows:
            try:
                out[str(row["asset_id"])] = config.AssetConfig.from_dict(json.loads(row["payload"]))
            except Exception as exc:
                log.error("skipping unreadable asset %s: %s", row.get("asset_id"), exc)
        return out

    def delete_asset(self, asset_id: str) -> None:
        self.db.execute_sql("DELETE FROM assets WHERE asset_id = ?", (asset_id,))

    # -- events -------------------------------------------------------------
    def add_event(self, asset_id: str, severity: str, code: str, message: str,
                  channel: str = "") -> None:
        ts, epoch = utc_now()
        self.db.store(
            "events",
            {
                "ts": ts,
                "ts_epoch": epoch,
                "asset_id": asset_id,
                "channel": channel,
                "severity": severity,
                "code": code,
                "message": message,
            },
            create_table=False,
        )

    def recent_events(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.db.execute_sql(
            "SELECT * FROM events ORDER BY ts_epoch DESC LIMIT ?", (int(limit),)
        ) or []
