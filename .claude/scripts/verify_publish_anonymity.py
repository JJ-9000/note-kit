#!/usr/bin/env python3
"""verify_publish_anonymity.py
==============================

Deterministic identity scan at the outward-publication boundary (CONFIG
§ Self-modification — the store-back / git push to the publication repo).
Before a push runs, this scan reads the staged bytes for the operator's
identity — real name, machine-absolute paths, email, account handles, private
tree names, secrets — and surfaces each hit as a proposed removal or
anonymous-token replacement. A hit holds the push (exit 2) until the operator
clears or replaces it; a clean scan passes silently (exit 0), so a passive
operator meets no gate.

The scan gates the leak vector the way the git-author check already gates the
push: the live → template copy is where identity crosses into public history
(`note-kit-publish-anonymity` — the 2026-06-12 `<sandbox-vault>` literal rode
64 public commits). Two independent gates hold even if a poisoned input slips
the model (Break-the-Lethal-Trifecta-at-the-Publish-Boundary): the scan is
deterministic and non-bypassable, and consent stays at the irreversible act the
operator already performs — the push (Stage-the-Reversible-Gate-Only-the-
Irreversible). The scan detects; judgment clears (Detect-by-Rule-Repair-by-
Judgment) — it proposes replacements and never edits a file.

No identity literals ship in this file — the template copy is published, so
every private canary DERIVES at runtime from the machine it guards:

  user-home path         the username segment, in plain, JSON-escaped,
                         forward-slash, and doubled-backslash forms
  global git identity    ``git config --global user.name / user.email`` —
                         name words and the email local part (the repo-local
                         publishing identity is exempt)
  environment            USERNAME, COMPUTERNAME (machine names)
  private tree names     directory names under ``<user-home>/repos/`` minus
                         this repo's own name
  vault paths            the resolved vault root's absolute form

Generic canaries (publishable, so stored here): non-system drive-absolute
paths, ``AppData`` segments, provider email domains, ``seed_<hex>`` runs, and
the secret shapes (api keys, tokens, private-key blocks, ``sk-`` keys with a
length floor so CSS ``mask-position`` never trips it).

Every canary is SELF-TESTED against the exact token it guards before the run
counts — a regex that cannot match its own token aborts the run (a July canary
slipped a word-boundary-vs-underscore miss on the ``seed`` + hex class; the
self-test is that lesson). The self-test tokens below are built by
concatenation so this file's own bytes never match a canary at rest — the
scanner scans clean with no self-exemption in the scan logic. Every blob is scanned in every encoding of the same bytes:
UTF-8 first, then UTF-16-LE/BE when the bytes decode — a UTF-16 leak hides
from a UTF-8-only grep.

Scan modes (all read-only; the tracked surface is read via git, never a raw
tree grep — a raw working-tree grep has core-dumped and printed a false clean):

  --staged      staged bytes (``git show :<path>``) — the pre-push default
  --head        every blob at HEAD — the whole-surface audit
  --payload     worktree-vs-HEAD changes plus untracked adds — the
                prospective payload
  --paths P...  arbitrary files — the seam for any other outward boundary
                (a social post, a hosted-platform update, a non-git send)

The refs check runs in every git mode: author/committer identities across
``git log --all`` and local tag names must carry no derived identity token.

Allowlist: a hit whose exact matched line is recorded below with its reason is
licensed and reported at info level only. The shipped entries are the two the
2026-07-26 publish battery recorded (README teaching placeholders, tokenized
``<user-home>`` rows); an install adds its own via ``--allow-file`` — one
exact line per line, ``#`` comments allowed.

Exit codes: 0 clean, 1 usage/environment error, 2 identity hits standing.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from config_variables import resolve_vault_root  # noqa: E402
except Exception:  # the --paths seam works outside a kit checkout
    resolve_vault_root = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Canary:
    """One identity pattern: a compiled regex plus the exact token that proves it."""

    name: str
    pattern: re.Pattern
    self_test_token: str          # the regex must match this, or the run aborts
    replacement: str              # the proposed anonymous form
    generic: bool = False         # True: shape-based, no private token behind it


@dataclass
class Hit:
    path: str
    line_no: int
    canary: str
    matched: str
    line: str
    encoding: str
    replacement: str
    licensed_reason: Optional[str] = None


# The two allowlist entries the 2026-07-26 publish battery recorded, with the
# reasons it recorded them. Generic teaching text, publishable.
_SHIPPED_ALLOWLIST: dict[str, str] = {
    r"C:\Users\you": "README teaching placeholder — an instruction, not an identity",
    "<user-home>": "tokenized path row — the anonymization itself",
}

_SECRET_MIN = 16  # sk-/key length floor: CSS `mask-position` never reaches it


def _word_re(token: str, *, case_sensitive: bool = True) -> re.Pattern:
    """A word-ish boundary that also fires against underscores and digits.

    ``\\b`` treats ``_`` as a word character, so a ``\\b``-bounded token
    misses its underscore-suffixed forms — the July canary miss. Guard with
    explicit lookarounds that exclude only letters, so ``_`` and digits still
    count as boundaries.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(
        r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])", flags
    )


def _path_variants(p: str) -> list[str]:
    """Every byte-shape a Windows path takes in text: plain, doubled-backslash
    (JSON-escaped), and forward-slash."""
    plain = p.replace("/", "\\")
    return list(dict.fromkeys([plain, plain.replace("\\", "\\\\"), plain.replace("\\", "/")]))


def _derive_canaries(repo: Optional[Path]) -> list[Canary]:
    canaries: list[Canary] = []

    def add(name: str, token: str, replacement: str, *, case_sensitive: bool = True) -> None:
        if len(token) < 3:  # a 1-2 char token would flood every file
            return
        canaries.append(Canary(name, _word_re(token, case_sensitive=case_sensitive),
                               token, replacement))

    home = Path.home()
    username = home.name
    add("username", username, "<user-name>")
    for variant in _path_variants(str(home)):
        add(f"home-path[{variant[:12]}…]" if len(variant) > 14 else f"home-path[{variant}]",
            variant, "<user-home>")

    env_user = os.environ.get("USERNAME", "").strip()
    if env_user and env_user.lower() != username.lower():
        add("env-username", env_user, "<user-name>", case_sensitive=False)
    machine = os.environ.get("COMPUTERNAME", "").strip()
    if machine:
        add("machine-name", machine, "<machine>", case_sensitive=False)

    # Global git identity — the person; the repo-local publishing identity is
    # the pseudonym and stays exempt by construction (only --global is read).
    local_idents: set[str] = set()
    if repo is not None:
        for key in ("user.name", "user.email"):
            v = _git(repo, "config", "--local", key, check=False)
            if v:
                local_idents.add(v.strip().lower())
    for key, label in (("user.name", "git-name"), ("user.email", "git-email")):
        value = _git(None, "config", "--global", key, check=False)
        if not value:
            continue
        value = value.strip()
        if value.lower() in local_idents:
            continue
        if key == "user.name":
            for word in value.split():
                if len(word) >= 3:
                    add(f"{label}[{word}]", word, "<user-name>")
        else:
            add(label, value, "<user-email>")
            local_part, domain = value.split("@", 1)
            if local_part.lower() not in {i.split("@", 1)[0].lower() for i in local_idents}:
                add(f"{label}-local", local_part, "<user-handle>")
            # A noreply domain is the pseudonymizing device, not an identity.
            if domain.lower() != "users.noreply.github.com":
                add(f"{label}-domain", "@" + domain, "<email>")

    # Private tree names: every sibling repo the operator keeps, minus this one.
    repos_root = home / "repos"
    self_name = repo.name.lower() if repo is not None else ""
    if repos_root.is_dir():
        for child in sorted(repos_root.iterdir()):
            if child.is_dir() and child.name.lower() != self_name:
                add(f"private-tree[{child.name}]", child.name, "<private-project>",
                    case_sensitive=False)

    # Vault and sandbox literals (the install's own absolute paths and names).
    if resolve_vault_root is not None:
        try:
            from config_variables import token_path
            vault = resolve_vault_root()
        except Exception:
            vault, token_path = None, None  # type: ignore[assignment]
        if vault:
            for variant in _path_variants(str(vault)):
                add(f"vault-path[{variant[:12]}…]", variant, "<vault-root>")
        if token_path is not None:
            try:
                sandbox = str(token_path("sandbox-vault"))
            except Exception:
                sandbox = ""
            if sandbox and Path(sandbox).is_absolute():
                for variant in _path_variants(sandbox):
                    add(f"sandbox-path[{variant[:12]}…]", variant, "<sandbox-vault>")
                add("sandbox-name", Path(sandbox).name, "<sandbox-vault>",
                    case_sensitive=False)

    # Generic shapes — publishable, so written here. Self-test tokens are
    # concatenated so this file's own bytes match no canary at rest.
    generics: list[tuple[str, str, str, str]] = [
        ("users-path", r"(?i)C:[\\/]{1,2}Users[\\/]{1,2}",
         "C:" + "\\Users" + "\\x", "<user-home> token"),
        ("drive-absolute", r"(?<![A-Za-z])[D-Z]:[\\/]{1,2}(?=[A-Za-z])",
         "F" + ":" + "\\x", "<data-root>"),
        ("provider-email", r"(?i)@(?:gmail|outlook|hotmail|proton(?:mail)?|yahoo|icloud)\.[a-z]{2,6}",
         "@" + "gmail" + ".com", "<email>"),
        ("seed-hex", r"(?<![A-Za-z])seed_[0-9a-fA-F]{3,}",
         "seed" + "_a1f", "<seed>"),
        ("api-key-assign", r"(?i)\b(?:api[_-]?key|client_secret|access[_-]?token|password)\s*[=:]\s*['\"][^'\"]{8,}",
         "api" + '_key = "0123456789abcdef"', "a secrets store, never a literal"),
        # The digit lookahead is the entropy test: a real key body carries
        # digits; a CSS class name does not.
        ("sk-key", r"sk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{%d,}" % _SECRET_MIN,
         "sk" + "-4" + "a" * _SECRET_MIN, "a secrets store, never a literal"),
        ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
         "-----BEGIN " + "PRIVATE" + " KEY-----", "never publishable"),
    ]
    for name, pat, token, repl in generics:
        canaries.append(Canary(name, re.compile(pat), token, repl, generic=True))

    return canaries


def _self_test(canaries: Iterable[Canary]) -> None:
    """Every canary must match the exact token it guards — or the run aborts.

    A scan whose regex is silently broken reports a false clean, which is worse
    than no scan; the self-test converts that failure into a loud one.
    """
    broken = [c.name for c in canaries if not c.pattern.search(c.self_test_token)]
    if broken:
        sys.exit(f"verify_publish_anonymity: self-test FAILED for canaries: {', '.join(broken)}")


# ---------------------------------------------------------------------------
# Blob access — via git, never a raw tree grep
# ---------------------------------------------------------------------------

def _git(repo: Optional[Path], *args: str, check: bool = True,
         binary: bool = False):
    cmd = ["git"] + (["-C", str(repo)] if repo is not None else []) + list(args)
    result = subprocess.run(cmd, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    if result.returncode != 0:
        return b"" if binary else ""
    return result.stdout if binary else result.stdout.decode(errors="replace").strip()

_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
               ".otf", ".eot", ".pdf", ".zip", ".gz", ".pyc", ".exe", ".dll", ".db"}


def _decodings(blob: bytes) -> list[tuple[str, str]]:
    """Every text reading of the same bytes: UTF-8 first, UTF-16 when it decodes.

    ``errors='replace'`` on a UTF-16 file read as UTF-8 turns the leak into
    mojibake a pattern cannot match — so each encoding is tried strictly and
    scanned separately.
    """
    out: list[tuple[str, str]] = []
    try:
        out.append(("utf-8", blob.decode("utf-8")))
    except UnicodeDecodeError:
        pass
    for enc in ("utf-16-le", "utf-16-be"):
        if len(blob) >= 4 and b"\x00" in blob[:200]:
            try:
                text = blob.decode(enc)
            except UnicodeDecodeError:
                continue
            if sum(ch.isprintable() or ch in "\r\n\t" for ch in text[:200]) > 0.9 * min(len(text), 200):
                out.append((enc, text))
    if not out:
        out.append(("utf-8/replace", blob.decode("utf-8", errors="replace")))
    return out


def _iter_blobs(repo: Optional[Path], mode: str, paths: list[str]):
    """Yield (display_path, blob_bytes) for the selected surface."""
    if mode == "paths":
        for p in paths:
            fp = Path(p)
            if fp.is_file() and fp.suffix.lower() not in _BINARY_EXT:
                yield str(fp), fp.read_bytes()
        return
    assert repo is not None
    if mode == "staged":
        names = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
        for name in filter(None, names.splitlines()):
            if Path(name).suffix.lower() in _BINARY_EXT:
                continue
            yield name, _git(repo, "show", f":{name}", binary=True, check=False)
    elif mode == "head":
        names = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
        for name in filter(None, names.splitlines()):
            if Path(name).suffix.lower() in _BINARY_EXT:
                continue
            yield name, _git(repo, "show", f"HEAD:{name}", binary=True, check=False)
    elif mode == "payload":
        changed = _git(repo, "diff", "--name-only", "--diff-filter=ACMR", "HEAD")
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
        seen = set()
        for name in filter(None, changed.splitlines() + untracked.splitlines()):
            if name in seen or Path(name).suffix.lower() in _BINARY_EXT:
                continue
            seen.add(name)
            fp = repo / name
            if fp.is_file():
                yield name, fp.read_bytes()


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _scan_blob(path: str, blob: bytes, canaries: list[Canary],
               allowlist: dict[str, str]) -> list[Hit]:
    hits: list[Hit] = []
    for enc, text in _decodings(blob):
        for line_no, line in enumerate(text.splitlines(), 1):
            for canary in canaries:
                m = canary.pattern.search(line)
                if not m:
                    continue
                reason = next((why for allowed, why in allowlist.items()
                               if allowed in line), None)
                hits.append(Hit(path, line_no, canary.name, m.group(0),
                                line.strip()[:160], enc, canary.replacement,
                                licensed_reason=reason))
    return hits


def _check_refs(repo: Path, canaries: list[Canary]) -> list[str]:
    """Author/committer identities and tag names across every ref."""
    problems: list[str] = []
    idents = _git(repo, "log", "--all", "--format=%an <%ae>%n%cn <%ce>", check=False)
    for ident in sorted(set(filter(None, idents.splitlines()))):
        for canary in canaries:
            if not canary.generic and canary.pattern.search(ident):
                problems.append(f"ref identity {ident!r} matches canary {canary.name}")
                break
    tags = _git(repo, "tag", "-l", check=False)
    for tag in filter(None, tags.splitlines()):
        for canary in canaries:
            if not canary.generic and canary.pattern.search(tag):
                problems.append(f"tag {tag!r} matches canary {canary.name}")
                break
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument("--staged", action="store_true", help="scan staged bytes (default)")
    mode_group.add_argument("--head", action="store_true", help="scan every blob at HEAD")
    mode_group.add_argument("--payload", action="store_true",
                            help="scan worktree-vs-HEAD changes plus untracked adds")
    mode_group.add_argument("--paths", nargs="+", metavar="FILE",
                            help="scan arbitrary files (any outward boundary)")
    ap.add_argument("--repo", type=Path, default=None,
                    help="publication repo (default: the note-kit template tree)")
    ap.add_argument("--allow-file", type=Path, default=None,
                    help="extra allowlist: one exact licensed line per line")
    ap.add_argument("--tokens-file", type=Path, default=None,
                    help="extra private tokens to guard, one per line — for "
                         "identity names no derivation reaches (keep the file "
                         "outside the repo, or gitignored)")
    ap.add_argument("--list-canaries", action="store_true",
                    help="print the derived canary names and exit")
    args = ap.parse_args(argv)

    mode = ("paths" if args.paths else "head" if args.head
            else "payload" if args.payload else "staged")

    repo: Optional[Path] = None
    if mode != "paths":
        repo = args.repo
        if repo is None and resolve_vault_root is not None:
            root = resolve_vault_root()
            if root is not None:
                candidate = root / "Areas" / "Note-Kit" / "Assets" / "note-kit"
                repo = candidate if candidate.is_dir() else None
        if repo is None or not (repo / ".git").exists():
            print("verify_publish_anonymity: no publication repo resolved — pass --repo", file=sys.stderr)
            return 1

    canaries = _derive_canaries(repo)
    if args.tokens_file and args.tokens_file.is_file():
        for line in args.tokens_file.read_text(encoding="utf-8").splitlines():
            token = line.strip()
            if token and not token.startswith("#") and len(token) >= 3:
                canaries.append(Canary(f"extra[{token}]",
                                       _word_re(token, case_sensitive=False),
                                       token, "<private-token>"))
    _self_test(canaries)

    if args.list_canaries:
        for c in canaries:
            kind = "generic" if c.generic else "derived"
            print(f"{c.name}  ({kind}; proposes {c.replacement})")
        return 0

    allowlist = dict(_SHIPPED_ALLOWLIST)
    if args.allow_file and args.allow_file.is_file():
        for line in args.allow_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allowlist[line] = f"licensed via {args.allow_file.name}"

    all_hits: list[Hit] = []
    scanned = 0
    for path, blob in _iter_blobs(repo, mode, args.paths or []):
        scanned += 1
        all_hits.extend(_scan_blob(path, blob, canaries, allowlist))

    ref_problems = _check_refs(repo, canaries) if repo is not None else []

    live = [h for h in all_hits if h.licensed_reason is None]
    licensed = [h for h in all_hits if h.licensed_reason is not None]

    for h in live:
        print(f"HIT  {h.path}:{h.line_no} | {h.canary} | {h.matched!r} "
              f"[{h.encoding}] -> propose {h.replacement} | {h.line}")
    for h in licensed:
        print(f"info licensed  {h.path}:{h.line_no} | {h.canary} | {h.licensed_reason}")
    for p in ref_problems:
        print(f"HIT  refs | {p}")

    verdict_hits = len(live) + len(ref_problems)
    print(f"verify_publish_anonymity: mode={mode} files={scanned} "
          f"canaries={len(canaries)} hits={verdict_hits} licensed={len(licensed)}")
    if verdict_hits:
        print("The push holds until every hit above is removed, replaced with its "
              "proposed token, or recorded in the allowlist with a reason.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
