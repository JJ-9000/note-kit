"""Single runtime source for the daemon's folder vocabulary, inbox name, and
canonical note-type set.

`generate_vocabulary.py` writes `vocabulary.json` from CONFIG § Folders and
§ Types at install time (the daemon runs its own venv, so it generates rather
than importing the kit's `config_variables`). This module loads that JSON and
exposes the values indexer.py, tools.py, and topology.py read — replacing the
folder-name and type-set literals those modules used to each hold their own copy
of.

If `vocabulary.json` is absent or unparseable the module falls back to the
kit-default § Folders literals and § Types keys below, so the daemon always
starts. The fallbacks are the safety net, not the source: keep them in step with
CONFIG's defaults, and let generate_vocabulary.py carry any per-install rename.
"""
from __future__ import annotations

import json
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_VOCAB_PATH = _DAEMON_DIR / "vocabulary.json"

# Fallback § Folders literals, keyed by the wildcard's bare semantic name.
# Synced to CONFIG's default § Folders table.
_FALLBACK_ROOTS: dict[str, str] = {
    "inbox": "Inbox",
    "outbox": "Outbox",
    "projects": "Projects",
    "areas": "Areas",
    "reference": "References",
    "snippets": "Snippets",
    "archive": "Archive",
}

# Fallback § Types keys. Synced to CONFIG's default § Types table (18 types).
_FALLBACK_TYPES: frozenset[str] = frozenset({
    "project", "area", "reference", "voice", "design", "format", "research",
    "session", "plan", "note", "journal", "idea", "snippet", "source",
    "index", "addendum", "log", "revision",
})


def _load() -> tuple[dict[str, str], frozenset[str]]:
    roots = dict(_FALLBACK_ROOTS)
    types = set(_FALLBACK_TYPES)
    try:
        data = json.loads(_VOCAB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return roots, frozenset(types)
    if isinstance(data, dict):
        j_roots = data.get("roots")
        if isinstance(j_roots, dict) and j_roots:
            roots.update({k: str(v) for k, v in j_roots.items() if v})
        j_types = data.get("types")
        if isinstance(j_types, list) and j_types:
            # Merge over the fallback (like roots): the JSON adds any extra types
            # but never removes a fallback one, so a shrunken vocabulary.json
            # degrades a missing type to the fallback rather than silently
            # dropping it back into classify-by-folder.
            types.update(str(t) for t in j_types if t)
    return roots, frozenset(types)


_ROOTS, CANONICAL_TYPES = _load()

INBOX_FOLDER = _ROOTS["inbox"]
OUTBOX_FOLDER = _ROOTS["outbox"]
PROJECT_ROOT = _ROOTS["projects"]
AREA_ROOT = _ROOTS["areas"]
REFERENCE_ROOT = _ROOTS["reference"]
SNIPPETS_ROOT = _ROOTS["snippets"]
ARCHIVE_ROOT = _ROOTS["archive"]

# The four PARA roots a path's owner segment is read under (classifiers and the
# topology builder share this set).
PARA_ROOTS: frozenset[str] = frozenset({
    PROJECT_ROOT, AREA_ROOT, REFERENCE_ROOT, SNIPPETS_ROOT,
})
