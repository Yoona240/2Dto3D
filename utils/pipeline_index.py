"""
utils/pipeline_index.py — SQLite-backed persistent index for pipeline listings.

Note: uses `from __future__ import annotations` for Python 3.9 compat (local dev).
The production server runs Python 3.10+.

Replaces the in-memory pipeline_listing_cache with a durable DB that survives
restarts and supports incremental updates (only rescan dirs whose mtime changed).

Usage in app.py:
    from utils.pipeline_index import PipelineIndex
    _index = PipelineIndex(db_path)
    _index.reconcile(models_dir, images_dir, triplets_dir, ...)   # on startup
    _index.update_model(model_id, ...)                             # after write ops
    entries = _index.get_models_index()
    payload = _index.get_model_payload(model_id)
    images  = _index.get_images_index()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Bump this when schema changes incompatibly — triggers auto-rebuild.
_SCHEMA_VERSION = "4"


class PipelineIndex:
    """Thread-safe SQLite index for pipeline model and image listings."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Internal DB helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS db_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS model_index (
                    id                       TEXT PRIMARY KEY,
                    provider                 TEXT,
                    created_at               TEXT,
                    has_views                INTEGER,
                    rendered_views_count     INTEGER,
                    first_rendered_view_path TEXT,
                    has_instructions         INTEGER,
                    instructions_count       INTEGER,
                    has_edits                INTEGER,
                    edit_count               INTEGER,
                    target_ready_count       INTEGER,
                    ready_pair_count         INTEGER,
                    category_name            TEXT,
                    object_name              TEXT,
                    path                     TEXT,
                    edited_batches_json      TEXT,
                    dir_mtime_ns             INTEGER,
                    updated_at               REAL
                );

                CREATE TABLE IF NOT EXISTS model_payload (
                    id           TEXT PRIMARY KEY,
                    payload_json TEXT,
                    dir_mtime_ns INTEGER,
                    updated_at   REAL
                );

                CREATE TABLE IF NOT EXISTS image_index (
                    id              TEXT PRIMARY KEY,
                    path            TEXT,
                    schema_name     TEXT,
                    subject         TEXT,
                    display_subject TEXT,
                    prompt          TEXT,
                    instruction     TEXT,
                    model_path      TEXT,
                    created_at      TEXT,
                    dir_mtime_ns    INTEGER,
                    updated_at      REAL
                );
            """)
            conn.commit()

            # Check schema version; rebuild if stale.
            row = conn.execute(
                "SELECT value FROM db_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or row["value"] != _SCHEMA_VERSION:
                logger.info(
                    "[pipeline_index] Schema version mismatch (%s vs %s) — rebuilding.",
                    row["value"] if row else "none",
                    _SCHEMA_VERSION,
                )
                conn.executescript("""
                    DELETE FROM model_index;
                    DELETE FROM model_payload;
                    DELETE FROM image_index;
                """)
                conn.execute(
                    "INSERT OR REPLACE INTO db_meta(key,value) VALUES('schema_version',?)",
                    (_SCHEMA_VERSION,),
                )
                conn.commit()

    # ------------------------------------------------------------------
    # Reconcile (incremental sync)
    # ------------------------------------------------------------------

    def reconcile(
        self,
        *,
        models_dir: Path,
        images_dir: Path,
        triplets_dir: Path,
        build_model_index_entry: Callable[[Path], dict | None],
        build_model_payload: Callable[[str], dict | None],
        build_image_index_entry: Callable[[Path], dict | None],
        semantic_tmp_dir_name: str = "",
    ) -> dict:
        """
        Incrementally sync the DB with the filesystem.

        Only rescans model/image directories whose mtime_ns has changed.
        Returns a summary dict with counts for logging.
        """
        t0 = time.monotonic()
        summary = {"models_added": 0, "models_updated": 0, "models_removed": 0,
                   "images_added": 0, "images_updated": 0, "images_removed": 0}

        self._reconcile_models(
            models_dir, triplets_dir,
            build_model_index_entry, build_model_payload,
            summary,
        )
        self._reconcile_images(images_dir, build_image_index_entry, summary)

        elapsed = time.monotonic() - t0
        logger.info(
            "[pipeline_index] reconcile done in %.2fs — %s", elapsed, summary
        )
        return summary

    def _reconcile_models(
        self,
        models_dir: Path,
        triplets_dir: Path,
        build_index_entry: Callable,
        build_payload: Callable,
        summary: dict,
    ):
        if not models_dir.exists():
            return

        with self._lock:
            conn = self._get_conn()
            existing = {
                row["id"]: row["dir_mtime_ns"]
                for row in conn.execute(
                    "SELECT id, dir_mtime_ns FROM model_index"
                ).fetchall()
            }

        seen_ids = set()
        for model_dir in models_dir.iterdir():
            if not model_dir.is_dir():
                continue
            # Source-model listing must exclude target/pair dirs like
            # <source_model_id>_edit_<edit_id>.
            if "_edit_" in model_dir.name:
                continue
            # Skip dirs that contain no .glb (not a real model dir)
            if not any(model_dir.glob("*.glb")):
                continue

            model_id = model_dir.name
            seen_ids.add(model_id)
            try:
                mtime_ns = model_dir.stat().st_mtime_ns
            except OSError:
                continue

            if model_id in existing and existing[model_id] == mtime_ns:
                continue  # unchanged

            # Build and upsert
            index_entry = build_index_entry(model_dir)
            if index_entry is None:
                continue
            payload = build_payload(model_id)

            action = "updated" if model_id in existing else "added"
            self._upsert_model(model_id, index_entry, payload, mtime_ns)
            summary[f"models_{action}"] += 1

        # Remove stale entries
        stale = set(existing.keys()) - seen_ids
        if stale:
            self._remove_models(stale)
            summary["models_removed"] += len(stale)

    def _reconcile_images(
        self,
        images_dir: Path,
        build_entry: Callable,
        summary: dict,
    ):
        if not images_dir.exists():
            return

        with self._lock:
            conn = self._get_conn()
            existing = {
                row["id"]: row["dir_mtime_ns"]
                for row in conn.execute(
                    "SELECT id, dir_mtime_ns FROM image_index"
                ).fetchall()
            }

        seen_ids = set()
        for image_dir in images_dir.iterdir():
            if not image_dir.is_dir():
                continue
            if not (image_dir / "image.png").exists():
                continue

            image_id = image_dir.name
            seen_ids.add(image_id)
            try:
                mtime_ns = image_dir.stat().st_mtime_ns
            except OSError:
                continue

            if image_id in existing and existing[image_id] == mtime_ns:
                continue

            entry = build_entry(image_dir)
            if entry is None:
                continue

            action = "updated" if image_id in existing else "added"
            self._upsert_image(image_id, entry, mtime_ns)
            summary[f"images_{action}"] += 1

        stale = set(existing.keys()) - seen_ids
        if stale:
            self._remove_images(stale)
            summary["images_removed"] += len(stale)

    # ------------------------------------------------------------------
    # Single-model update (called after write ops)
    # ------------------------------------------------------------------

    def update_model(
        self,
        model_id: str,
        model_dir: Path,
        build_index_entry: Callable[[Path], dict | None],
        build_payload: Callable[[str], dict | None],
    ):
        """Update a single model's index + payload rows. Called after render/edit/gen3d."""
        if not model_dir.exists():
            self._remove_models({model_id})
            return
        index_entry = build_index_entry(model_dir)
        if index_entry is None:
            self._remove_models({model_id})
            return
        payload = build_payload(model_id)
        try:
            mtime_ns = model_dir.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        self._upsert_model(model_id, index_entry, payload, mtime_ns)

    def update_image(
        self,
        image_id: str,
        image_dir: Path,
        build_entry: Callable[[Path], dict | None],
    ):
        """Update a single image's index row."""
        if not image_dir.exists():
            self._remove_images({image_id})
            return
        entry = build_entry(image_dir)
        if entry is None:
            self._remove_images({image_id})
            return
        try:
            mtime_ns = image_dir.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        self._upsert_image(image_id, entry, mtime_ns)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_models_index(self) -> list[dict]:
        """Return all lightweight model index entries, newest first."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM model_index ORDER BY created_at DESC NULLS LAST"
            ).fetchall()
        return [self._row_to_index_entry(r) for r in rows]

    def get_model_payload(self, model_id: str) -> dict | None:
        """Return full payload dict for a single model, or None if not found."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT payload_json FROM model_payload WHERE id=?", (model_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def get_models_page(
        self,
        page: int,
        per_page: int,
        priority_ids: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """
        Return (items, total) for a paginated models listing.
        priority_ids: if given, these model IDs are sorted to the front of page 1.
        """
        with self._lock:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM model_payload").fetchone()[0]

            if priority_ids:
                # Build a CASE WHEN ordering so priority IDs come first.
                placeholders = ",".join("?" * len(priority_ids))
                order_sql = (
                    f"CASE WHEN mp.id IN ({placeholders}) THEN 0 ELSE 1 END, "
                    "mi.created_at DESC NULLS LAST, mp.id"
                )
                sql = (
                    "SELECT mp.payload_json FROM model_payload mp "
                    "LEFT JOIN model_index mi ON mp.id = mi.id "
                    f"ORDER BY {order_sql} "
                    "LIMIT ? OFFSET ?"
                )
                offset = max(page - 1, 0) * per_page
                rows = conn.execute(
                    sql, (*priority_ids, per_page, offset)
                ).fetchall()
            else:
                sql = (
                    "SELECT mp.payload_json FROM model_payload mp "
                    "LEFT JOIN model_index mi ON mp.id = mi.id "
                    "ORDER BY mi.created_at DESC NULLS LAST, mp.id "
                    "LIMIT ? OFFSET ?"
                )
                offset = max(page - 1, 0) * per_page
                rows = conn.execute(sql, (per_page, offset)).fetchall()

        items = []
        for row in rows:
            try:
                items.append(json.loads(row["payload_json"]))
            except (json.JSONDecodeError, TypeError):
                pass
        return items, total

    def get_images_index(self) -> list[dict]:
        """Return all image index entries, newest first."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM image_index ORDER BY created_at DESC NULLS LAST"
            ).fetchall()
        return [self._row_to_image_entry(r) for r in rows]

    def model_count(self) -> int:
        with self._lock:
            return self._get_conn().execute(
                "SELECT COUNT(*) FROM model_index"
            ).fetchone()[0]

    def image_count(self) -> int:
        with self._lock:
            return self._get_conn().execute(
                "SELECT COUNT(*) FROM image_index"
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # Internal write helpers
    # ------------------------------------------------------------------

    def _upsert_model(
        self,
        model_id: str,
        entry: dict,
        payload: dict | None,
        mtime_ns: int,
    ):
        now = time.time()
        edited_batches = entry.get("edited_batches", [])
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO model_index
                  (id, provider, created_at, has_views, rendered_views_count,
                   first_rendered_view_path, has_instructions, instructions_count,
                   has_edits, edit_count, target_ready_count, ready_pair_count,
                   category_name, object_name, path, edited_batches_json,
                   dir_mtime_ns, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    model_id,
                    entry.get("provider"),
                    entry.get("created_at"),
                    1 if entry.get("has_views") else 0,
                    entry.get("rendered_views_count", 0),
                    entry.get("first_rendered_view_path"),
                    1 if entry.get("has_instructions") else 0,
                    entry.get("instructions_count", 0),
                    1 if entry.get("has_edits") else 0,
                    entry.get("edit_count", 0),
                    entry.get("target_ready_count", 0),
                    entry.get("ready_pair_count", 0),
                    entry.get("category_name"),
                    entry.get("object_name"),
                    entry.get("path"),
                    json.dumps(edited_batches, ensure_ascii=False),
                    mtime_ns,
                    now,
                ),
            )
            if payload is not None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO model_payload
                      (id, payload_json, dir_mtime_ns, updated_at)
                    VALUES (?,?,?,?)
                    """,
                    (model_id, json.dumps(payload, ensure_ascii=False), mtime_ns, now),
                )
            conn.commit()

    def _upsert_image(self, image_id: str, entry: dict, mtime_ns: int):
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO image_index
                  (id, path, schema_name, subject, display_subject, prompt,
                   instruction, model_path, created_at, dir_mtime_ns, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    image_id,
                    entry.get("path"),
                    entry.get("schema"),
                    entry.get("subject"),
                    entry.get("display_subject"),
                    entry.get("prompt"),
                    entry.get("instruction"),
                    entry.get("model_path"),
                    entry.get("created_at"),
                    mtime_ns,
                    now,
                ),
            )
            conn.commit()

    def _remove_models(self, ids: set[str]):
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM model_index WHERE id IN ({placeholders})", tuple(ids)
            )
            conn.execute(
                f"DELETE FROM model_payload WHERE id IN ({placeholders})", tuple(ids)
            )
            conn.commit()

    def _remove_images(self, ids: set[str]):
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM image_index WHERE id IN ({placeholders})", tuple(ids)
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Row -> dict converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_index_entry(row: sqlite3.Row) -> dict:
        try:
            edited_batches = json.loads(row["edited_batches_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            edited_batches = []
        return {
            "id": row["id"],
            "provider": row["provider"],
            "created_at": row["created_at"],
            "has_views": bool(row["has_views"]),
            "rendered_views_count": row["rendered_views_count"],
            "first_rendered_view_path": row["first_rendered_view_path"],
            "has_instructions": bool(row["has_instructions"]),
            "instructions_count": row["instructions_count"],
            "has_edits": bool(row["has_edits"]),
            "edit_count": row["edit_count"],
            "target_ready_count": row["target_ready_count"],
            "ready_pair_count": row["ready_pair_count"],
            "category_name": row["category_name"],
            "object_name": row["object_name"],
            "path": row["path"],
            "edited_batches": edited_batches,
            "edits_without_target": (
                bool(row["has_edits"])
                and row["target_ready_count"] < row["edit_count"]
            ),
        }

    @staticmethod
    def _row_to_image_entry(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "path": row["path"],
            "schema": row["schema_name"],
            "subject": row["subject"],
            "display_subject": row["display_subject"],
            "prompt": row["prompt"],
            "instruction": row["instruction"],
            "model_path": row["model_path"],
            "created_at": row["created_at"],
        }
