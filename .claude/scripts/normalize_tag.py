"""Resolve a frontmatter `tags:` value to its canonical key.

Thin wrapper — all logic lives in config_variables.normalize_tag().

Public API
----------
normalize_tag(value: str) -> str | None
    Canonical tag key, or None if unresolvable.

CANONICAL_TAG_KEYS : frozenset[str]
    The full set of canonical tag keys from CONFIG.md ## Tags.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_variables import normalize_tag, CANONICAL_TAG_KEYS  # noqa: F401

__all__ = ["normalize_tag", "CANONICAL_TAG_KEYS"]

if __name__ == "__main__":
    from config_variables import TAGS
    print(f"Canonical tags: {sorted(CANONICAL_TAG_KEYS)}")
    failures = 0
    for row in TAGS.values():
        for alias in row.aliases:
            result = normalize_tag(alias)
            if result == row.tag:
                print(f"OK   normalize_tag({alias!r}) -> {result!r}")
            else:
                print(f"FAIL normalize_tag({alias!r}) -> {result!r}  (expected {row.tag!r})")
                failures += 1
    if failures:
        print(f"\n{failures} test(s) failed.")
        sys.exit(1)
    print("\nnormalize_tag self-tests passed.")
