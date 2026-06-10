"""Resolve a frontmatter `type:` value to its canonical key.

Resolution order (cheapest first):
    1. Strip whitespace, lowercase.
    2. Exact canonical match (from CONFIG.md ## Types).
    3. Stable alias lookup (renames, deprecated terms).
    4. Trailing-s plural collapse.
    5. Levenshtein-1 fuzzy match.
    6. Return None — unresolvable.

Public API
----------
normalize_type(value: str) -> str | None
    Canonical type key, or None if unresolvable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_variables import CANONICAL_TYPE_KEYS, levenshtein_le1

# Stable non-trivial substitutions (renames, deprecated terms).
# Casing, plurals, and Levenshtein-1 typos are handled below.
_ALIASES: dict[str, str] = {
    "moc": "index",
}


def normalize_type(value: str) -> str | None:
    """Resolve `value` to its canonical type form, or None if unresolvable."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not candidate:
        return None
    if candidate in CANONICAL_TYPE_KEYS:
        return candidate
    if candidate in _ALIASES:
        return _ALIASES[candidate]
    if candidate.endswith("s") and len(candidate) > 1 and candidate[:-1] in CANONICAL_TYPE_KEYS:
        return candidate[:-1]
    for canonical in CANONICAL_TYPE_KEYS:
        if levenshtein_le1(candidate, canonical):
            return canonical
    return None


if __name__ == "__main__":
    tests = [
        ("note", "note"),
        ("Notes", "note"),
        ("NOTE", "note"),
        ("moc", "index"),
        ("snippets", "snippet"),
        ("referense", "reference"),  # Levenshtein-1
        ("xyz_impossible_type", None),
    ]
    failures = 0
    for val, expected in tests:
        result = normalize_type(val)
        if result == expected:
            print(f"OK   normalize_type({val!r}) -> {result!r}")
        else:
            print(f"FAIL normalize_type({val!r}) -> {result!r}  (expected {expected!r})")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed.")
        sys.exit(1)
    print("\nnormalize_type self-tests passed.")
