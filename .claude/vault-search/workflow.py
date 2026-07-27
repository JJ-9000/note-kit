"""Workflow clustering: discover groups of References co-cited across projects.

Per spec §5.10:
1. Build co-citation graph: edge weight = Jaccard over citing-project sets.
2. Run Louvain community detection.
3. Filter clusters by min_size, persist with centroid refs / co-citing projects /
   member sessions / score.
4. Optional Haiku labeling — gated by config; not invoked by default.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import networkx as nx
from networkx.algorithms.community import louvain_communities, modularity

from store import Store


log = logging.getLogger("vault.workflow")


def build_workflow_clusters(
    store: Store,
    jaccard_threshold: float = 0.15,
    min_cluster_size: int = 2,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Returns a list of cluster dicts ready for store.replace_workflow_clusters."""
    t0 = time.time()
    ref_to_projects = _ref_to_citing_projects(store)
    if not ref_to_projects:
        log.info("workflow_clustering: no cited references")
        return []

    refs = list(ref_to_projects.keys())
    n = len(refs)
    log.info(f"workflow_clustering: {n} cited references, building co-citation graph")

    G = nx.Graph()
    for r in refs:
        G.add_node(r)
    for i in range(n):
        a = refs[i]
        a_set = ref_to_projects[a]
        if not a_set:
            continue
        for j in range(i + 1, n):
            b = refs[j]
            b_set = ref_to_projects[b]
            if not b_set:
                continue
            inter = a_set & b_set
            if not inter:
                continue
            union = a_set | b_set
            j_score = len(inter) / len(union)
            if j_score >= jaccard_threshold:
                G.add_edge(a, b, weight=j_score)

    log.info(
        f"workflow_clustering: graph nodes={G.number_of_nodes()} "
        f"edges={G.number_of_edges()}"
    )

    if G.number_of_edges() == 0:
        return []

    communities = louvain_communities(G, weight="weight", seed=seed)
    log.info(f"workflow_clustering: {len(communities)} communities (raw)")

    member_sessions = _ref_to_member_sessions(store)

    clusters: list[dict[str, Any]] = []
    for community in communities:
        community = list(community)
        if len(community) < min_cluster_size:
            continue
        # Compute co-citing projects (union of citing-project sets)
        co_proj: set[str] = set()
        for r in community:
            co_proj |= ref_to_projects.get(r, set())
        if len(co_proj) < 2:
            continue
        score = _cluster_modularity(G, community)
        # Member sessions: sessions that wikilink to >=2 refs in this community
        community_set = set(community)
        sessions: list[str] = []
        for sess_path, sess_refs in member_sessions.items():
            if len(sess_refs & community_set) >= 2:
                sessions.append(sess_path)

        clusters.append({
            "label": None,
            "centroid_refs": sorted(community),
            "co_citing_projects": sorted(co_proj),
            "member_sessions": sorted(sessions),
            "score": round(score, 4),
            "computed_at": time.time(),
        })
    clusters.sort(key=lambda c: -c["score"])
    log.info(
        f"workflow_clustering: produced {len(clusters)} clusters "
        f"in {time.time() - t0:.1f}s"
    )
    return clusters


def _ref_to_citing_projects(store: Store) -> dict[str, set[str]]:
    """For each Reference/Snippet/Index path, set of project owners that cite it."""
    out: dict[str, set[str]] = {}
    for r in store.ref_to_citing_projects_rows():
        out.setdefault(r["ref_path"], set()).add(r["project"])
    return out


def _ref_to_member_sessions(store: Store) -> dict[str, set[str]]:
    """For each session path, set of reference paths it wikilinks to."""
    out: dict[str, set[str]] = {}
    for r in store.session_to_cited_refs_rows():
        out.setdefault(r["session_path"], set()).add(r["ref_path"])
    return out


def _cluster_modularity(G: nx.Graph, community: list[str]) -> float:
    """Modularity contribution of one community vs the rest of G."""
    if len(community) < 2:
        return 0.0
    rest = [n for n in G.nodes if n not in community]
    try:
        return modularity(G, [set(community), set(rest)] if rest else [set(community)])
    except Exception:
        return 0.0
