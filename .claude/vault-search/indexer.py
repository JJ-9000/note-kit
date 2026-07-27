"""Vault file chunking, embedding, and watching.

Chunks each file's body, embeds every chunk, and watches for changes. Wikilinks
are extracted per chunk (and recursively from frontmatter) and persisted to the
store's wikilinks table through upsert_file_with_chunks; the link graph is
resolved to dst_file_id after each walk.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import frontmatter
from watchdog.events import FileSystemEvent, PatternMatchingEventHandler
from watchdog.observers import Observer

from store import Store, content_hash
from vocabulary import (
    INBOX_FOLDER,
    PROJECT_ROOT,
    AREA_ROOT,
    REFERENCE_ROOT,
    SNIPPETS_ROOT,
    ARCHIVE_ROOT,
    PARA_ROOTS,
    CANONICAL_TYPES,
)
from wikilink_helpers import WIKILINK_RE, normalize_link_target

log = logging.getLogger("vault.indexer")

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_PATTERN = re.compile(r"^(```|~~~)")

# The canonical note-type set and the inbox / PARA-root folder names come from
# vocabulary.py — one install-time source generated from CONFIG § Folders and
# § Types by generate_vocabulary.py. Every folder-name and type-set literal the
# indexer used to hold its own copy of now lives there (INBOX_FOLDER,
# PROJECT_ROOT, AREA_ROOT, REFERENCE_ROOT, SNIPPETS_ROOT, ARCHIVE_ROOT,
# PARA_ROOTS, CANONICAL_TYPES are imported above).
VALID_FRONTMATTER_TYPES = CANONICAL_TYPES


def _root_name(segment: str) -> str:
    """A top-level folder's semantic name: the segment with any legacy numeric
    prefix stripped ("01-Projects" and "Projects" both -> "Projects")."""
    return re.sub(r"^\d+-", "", segment)


def _iter_wikilinks(text: str):
    """Yield (dst_basename, alias|None) for each non-embed `[[...]]` in text.

    Uses the vendored wikilink_helpers (WIKILINK_RE + normalize_link_target) so
    the daemon parses links identically to the rest of the kit. Embeds
    (`![[...]]`) are skipped; the empty placeholder `[[]]` and fragment-only
    links (`[[#anchor]]`) normalize to an empty basename and are dropped.
    """
    for m in WIKILINK_RE.finditer(text):
        if m.group(1):  # '!' embed marker
            continue
        interior = m.group(2)
        dst = normalize_link_target(interior)
        if not dst:
            continue
        alias = interior.split("|", 1)[1].strip() if "|" in interior else None
        yield dst, (alias or None)


# ---- chunking ---------------------------------------------------------------


@dataclass
class _Block:
    """One markdown atom: heading, paragraph, code block, list, etc."""
    text: str
    is_code: bool = False
    heading_level: int | None = None
    heading_text: str | None = None


@dataclass
class Chunk:
    chunk_index: int
    text: str
    section_heading: str | None
    token_count: int
    wikilinks: list[tuple[str, str | None]] = field(default_factory=list)
    """List of (dst_path, alias) extracted from this chunk's text."""


def _approx_token_count(text: str) -> int:
    """Word-count * 1.3 rough estimate."""
    return int(len(re.findall(r"\S+", text)) * 1.3)


def _split_into_blocks(body: str) -> list[_Block]:
    """Split markdown body into atoms. Code blocks stay whole."""
    lines = body.splitlines(keepends=True)
    blocks: list[_Block] = []
    i = 0
    n = len(lines)
    current_para: list[str] = []

    def flush_para() -> None:
        if current_para:
            text = "".join(current_para).strip("\n")
            if text:
                blocks.append(_Block(text=text + "\n"))
            current_para.clear()

    while i < n:
        line = lines[i]
        stripped = line.rstrip("\n")

        if FENCE_PATTERN.match(stripped):
            flush_para()
            fence_marker = stripped[:3]
            code_lines = [line]
            i += 1
            while i < n:
                code_lines.append(lines[i])
                if lines[i].startswith(fence_marker):
                    i += 1
                    break
                i += 1
            blocks.append(_Block(text="".join(code_lines), is_code=True))
            continue

        m = HEADING_PATTERN.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            heading_text = m.group(2).strip()
            blocks.append(_Block(
                text=line,
                heading_level=level,
                heading_text=heading_text,
            ))
            i += 1
            continue

        if line.strip() == "":
            flush_para()
            i += 1
            continue

        current_para.append(line)
        i += 1

    flush_para()
    return blocks


def _build_chunks(
    blocks: list[_Block],
    chunk_size: int,
    overlap_tokens: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur_atoms: list[_Block] = []
    cur_tokens = 0
    cur_section: str | None = None

    def emit() -> None:
        nonlocal cur_atoms, cur_tokens
        if not cur_atoms:
            return
        text = "".join(b.text for b in cur_atoms).rstrip("\n")
        tok = _approx_token_count(text)
        if not text.strip():
            cur_atoms = []
            cur_tokens = 0
            return
        # Body wikilinks: store the canonical basename (with section anchor
        # and path prefix stripped) so the DB has uniform dst_path values
        # regardless of which syntactic form the author used. Embeds are
        # skipped by _iter_wikilinks.
        wls: list[tuple[str, str | None]] = list(_iter_wikilinks(text))
        chunks.append(Chunk(
            chunk_index=len(chunks),
            text=text,
            section_heading=cur_section,
            token_count=tok,
            wikilinks=wls,
        ))
        cur_atoms = []
        cur_tokens = 0

    for block in blocks:
        block_tokens = _approx_token_count(block.text)

        if block.heading_level is not None:
            # Heading boundary: emit pending chunk, start fresh.
            emit()
            if block.heading_level <= 2:
                cur_section = block.heading_text
            else:
                # H3+ doesn't reset section anchor; just included in chunk.
                pass
            cur_atoms.append(block)
            cur_tokens = block_tokens
            continue

        if block.is_code:
            # Atomic. If oversized, emit on its own.
            if cur_tokens + block_tokens > chunk_size and cur_atoms:
                emit()
            cur_atoms.append(block)
            cur_tokens += block_tokens
            if cur_tokens > chunk_size:
                emit()
            continue

        if cur_tokens + block_tokens > chunk_size and cur_atoms:
            emit()

        cur_atoms.append(block)
        cur_tokens += block_tokens

        if cur_tokens >= chunk_size:
            emit()

    emit()

    if overlap_tokens > 0:
        chunks = _apply_overlap(chunks, overlap_tokens)

    return chunks


def _apply_overlap(chunks: list[Chunk], overlap_tokens: int) -> list[Chunk]:
    if len(chunks) <= 1:
        return chunks
    out: list[Chunk] = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:]):
        prev_words = re.findall(r"\S+|\s+", prev.text)
        # Take roughly overlap_tokens words from the tail.
        target_words = int(overlap_tokens / 1.3)
        token_count = 0
        slice_idx = len(prev_words)
        for i in range(len(prev_words) - 1, -1, -1):
            if prev_words[i].strip():
                token_count += 1
            if token_count >= target_words:
                slice_idx = i
                break
        prefix = "".join(prev_words[slice_idx:]).strip()
        if prefix:
            new_text = prefix + "\n\n" + cur.text
            out.append(Chunk(
                chunk_index=cur.chunk_index,
                text=new_text,
                section_heading=cur.section_heading,
                token_count=_approx_token_count(new_text),
                # Carry the chunk's own wikilinks through the overlap rebuild.
                # The prepended prefix is only embedding context; its links
                # already belong to the previous chunk, so re-attributing them
                # here would double-count — keep cur's links, and only cur's.
                wikilinks=cur.wikilinks,
            ))
        else:
            out.append(cur)
    return out


def chunk_markdown(content: str, chunk_size: int = 500, overlap: int = 80) -> tuple[dict, list[Chunk]]:
    """Parse frontmatter + chunk body. Returns (frontmatter_dict, chunks)."""
    try:
        post = frontmatter.loads(content)
        meta = dict(post.metadata) if post.metadata else {}
        body = post.content
    except Exception as e:
        log.warning(f"frontmatter parse failed: {e}; treating whole file as body")
        meta = {}
        body = content

    blocks = _split_into_blocks(body)
    chunks = _build_chunks(blocks, chunk_size, overlap)
    return meta, chunks


def extract_frontmatter_wikilinks(meta: dict) -> list[tuple[str, str | None]]:
    """Pull `[[X]]` patterns out of any frontmatter value (recursively)."""
    out: list[tuple[str, str | None]] = []

    def walk(v) -> None:
        if isinstance(v, str):
            out.extend(_iter_wikilinks(v))
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)

    walk(meta)
    return out


# ---- PARA classification (Phase 0 minimal) ----------------------------------


def classify_para(rel_path: str, frontmatter_meta: dict) -> tuple[str, str | None]:
    """Return (para_type, para_owner). Phase 0 minimal — full version in Phase 1."""
    fm_type = frontmatter_meta.get("type")
    parts = rel_path.replace("\\", "/").split("/")

    root = _root_name(parts[0]) if parts else ""
    if isinstance(fm_type, str) and fm_type in VALID_FRONTMATTER_TYPES:
        para_type = fm_type
    else:
        if not parts:
            para_type = "unknown"
        elif parts[0] == INBOX_FOLDER or root == INBOX_FOLDER:
            para_type = "inbox"
        elif root == PROJECT_ROOT:
            para_type = "session" if (len(parts) >= 4 and parts[2] == "Sessions") else "project"
        elif root == AREA_ROOT:
            para_type = "session" if (len(parts) >= 4 and parts[2] == "Sessions") else "area"
        elif root == REFERENCE_ROOT:
            para_type = "reference"
        elif root == SNIPPETS_ROOT:
            para_type = "snippet"
        elif root == ARCHIVE_ROOT:
            para_type = "archive"
        else:
            para_type = "unknown"

    para_owner: str | None = None
    if len(parts) >= 2 and root in PARA_ROOTS:
        para_owner = parts[1]

    project_fm = frontmatter_meta.get("project")
    if isinstance(project_fm, str):
        first = next(_iter_wikilinks(project_fm), None)
        if first and first[0]:
            para_owner = first[0]

    return para_type, para_owner


def extract_title(content_body: str, fallback_filename: str) -> str:
    for line in content_body.splitlines():
        m = HEADING_PATTERN.match(line.rstrip("\n"))
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return Path(fallback_filename).stem


def extract_parent_index(body: str) -> str | None:
    """Look for `Parent: [[Index-Name]]` or `Parent:: [[Index-Name]]` within
    the first ~30 lines. Handles every wikilink syntactic form via the vendored
    `normalize_link_target` (anchor, alias, path-prefix, .md suffix).
    """
    for line in body.splitlines()[:30]:
        m = re.match(r"^\s*Parent\s*::?\s*\[\[([^\[\]]+?)\]\]", line)
        if m:
            return normalize_link_target(m.group(1)) or None
    return None


# ---- file indexing pipeline -------------------------------------------------


@dataclass
class IndexResult:
    path: str
    chunks: int
    duration_ms: float
    skipped: bool = False
    error: str | None = None


class Embedder:
    """Lazy-loaded sentence-transformers wrapper."""

    def __init__(self, model_name: str, device: str, cache_dir: str, batch_size: int):
        self.model_name = model_name
        self.device = device
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    log.info(f"Loading sentence-transformers model: {self.model_name}")
                    t0 = time.time()
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(
                        self.model_name,
                        device=self.device,
                        cache_folder=self.cache_dir,
                    )
                    log.info(f"Model loaded in {time.time() - t0:.1f}s")
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        arr = model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return arr.tolist()


class Indexer:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        vault_path: Path,
        chunk_size: int,
        overlap: int,
        exclude_paths: list[str],
    ):
        self.store = store
        self.embedder = embedder
        self.vault_path = Path(vault_path).resolve()
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.exclude_paths = [p.replace("\\", "/").strip("/") for p in exclude_paths]
        self.start_time = time.time()
        self.changes_processed = 0

    def is_excluded(self, abs_path: Path) -> bool:
        try:
            rel = abs_path.resolve().relative_to(self.vault_path).as_posix()
        except ValueError:
            return True
        # Dot-directory rule: never index anything under a dot-directory at any
        # depth — <history> (.history, cold storage), .trash, .redteam,
        # .obsidian, .claude, .git, .smart-env, and any future one. This is the
        # general guarantee; config.yaml's exclude_paths names the same dirs for
        # a fresh install's documentation, but the rule holds even if that list
        # is edited. Only directory segments are tested (not the filename), so a
        # cold-stored note under .history returns no live hit after reindex.
        if any(seg.startswith(".") for seg in rel.split("/")[:-1]):
            return True
        for ex in self.exclude_paths:
            if rel == ex or rel.startswith(ex + "/"):
                return True
        return False

    def _vault_rel(self, abs_path: Path) -> str:
        return abs_path.resolve().relative_to(self.vault_path).as_posix()

    def index_file(self, abs_path: Path, force: bool = False) -> IndexResult:
        t0 = time.perf_counter()
        rel = self._vault_rel(abs_path)
        try:
            stat = abs_path.stat()
        except FileNotFoundError:
            self.store.delete_file(rel)
            return IndexResult(rel, 0, (time.perf_counter() - t0) * 1000.0, skipped=True)

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as e:
            return IndexResult(rel, 0, (time.perf_counter() - t0) * 1000.0,
                               error=f"read failed: {e}")

        ch = content_hash(raw)
        if not force:
            existing = self.store.get_file_state(rel)
            if existing is not None and existing[1] == ch:
                return IndexResult(rel, 0, (time.perf_counter() - t0) * 1000.0, skipped=True)

        meta, chunks = chunk_markdown(raw, self.chunk_size, self.overlap)
        para_type, para_owner = classify_para(rel, meta)

        if para_type == "archive":
            self.store.delete_file(rel)
            return IndexResult(rel, 0, (time.perf_counter() - t0) * 1000.0, skipped=True)

        title = extract_title(strip_frontmatter(raw), rel)
        parent_index = extract_parent_index(strip_frontmatter(raw))

        fm_links = extract_frontmatter_wikilinks(meta)

        if not chunks:
            self.store.upsert_file_with_chunks(
                path=rel, para_type=para_type, para_owner=para_owner,
                parent_index=parent_index, frontmatter=meta, title=title,
                last_modified=stat.st_mtime, last_indexed=time.time(),
                content_hash_value=ch, chunks_with_embeddings=[],
                body_wikilinks=[], frontmatter_wikilinks=fm_links,
            )
            return IndexResult(rel, 0, (time.perf_counter() - t0) * 1000.0)

        embeddings = self.embedder.encode([c.text for c in chunks])
        cwe = [
            (c.chunk_index, c.text, c.section_heading, c.token_count, emb)
            for c, emb in zip(chunks, embeddings)
        ]
        body_links = [
            (c.chunk_index, dst, alias)
            for c in chunks
            for (dst, alias) in c.wikilinks
        ]
        self.store.upsert_file_with_chunks(
            path=rel, para_type=para_type, para_owner=para_owner,
            parent_index=parent_index, frontmatter=meta, title=title,
            last_modified=stat.st_mtime, last_indexed=time.time(),
            content_hash_value=ch, chunks_with_embeddings=cwe,
            body_wikilinks=body_links, frontmatter_wikilinks=fm_links,
        )
        return IndexResult(rel, len(chunks), (time.perf_counter() - t0) * 1000.0)

    def delete_file(self, abs_path: Path) -> None:
        try:
            rel = self._vault_rel(abs_path)
        except ValueError:
            return
        self.store.delete_file(rel)

    def full_walk(
        self,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """Walk vault, index changed files, GC removed ones. Returns (indexed, removed)."""
        log.info("starting full vault walk")
        all_files: list[Path] = []
        for path in self.vault_path.rglob("*.md"):
            if path.is_file() and not self.is_excluded(path):
                all_files.append(path)
        log.info(f"discovered {len(all_files)} markdown files")

        indexed = 0
        skipped = 0
        errors = 0
        seen_rel: set[str] = set()
        t_walk_start = time.time()

        for i, abs_path in enumerate(all_files):
            rel = self._vault_rel(abs_path)
            seen_rel.add(rel)
            try:
                result = self.index_file(abs_path)
                if result.error:
                    errors += 1
                    log.warning(f"error indexing {rel}: {result.error}")
                elif result.skipped:
                    skipped += 1
                else:
                    indexed += 1
            except Exception as e:
                errors += 1
                log.exception(f"failed to index {rel}: {e}")
            if progress_cb and (i + 1) % 25 == 0:
                progress_cb(i + 1, len(all_files))

        # GC
        old = self.store.all_indexed_paths()
        removed = 0
        for rel in old - seen_rel:
            self.store.delete_file(rel)
            removed += 1

        # Resolve wikilinks now that all files are indexed.
        try:
            new_resolved = self.store.resolve_links(only_unresolved=True)
            total, resolved = self.store.wikilink_count()
            cvf = self.store.cvf_link_count()
            log.info(
                f"wikilink resolution: newly_resolved={new_resolved} "
                f"total={total} resolved={resolved} cvf_excluded={cvf}"
            )
        except Exception:
            log.exception("wikilink resolution failed")

        log.info(
            f"full walk done in {time.time() - t_walk_start:.1f}s: "
            f"indexed={indexed} skipped={skipped} errors={errors} removed={removed}"
        )
        return indexed, removed


def strip_frontmatter(raw: str) -> str:
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[end + 4:].lstrip("\n")
    return raw


# ---- watcher + indexer thread ----------------------------------------------


@dataclass
class _PendingChange:
    timestamp: float
    event_type: str  # "modified" | "deleted"


class _Handler(PatternMatchingEventHandler):
    def __init__(self, indexer: Indexer, change_queue: queue.Queue):
        super().__init__(patterns=["*.md"], ignore_directories=True, case_sensitive=False)
        self.indexer = indexer
        self.queue = change_queue

    def _enqueue(self, path: str, event_type: str) -> None:
        p = Path(path)
        if self.indexer.is_excluded(p):
            return
        self.queue.put((time.time(), p, event_type))

    def on_modified(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path, "modified")

    def on_created(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path, "deleted")

    def on_moved(self, event) -> None:
        self._enqueue(event.src_path, "deleted")
        if hasattr(event, "dest_path") and event.dest_path:
            self._enqueue(event.dest_path, "modified")


class IndexerThread(threading.Thread):
    def __init__(
        self,
        indexer: Indexer,
        change_queue: queue.Queue,
        debounce_seconds: float,
        on_batch: Callable[[], None] | None = None,
    ):
        super().__init__(daemon=True, name="indexer-thread")
        self.indexer = indexer
        self.queue = change_queue
        self.debounce_seconds = debounce_seconds
        self.on_batch = on_batch
        self._stop = threading.Event()
        self._pending: dict[Path, _PendingChange] = {}
        self.pending_count_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def pending_count(self) -> int:
        with self.pending_count_lock:
            return len(self._pending)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                ts, path, event_type = self.queue.get(timeout=0.5)
                with self.pending_count_lock:
                    self._pending[path] = _PendingChange(ts, event_type)
            except queue.Empty:
                pass

            now = time.time()
            ready: list[tuple[Path, str]] = []
            with self.pending_count_lock:
                for p, change in list(self._pending.items()):
                    if now - change.timestamp >= self.debounce_seconds:
                        ready.append((p, change.event_type))
                        del self._pending[p]

            any_create_or_modify = False
            for p, ev in ready:
                try:
                    if ev == "deleted":
                        self.indexer.delete_file(p)
                    else:
                        self.indexer.index_file(p)
                        any_create_or_modify = True
                    self.indexer.changes_processed += 1
                except Exception:
                    log.exception(f"failed to process {ev} on {p}")
            if any_create_or_modify:
                try:
                    self.indexer.store.resolve_links(only_unresolved=True)
                except Exception:
                    log.exception("incremental wikilink resolution failed")
            if ready and self.on_batch is not None:
                try:
                    self.on_batch()
                except Exception:
                    log.exception("on_batch callback failed")


def start_watcher(
    indexer: Indexer,
    debounce_seconds: float,
    on_batch: Callable[[], None] | None = None,
) -> tuple[Observer, IndexerThread, queue.Queue]:
    q: queue.Queue = queue.Queue()
    handler = _Handler(indexer, q)
    observer = Observer()
    observer.schedule(handler, str(indexer.vault_path), recursive=True)
    observer.start()
    indexer_thread = IndexerThread(indexer, q, debounce_seconds, on_batch=on_batch)
    indexer_thread.start()
    return observer, indexer_thread, q
