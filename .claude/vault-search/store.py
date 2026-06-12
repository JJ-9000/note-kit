"""SQLite + sqlite-vec + FTS5 storage layer for the vault-search daemon.

Phase 0 subset of the schema in §5.5 of Vault-Retrieval-System-Description-Detailed.md.
Wikilinks / workflow_clusters / quality_signals tables are deferred to later phases.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (_dt.date, _dt.datetime, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def _safe_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)

import sqlite_vec
from sqlite_vec import serialize_float32

SCHEMA_VERSION = "3"
EMBEDDING_DIM = 384
EMBEDDING_MODEL_KEY = "all-MiniLM-L6-v2"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    para_type TEXT NOT NULL,
    para_owner TEXT,
    parent_index TEXT,
    frontmatter_json TEXT,
    title TEXT,
    last_modified REAL,
    last_indexed REAL,
    content_hash TEXT,
    chunk_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_para_type ON files(para_type);
CREATE INDEX IF NOT EXISTS idx_files_para_owner ON files(para_owner);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    contextual_prefix TEXT,
    section_heading TEXT,
    token_count INTEGER,
    UNIQUE(file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[{EMBEDDING_DIM}]
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES('delete', old.chunk_id, old.text);
    INSERT INTO chunk_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

CREATE TABLE IF NOT EXISTS wikilinks (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    src_chunk_id INTEGER REFERENCES chunks(chunk_id) ON DELETE SET NULL,
    dst_path TEXT NOT NULL,
    dst_file_id INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
    alias TEXT,
    is_resolved INTEGER NOT NULL DEFAULT 0,
    in_frontmatter INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wikilinks_src ON wikilinks(src_file_id);
CREATE INDEX IF NOT EXISTS idx_wikilinks_dst_path ON wikilinks(dst_path);
CREATE INDEX IF NOT EXISTS idx_wikilinks_dst_file ON wikilinks(dst_file_id);
CREATE INDEX IF NOT EXISTS idx_wikilinks_unresolved ON wikilinks(is_resolved) WHERE is_resolved = 0;

CREATE TABLE IF NOT EXISTS workflow_clusters (
    cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    centroid_refs TEXT NOT NULL,
    co_citing_projects TEXT NOT NULL,
    member_session_paths TEXT,
    score REAL,
    computed_at REAL
);
"""


@dataclass
class FileRow:
    file_id: int
    path: str
    para_type: str
    para_owner: str | None
    parent_index: str | None
    title: str | None
    last_modified: float
    last_indexed: float
    content_hash: str
    chunk_count: int


@dataclass
class ChunkRow:
    chunk_id: int
    file_id: int
    chunk_index: int
    text: str
    section_heading: str | None
    token_count: int


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """Thread-safe wrapper around the sqlite-vec database."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._open()
        self._init_schema()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone() if self._table_exists("meta") else None

            if existing and existing["value"] != SCHEMA_VERSION:
                self._drop_all()

            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("embedding_model", EMBEDDING_MODEL_KEY),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("embedding_dim", str(EMBEDDING_DIM)),
            )

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _drop_all(self) -> None:
        self._conn.executescript("""
            DROP TABLE IF EXISTS workflow_clusters;
            DROP TABLE IF EXISTS wikilinks;
            DROP TABLE IF EXISTS chunk_fts;
            DROP TABLE IF EXISTS chunk_embeddings;
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS meta;
        """)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- file lookup --------------------------------------------------------

    def get_file_state(self, path: str) -> tuple[float, str] | None:
        """Return (last_indexed, content_hash) for path, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_indexed, content_hash FROM files WHERE path = ?",
                (path,),
            ).fetchone()
        if row is None:
            return None
        return row["last_indexed"], row["content_hash"]

    def all_indexed_paths(self) -> set[str]:
        with self._lock:
            return {
                row["path"]
                for row in self._conn.execute("SELECT path FROM files")
            }

    # ---- upsert pipeline ----------------------------------------------------

    def upsert_file_with_chunks(
        self,
        path: str,
        para_type: str,
        para_owner: str | None,
        parent_index: str | None,
        frontmatter: dict[str, Any],
        title: str | None,
        last_modified: float,
        last_indexed: float,
        content_hash_value: str,
        chunks_with_embeddings: list[tuple[int, str, str | None, int, list[float]]],
        body_wikilinks: list[tuple[int, str, str | None]] | None = None,
        frontmatter_wikilinks: list[tuple[str, str | None]] | None = None,
    ) -> int:
        """Atomic upsert. Replaces all chunks for the file. Returns file_id.

        chunks_with_embeddings: list of (chunk_index, text, section_heading,
        token_count, embedding_vector).
        body_wikilinks: list of (chunk_index, dst_path, alias). chunk_index
        maps to the position of the chunk in chunks_with_embeddings; we resolve
        it to chunk_id after insert.
        frontmatter_wikilinks: list of (dst_path, alias) extracted from
        frontmatter values like `project: "[[X]]"` — no chunk_id.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO files (path, para_type, para_owner, parent_index,
                                       frontmatter_json, title, last_modified,
                                       last_indexed, content_hash, chunk_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        para_type=excluded.para_type,
                        para_owner=excluded.para_owner,
                        parent_index=excluded.parent_index,
                        frontmatter_json=excluded.frontmatter_json,
                        title=excluded.title,
                        last_modified=excluded.last_modified,
                        last_indexed=excluded.last_indexed,
                        content_hash=excluded.content_hash,
                        chunk_count=excluded.chunk_count
                    """,
                    (
                        path,
                        para_type,
                        para_owner,
                        parent_index,
                        _safe_dumps(frontmatter),
                        title,
                        last_modified,
                        last_indexed,
                        content_hash_value,
                        len(chunks_with_embeddings),
                    ),
                )
                file_id = self._conn.execute(
                    "SELECT file_id FROM files WHERE path = ?", (path,)
                ).fetchone()["file_id"]

                # Drop old chunks (cascades to chunk_embeddings via app code below
                # since vec0 doesn't honour FK cascade).
                old_chunk_ids = [
                    row["chunk_id"]
                    for row in self._conn.execute(
                        "SELECT chunk_id FROM chunks WHERE file_id = ?", (file_id,)
                    )
                ]
                if old_chunk_ids:
                    self._conn.executemany(
                        "DELETE FROM chunk_embeddings WHERE chunk_id = ?",
                        [(cid,) for cid in old_chunk_ids],
                    )
                    self._conn.execute(
                        "DELETE FROM chunks WHERE file_id = ?", (file_id,)
                    )

                # Drop old wikilinks for this src.
                self._conn.execute(
                    "DELETE FROM wikilinks WHERE src_file_id = ?", (file_id,)
                )

                chunk_ids_by_index: dict[int, int] = {}
                for chunk_index, text, section, tok, emb in chunks_with_embeddings:
                    cur = self._conn.execute(
                        """
                        INSERT INTO chunks (file_id, chunk_index, text,
                                            section_heading, token_count)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (file_id, chunk_index, text, section, tok),
                    )
                    chunk_id = cur.lastrowid
                    chunk_ids_by_index[chunk_index] = chunk_id
                    self._conn.execute(
                        "INSERT INTO chunk_embeddings (chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, serialize_float32(emb)),
                    )

                if body_wikilinks:
                    self._conn.executemany(
                        """
                        INSERT INTO wikilinks
                            (src_file_id, src_chunk_id, dst_path, alias,
                             is_resolved, in_frontmatter)
                        VALUES (?, ?, ?, ?, 0, 0)
                        """,
                        [
                            (file_id, chunk_ids_by_index.get(ci), dst, alias)
                            for (ci, dst, alias) in body_wikilinks
                        ],
                    )
                if frontmatter_wikilinks:
                    self._conn.executemany(
                        """
                        INSERT INTO wikilinks
                            (src_file_id, src_chunk_id, dst_path, alias,
                             is_resolved, in_frontmatter)
                        VALUES (?, NULL, ?, ?, 0, 1)
                        """,
                        [(file_id, dst, alias) for (dst, alias) in frontmatter_wikilinks],
                    )

                self._conn.execute("COMMIT")
                return file_id
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_file(self, path: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT file_id FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return
            file_id = row["file_id"]
            chunk_ids = [
                r["chunk_id"]
                for r in self._conn.execute(
                    "SELECT chunk_id FROM chunks WHERE file_id = ?", (file_id,)
                )
            ]
            self._conn.execute("BEGIN")
            try:
                if chunk_ids:
                    self._conn.executemany(
                        "DELETE FROM chunk_embeddings WHERE chunk_id = ?",
                        [(cid,) for cid in chunk_ids],
                    )
                # Mark inbound links unresolved (FK SET NULL nulls dst_file_id;
                # we also flip is_resolved so the resolver picks them up later).
                self._conn.execute(
                    "UPDATE wikilinks SET is_resolved = 0 WHERE dst_file_id = ?",
                    (file_id,),
                )
                self._conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---- wikilink graph -----------------------------------------------------

    def basename_map(self) -> dict[str, list[tuple[int, str]]]:
        """Build {basename_lower: [(file_id, path), ...]} for resolution.

        Basename is the filename stem without the .md extension, lowercased.
        """
        out: dict[str, list[tuple[int, str]]] = {}
        with self._lock:
            rows = self._conn.execute("SELECT file_id, path FROM files").fetchall()
        for row in rows:
            base = Path(row["path"]).stem.lower()
            out.setdefault(base, []).append((row["file_id"], row["path"]))
        return out

    def resolve_links(self, only_unresolved: bool = True) -> int:
        """Resolve dst_path → dst_file_id for unresolved (or all) wikilinks.

        Returns count of links newly resolved.
        """
        bn_map = self.basename_map()

        # Path-suffix lookup map: {full_path: file_id}
        with self._lock:
            path_to_id = {
                row["path"]: row["file_id"]
                for row in self._conn.execute("SELECT file_id, path FROM files")
            }
        resolved = 0
        with self._lock:
            if only_unresolved:
                rows = self._conn.execute(
                    "SELECT link_id, src_file_id, dst_path FROM wikilinks "
                    "WHERE is_resolved = 0"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT link_id, src_file_id, dst_path FROM wikilinks"
                ).fetchall()

            updates: list[tuple[int, int]] = []
            for row in rows:
                target_id = _resolve_one(
                    row["dst_path"], row["src_file_id"], bn_map, path_to_id
                )
                if target_id is not None:
                    updates.append((target_id, row["link_id"]))

            if updates:
                self._conn.executemany(
                    "UPDATE wikilinks SET dst_file_id = ?, is_resolved = 1 "
                    "WHERE link_id = ?",
                    updates,
                )
                resolved = len(updates)
        return resolved

    def get_outbound_refs(
        self, src_file_id: int, target_para_types: tuple[str, ...] = ("reference", "snippet", "index"),
    ) -> list[dict[str, Any]]:
        """Distinct citation targets matching given para_types, with counts."""
        placeholders = ",".join("?" for _ in target_para_types)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT f.file_id, f.path, f.title, f.para_type, f.para_owner,
                       COUNT(*) AS citation_count,
                       GROUP_CONCAT(DISTINCT w.src_chunk_id) AS chunk_ids
                FROM wikilinks w
                JOIN files f ON w.dst_file_id = f.file_id
                WHERE w.src_file_id = ?
                  AND w.is_resolved = 1
                  AND f.para_type IN ({placeholders})
                GROUP BY f.file_id
                ORDER BY citation_count DESC
                """,
                (src_file_id, *target_para_types),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_inbound_projects(self, dst_file_id: int) -> list[dict[str, Any]]:
        """All projects/sessions citing this file. Rolled up to project level."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.para_owner AS project_name,
                       MIN(f.path) AS project_path,
                       COUNT(DISTINCT CASE WHEN f.para_type = 'session'
                                            THEN f.file_id END) AS session_count,
                       MAX(f.last_modified) AS last_cited_at
                FROM wikilinks w
                JOIN files f ON w.src_file_id = f.file_id
                WHERE w.dst_file_id = ?
                  AND f.para_type IN ('project', 'session')
                  AND f.para_owner IS NOT NULL
                GROUP BY f.para_owner
                ORDER BY last_cited_at DESC
                """,
                (dst_file_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_linked_file_ids(self, file_id: int) -> set[int]:
        """File IDs that link to OR are linked from `file_id`. For boost_linked_to."""
        with self._lock:
            outbound = self._conn.execute(
                "SELECT DISTINCT dst_file_id FROM wikilinks "
                "WHERE src_file_id = ? AND is_resolved = 1",
                (file_id,),
            ).fetchall()
            inbound = self._conn.execute(
                "SELECT DISTINCT src_file_id FROM wikilinks "
                "WHERE dst_file_id = ?",
                (file_id,),
            ).fetchall()
        result: set[int] = set()
        for r in outbound:
            if r["dst_file_id"] is not None:
                result.add(r["dst_file_id"])
        for r in inbound:
            result.add(r["src_file_id"])
        return result

    def get_referenced_file_ids_by_owner(self, owner: str) -> set[int]:
        """All reference/snippet file_ids cited by any file with para_owner=owner.

        Used by para_position boost in vault_search to detect 'linked references'.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT w.dst_file_id
                FROM wikilinks w
                JOIN files src ON w.src_file_id = src.file_id
                JOIN files dst ON w.dst_file_id = dst.file_id
                WHERE src.para_owner = ?
                  AND w.is_resolved = 1
                  AND dst.para_type IN ('reference', 'snippet', 'index')
                """,
                (owner,),
            ).fetchall()
        return {r["dst_file_id"] for r in rows if r["dst_file_id"] is not None}

    def file_id_for_path(self, path: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT file_id FROM files WHERE path = ?", (path,)
            ).fetchone()
        return row["file_id"] if row else None

    def wikilink_count(self) -> tuple[int, int]:
        """Returns (total, resolved). Excludes [[CVF-NNNN]] citations that point at
        Sources tree files which are intentionally not indexed (config.yaml
        excludes 01-References/99-Sources/). Those citations cannot resolve by
        design; counting them as unresolved pollutes the metric. See
        the wikilink-resolution refinement spec."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM wikilinks WHERE dst_path NOT GLOB 'CVF-[0-9]*'"
            ).fetchone()[0]
            resolved = self._conn.execute(
                "SELECT COUNT(*) FROM wikilinks WHERE is_resolved = 1 "
                "AND dst_path NOT GLOB 'CVF-[0-9]*'"
            ).fetchone()[0]
        return total, resolved

    def cvf_link_count(self) -> int:
        """Count of [[CVF-NNNN]] citations. Excluded from wikilink_count() so the
        resolution metric reflects authoring-quality wikilinks, not the
        intentionally-unindexed Sources tree."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM wikilinks WHERE dst_path GLOB 'CVF-[0-9]*'"
            ).fetchone()[0]

    # ---- workflow clusters --------------------------------------------------

    def replace_workflow_clusters(
        self,
        clusters: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM workflow_clusters")
                for c in clusters:
                    self._conn.execute(
                        """
                        INSERT INTO workflow_clusters
                            (label, centroid_refs, co_citing_projects,
                             member_session_paths, score, computed_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            c.get("label"),
                            _safe_dumps(c["centroid_refs"]),
                            _safe_dumps(c["co_citing_projects"]),
                            _safe_dumps(c.get("member_sessions", [])),
                            c.get("score", 0.0),
                            c.get("computed_at", 0.0),
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_workflow_clusters(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT cluster_id, label, centroid_refs, co_citing_projects, "
                "member_session_paths, score, computed_at FROM workflow_clusters "
                "ORDER BY score DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "cluster_id": r["cluster_id"],
                "label": r["label"],
                "centroid_refs": json.loads(r["centroid_refs"]),
                "co_citing_projects": json.loads(r["co_citing_projects"]),
                "member_sessions": (
                    json.loads(r["member_session_paths"])
                    if r["member_session_paths"] else []
                ),
                "score": r["score"],
                "computed_at": r["computed_at"],
            })
        return out

    # ---- search -------------------------------------------------------------

    def vec_search(self, embedding: list[float], k: int) -> list[tuple[int, float]]:
        """Returns [(chunk_id, distance)] ordered by ascending cosine distance."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT chunk_id, distance
                FROM chunk_embeddings
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (serialize_float32(embedding), k),
            ).fetchall()
        return [(row["chunk_id"], row["distance"]) for row in rows]

    def fts_search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Returns [(chunk_id, bm25_score)]. Lower bm25 = better."""
        if not query.strip():
            return []
        sanitized = _fts_sanitize(query)
        if not sanitized:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT rowid AS chunk_id, bm25(chunk_fts) AS score
                FROM chunk_fts
                WHERE chunk_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (sanitized, k),
            ).fetchall()
        return [(row["chunk_id"], row["score"]) for row in rows]

    def get_chunks_with_files(
        self, chunk_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]:
        ids = list(chunk_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.chunk_id, c.chunk_index, c.text, c.section_heading,
                       c.token_count,
                       f.file_id, f.path, f.para_type, f.para_owner,
                       f.parent_index, f.title, f.last_modified
                FROM chunks c
                JOIN files f ON c.file_id = f.file_id
                WHERE c.chunk_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {row["chunk_id"]: dict(row) for row in rows}

    # ---- stats --------------------------------------------------------------

    def file_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def chunk_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def last_update(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(last_indexed) AS m FROM files"
            ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def db_size_bytes(self) -> int:
        try:
            return Path(self.db_path).stat().st_size
        except FileNotFoundError:
            return 0


def _resolve_one(
    dst_path: str,
    src_file_id: int,
    bn_map: dict[str, list[tuple[int, str]]],
    path_to_id: dict[str, int],
) -> int | None:
    """Apply Obsidian-style wikilink resolution rules. Returns target file_id."""
    cleaned = dst_path.split("#", 1)[0].strip()
    if not cleaned:
        return None

    if cleaned in path_to_id:
        return path_to_id[cleaned]
    md_form = cleaned + ".md"
    if md_form in path_to_id:
        return path_to_id[md_form]

    cleaned_norm = cleaned.replace("\\", "/").lstrip("/")
    if "/" in cleaned_norm:
        for full_path, fid in path_to_id.items():
            if full_path == cleaned_norm + ".md" or full_path == cleaned_norm:
                return fid
            if full_path.endswith("/" + cleaned_norm + ".md"):
                return fid
        # Fallback to basename of the last segment
        last = cleaned_norm.rsplit("/", 1)[-1]
    else:
        last = cleaned_norm

    candidates = bn_map.get(last.lower(), [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    # Disambiguate: prefer same-folder as src.
    src_path = next(
        (p for p, fid in path_to_id.items() if fid == src_file_id), None
    )
    if src_path:
        src_folder = src_path.rsplit("/", 1)[0] if "/" in src_path else ""
        same_folder = [
            (fid, p) for fid, p in candidates
            if (p.rsplit("/", 1)[0] if "/" in p else "") == src_folder
        ]
        if same_folder:
            return sorted(same_folder, key=lambda x: x[1])[0][0]
    return sorted(candidates, key=lambda x: x[1])[0][0]


def _fts_sanitize(query: str) -> str:
    """Quote each whitespace-separated token so FTS5 treats them as literal terms.

    Avoids interpreting user query as the FTS5 mini-DSL (which would error on
    things like "API dict has-key" because of the hyphen).
    """
    import re

    tokens = [t for t in re.findall(r"[\w']+", query) if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
