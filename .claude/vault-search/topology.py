"""Vault topology: PARA structure, link graph rollups, quality signals.

In-memory snapshot rebuilt after every full walk and after each batch of
incremental changes. Cheap (~50ms on 350-file vault), so we don't bother with
incremental updates — full rebuild on demand.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from store import Store
from vocabulary import (
    PROJECT_ROOT,
    AREA_ROOT,
    REFERENCE_ROOT,
    SNIPPETS_ROOT,
)


# The semantic root names come from vocabulary.py (one install-time source
# generated from CONFIG § Folders). A legacy install's numeric prefix
# ("01-Projects") is stripped before comparison, and constructed paths reuse the
# root segment actually observed on disk, so both schemes work unchanged.


def _root_name(segment: str) -> str:
    """Strip a legacy numeric prefix: '01-Projects' and 'Projects' -> 'Projects'."""
    return re.sub(r"^\d+-", "", segment)


@dataclass
class ProjectInfo:
    name: str
    path: str
    project_md: str | None
    architecture_md: str | None
    open_issues_md: str | None
    sessions: list[str]
    cited_references: list[str]
    cited_snippets: list[str]
    last_modified: float
    has_frontmatter_complete: bool


@dataclass
class AreaInfo:
    name: str
    path: str
    sessions: list[str]
    last_modified: float


@dataclass
class ReferenceDomainInfo:
    name: str
    path: str
    index_path: str | None
    child_refs: list[str]
    citing_projects: dict[str, int]
    last_modified: float


@dataclass
class QualityGaps:
    missing_project_md: list[str] = field(default_factory=list)
    missing_open_issues: list[str] = field(default_factory=list)
    missing_index: list[str] = field(default_factory=list)
    orphan_references: list[str] = field(default_factory=list)


@dataclass
class VaultTopology:
    projects: dict[str, ProjectInfo] = field(default_factory=dict)
    areas: dict[str, AreaInfo] = field(default_factory=dict)
    reference_domains: dict[str, ReferenceDomainInfo] = field(default_factory=dict)
    snippet_domains: dict[str, ReferenceDomainInfo] = field(default_factory=dict)
    quality_gaps: QualityGaps = field(default_factory=QualityGaps)
    workflow_clusters: list[Any] = field(default_factory=list)  # populated in Phase 3
    last_full_scan: float = 0.0


class TopologyCache:
    """Thread-safe wrapper around a VaultTopology snapshot."""

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.RLock()
        self._topology: VaultTopology | None = None

    def get(self) -> VaultTopology:
        with self._lock:
            if self._topology is None:
                self._topology = build_topology(self.store)
            return self._topology

    def invalidate(self) -> None:
        with self._lock:
            self._topology = None

    def rebuild(self) -> VaultTopology:
        with self._lock:
            self._topology = build_topology(self.store)
            return self._topology


# ---- builders ---------------------------------------------------------------


def build_topology(store: Store) -> VaultTopology:
    t0 = time.time()
    files_by_root: dict[str, list[dict[str, Any]]] = {
        PROJECT_ROOT: [],
        AREA_ROOT: [],
        REFERENCE_ROOT: [],
        SNIPPETS_ROOT: [],
    }
    rows = store.all_file_rows()

    file_by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        file_by_path[d["path"]] = d
        seg0 = d["path"].replace("\\", "/").split("/", 1)[0]
        root = _root_name(seg0)
        if root in files_by_root and "/" in d["path"].replace("\\", "/"):
            files_by_root[root].append(d)

    projects = _build_projects(store, files_by_root[PROJECT_ROOT])
    areas = _build_areas(files_by_root[AREA_ROOT])
    ref_domains = _build_domains(store, files_by_root[REFERENCE_ROOT], REFERENCE_ROOT)
    snip_domains = _build_domains(store, files_by_root[SNIPPETS_ROOT], SNIPPETS_ROOT)

    quality_gaps = _detect_quality_gaps(
        store, projects, ref_domains, file_by_path
    )

    try:
        clusters = store.list_workflow_clusters()
    except Exception:
        clusters = []

    return VaultTopology(
        projects=projects,
        areas=areas,
        reference_domains=ref_domains,
        snippet_domains=snip_domains,
        quality_gaps=quality_gaps,
        workflow_clusters=clusters,
        last_full_scan=t0,
    )


def _has_complete_frontmatter(frontmatter_json: str | None) -> bool:
    if not frontmatter_json:
        return False
    try:
        meta = json.loads(frontmatter_json)
    except Exception:
        return False
    return all(k in meta for k in ("type", "tags", "date"))


def _on_disk_root(files: list[dict[str, Any]], default: str) -> str:
    """The root segment as it exists on disk (e.g. 'Projects', or a legacy
    '01-Projects'), read from the first member's path so constructed paths
    match either naming scheme."""
    for f in files:
        seg0 = f["path"].replace("\\", "/").split("/", 1)[0]
        if seg0:
            return seg0
    return default


def _build_projects(
    store: Store, project_files: list[dict[str, Any]]
) -> dict[str, ProjectInfo]:
    """Group project files by para_owner. Detect Project.md, sessions, refs."""
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for f in project_files:
        owner = f["para_owner"]
        if owner is None:
            continue
        by_owner.setdefault(owner, []).append(f)

    projects: dict[str, ProjectInfo] = {}
    for owner, files in by_owner.items():
        sessions = sorted(
            (f["path"] for f in files if f["para_type"] == "session"),
            reverse=True,
        )
        non_session_files = [f for f in files if f["para_type"] != "session"]
        disk_root = _on_disk_root(files, PROJECT_ROOT)

        project_md = _find_project_root_md(owner, non_session_files, disk_root)
        architecture_md = _find_named(non_session_files, "Architecture.md")
        open_issues_md = _find_named(non_session_files, "Open-Issues.md")

        last_mod = max((f["last_modified"] or 0.0) for f in files) if files else 0.0

        # outbound refs from any file in this project
        ref_paths, snip_paths = _outbound_refs_for_owner(store, owner)

        complete_fm = (
            _has_complete_frontmatter(
                next(
                    (f["frontmatter_json"] for f in non_session_files
                     if f["path"] == project_md),
                    None,
                )
            ) if project_md else False
        )

        projects[owner] = ProjectInfo(
            name=owner,
            path=f"{disk_root}/{owner}",
            project_md=project_md,
            architecture_md=architecture_md,
            open_issues_md=open_issues_md,
            sessions=sessions,
            cited_references=ref_paths,
            cited_snippets=snip_paths,
            last_modified=last_mod,
            has_frontmatter_complete=complete_fm,
        )
    return projects


def _find_project_root_md(
    owner: str, files: list[dict[str, Any]], disk_root: str = PROJECT_ROOT
) -> str | None:
    """Heuristic: prefer <projects>/<owner>/<owner>.md (the folder-note cover),
    else Project.md, else any single file whose para_type == 'project'."""
    expected_a = f"{disk_root}/{owner}/{owner}.md"
    expected_b = f"{disk_root}/{owner}/Project.md"
    paths = {f["path"]: f for f in files}
    if expected_a in paths:
        return expected_a
    if expected_b in paths:
        return expected_b
    project_typed = [
        f for f in files
        if f["para_type"] == "project" and "/Sessions/" not in f["path"]
    ]
    if len(project_typed) == 1:
        return project_typed[0]["path"]
    if project_typed:
        return min(project_typed, key=lambda f: f["path"])["path"]
    return None


def _find_named(files: list[dict[str, Any]], name: str) -> str | None:
    for f in files:
        if f["path"].endswith("/" + name):
            return f["path"]
    return None


def _outbound_refs_for_owner(
    store: Store, owner: str
) -> tuple[list[str], list[str]]:
    rows = store.outbound_ref_rows_for_owner(owner)
    refs = [r["path"] for r in rows if r["para_type"] in ("reference", "index")]
    snips = [r["path"] for r in rows if r["para_type"] == "snippet"]
    return refs, snips


def _build_areas(area_files: list[dict[str, Any]]) -> dict[str, AreaInfo]:
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for f in area_files:
        owner = f["para_owner"]
        if owner is None:
            continue
        by_owner.setdefault(owner, []).append(f)
    areas: dict[str, AreaInfo] = {}
    for owner, files in by_owner.items():
        sessions = sorted(
            (f["path"] for f in files if f["para_type"] == "session"),
            reverse=True,
        )
        last_mod = max((f["last_modified"] or 0.0) for f in files) if files else 0.0
        areas[owner] = AreaInfo(
            name=owner,
            path=f"{_on_disk_root(files, AREA_ROOT)}/{owner}",
            sessions=sessions,
            last_modified=last_mod,
        )
    return areas


def _build_domains(
    store: Store,
    domain_files: list[dict[str, Any]],
    root: str,
) -> dict[str, ReferenceDomainInfo]:
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for f in domain_files:
        owner = f["para_owner"]
        if owner is None:
            continue
        by_owner.setdefault(owner, []).append(f)

    domains: dict[str, ReferenceDomainInfo] = {}
    for owner, files in by_owner.items():
        disk_root = _on_disk_root(files, root)
        index_path = _find_index(owner, files)
        child_refs = sorted(f["path"] for f in files)
        citing_projects = _citing_projects_for_domain(store, owner, disk_root)
        last_mod = max((f["last_modified"] or 0.0) for f in files) if files else 0.0
        domains[owner] = ReferenceDomainInfo(
            name=owner,
            path=f"{disk_root}/{owner}",
            index_path=index_path,
            child_refs=child_refs,
            citing_projects=citing_projects,
            last_modified=last_mod,
        )
    return domains


def _find_index(owner: str, files: list[dict[str, Any]]) -> str | None:
    index_typed = [f for f in files if f["para_type"] == "index"]
    if index_typed:
        # Prefer one whose basename matches "<owner>-Index".
        target_stem = f"{owner}-Index.md".lower()
        for f in index_typed:
            if f["path"].lower().endswith("/" + target_stem):
                return f["path"]
        return min(index_typed, key=lambda f: f["path"])["path"]
    # Fallback by filename pattern even if frontmatter type is wrong.
    for f in files:
        if f["path"].lower().endswith(f"/{owner.lower()}-index.md"):
            return f["path"]
    return None


def _citing_projects_for_domain(
    store: Store, domain: str, root: str
) -> dict[str, int]:
    rows = store.citing_projects_for_domain(domain, root)
    return {r["project"]: r["c"] for r in rows}


def _detect_quality_gaps(
    store: Store,
    projects: dict[str, ProjectInfo],
    ref_domains: dict[str, ReferenceDomainInfo],
    file_by_path: dict[str, dict[str, Any]],
) -> QualityGaps:
    gaps = QualityGaps()
    for owner, proj in projects.items():
        if proj.project_md is None:
            gaps.missing_project_md.append(proj.path)
        if proj.open_issues_md is None:
            gaps.missing_open_issues.append(proj.path)
    for owner, dom in ref_domains.items():
        if dom.index_path is None:
            gaps.missing_index.append(dom.path)

    # orphan_references: type=reference with 0 inbound wikilinks AND no parent_index.
    gaps.orphan_references = store.orphan_reference_paths()

    return gaps


# ---- helpers used by tools --------------------------------------------------


def project_info_dict(p: ProjectInfo) -> dict[str, Any]:
    return {
        "name": p.name,
        "path": p.path,
        "project_md": p.project_md,
        "architecture_md": p.architecture_md,
        "open_issues_md": p.open_issues_md,
        "sessions": p.sessions,
        "session_count": len(p.sessions),
        "cited_references": p.cited_references,
        "cited_snippets": p.cited_snippets,
        "ref_count": len(p.cited_references) + len(p.cited_snippets),
        "last_modified": p.last_modified,
        "has_frontmatter_complete": p.has_frontmatter_complete,
    }


def domain_info_dict(d: ReferenceDomainInfo) -> dict[str, Any]:
    return {
        "name": d.name,
        "path": d.path,
        "index_path": d.index_path,
        "child_refs": d.child_refs,
        "child_count": len(d.child_refs),
        "citing_projects": d.citing_projects,
        "citing_project_count": len(d.citing_projects),
        "last_modified": d.last_modified,
    }
