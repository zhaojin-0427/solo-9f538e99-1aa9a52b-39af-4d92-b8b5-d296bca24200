"""Lineage registration and recursive impact analysis."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .exceptions import (
    ConflictLineageError,
    CycleDetectedError,
    NotFoundLineageError,
    UndoConflictError,
    ValidationLineageError,
)

# Graph traversal may be deep for large lineage graphs.
_MAX_RECURSION_DEPTH = max(10_000, sys.getrecursionlimit())


# --------------------------------------------------------------------------- #
# Graph loading
# --------------------------------------------------------------------------- #

def load_graph(conn: sqlite3.Connection) -> tuple[
    dict[str, str], dict[str, list[list[str]]]
]:
    """Load every node and its candidate input groups from one snapshot."""
    kinds: dict[str, str] = {
        row["name"]: row["kind"] for row in conn.execute("SELECT name, kind FROM nodes")
    }
    groups: dict[str, list[list[str]]] = defaultdict(list)

    group_rows = conn.execute(
        "SELECT id, node_name FROM candidate_groups ORDER BY node_name, position"
    ).fetchall()
    id_to_node = {row["id"]: row["node_name"] for row in group_rows}

    if id_to_node:
        placeholders = ",".join("?" for _ in id_to_node)
        input_rows = conn.execute(
            f"SELECT group_id, input_name FROM group_inputs "
            f"WHERE group_id IN ({placeholders}) ORDER BY group_id, position",
            tuple(id_to_node.keys()),
        ).fetchall()
        inputs_by_group: dict[int, list[str]] = defaultdict(list)
        for row in input_rows:
            inputs_by_group[row["group_id"]].append(row["input_name"])
        for gid, node_name in id_to_node.items():
            groups[node_name].append(inputs_by_group.get(gid, []))

    return kinds, dict(groups)


def adjacency(
    groups: dict[str, list[list[str]]]
) -> dict[str, list[str]]:
    """Flatten candidate groups into node -> unique downstream inputs."""
    edges: dict[str, list[str]] = {}
    for node, node_groups in groups.items():
        seen: set[str] = set()
        ordered: list[str] = []
        for group in node_groups:
            for inp in group:
                if inp not in seen:
                    seen.add(inp)
                    ordered.append(inp)
        edges[node] = ordered
    return edges


# --------------------------------------------------------------------------- #
# Cycle detection (whole graph, DFS coloring)
# --------------------------------------------------------------------------- #

def find_cycle(
    kinds: dict[str, str], groups: dict[str, list[list[str]]]
) -> list[str] | None:
    """Return one cycle path (node repeated at both ends), or None."""
    edges = adjacency(groups)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in kinds}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)
    for name in sorted(kinds):
        if color[name] == WHITE:
            cycle = visit(name)
            if cycle is not None:
                return cycle
    return None


# --------------------------------------------------------------------------- #
# Batch registration (all-or-nothing)
# --------------------------------------------------------------------------- #

def _replace_groups(
    conn: sqlite3.Connection, node_name: str, groups: list[list[str]]
) -> None:
    """Rewrite one node's candidate groups from scratch."""
    conn.execute(
        "DELETE FROM group_inputs WHERE group_id IN "
        "(SELECT id FROM candidate_groups WHERE node_name = ?)",
        (node_name,),
    )
    conn.execute(
        "DELETE FROM candidate_groups WHERE node_name = ?", (node_name,)
    )
    for pos, group in enumerate(groups):
        cur = conn.execute(
            "INSERT INTO candidate_groups(node_name, position) VALUES(?, ?)",
            (node_name, pos),
        )
        gid = cur.lastrowid
        for inp_pos, inp in enumerate(group):
            conn.execute(
                "INSERT INTO group_inputs(group_id, input_name, position) "
                "VALUES(?, ?, ?)",
                (gid, inp, inp_pos),
            )


def register_batch(
    payload_nodes: list, db_path: str | None = None
) -> dict:
    """Validate and upsert a batch; any failure leaves the graph untouched."""
    # ---- pure payload validation (no lock held yet) ----
    seen_names: set[str] = set()
    duplicates: list[str] = []
    sources_with_inputs: list[str] = []
    empty_group_nodes: list[str] = []
    intra_group_dupes: dict[str, list[list[int]]] = {}

    for node in payload_nodes:
        if node.name in seen_names:
            duplicates.append(node.name)
        seen_names.add(node.name)

        if node.kind == "source" and node.candidate_groups:
            sources_with_inputs.append(node.name)
        if node.kind != "source" and not node.candidate_groups:
            empty_group_nodes.append(node.name)
        for i, group in enumerate(node.candidate_groups):
            if not group:
                empty_group_nodes.append(node.name)
            local_dupes = sorted(
                {inp for inp in group if group.count(inp) > 1}
            )
            if local_dupes:
                intra_group_dupes.setdefault(node.name, []).append(
                    [i, *local_dupes]
                )

    problems: dict[str, list] = {}
    if duplicates:
        problems["duplicate_names"] = sorted(set(duplicates))
    if sources_with_inputs:
        problems["source_nodes_with_inputs"] = sorted(set(sources_with_inputs))
    if empty_group_nodes:
        problems["missing_candidate_groups"] = sorted(set(empty_group_nodes))
    if intra_group_dupes:
        problems["duplicate_inputs_within_group"] = intra_group_dupes
    if problems:
        raise ValidationLineageError(
            "请求数据不合法，整批未登记", problems
        )

    with db.immediate_write(db_path) as conn:
        existing_kinds: dict[str, str] = {
            row["name"]: row["kind"]
            for row in conn.execute("SELECT name, kind FROM nodes")
        }

        # ---- node kind conflicts ----
        conflicts = sorted(
            node.name
            for node in payload_nodes
            if node.name in existing_kinds
            and existing_kinds[node.name] != node.kind
        )
        if conflicts:
            raise ConflictLineageError(
                "节点已存在且类型不同，整批未生效",
                {"kind_conflicts": conflicts},
            )

        # ---- referential integrity against post-batch node set ----
        final_nodes = dict(existing_kinds)
        for node in payload_nodes:
            final_nodes[node.name] = node.kind
        unknown: dict[str, list[str]] = {}
        for node in payload_nodes:
            missing = sorted(
                {
                    inp
                    for group in node.candidate_groups
                    for inp in group
                    if inp not in final_nodes
                }
            )
            if missing:
                unknown[node.name] = missing
        if unknown:
            raise ValidationLineageError(
                "候选输入引用了未登记的字段，整批未生效",
                {"unknown_inputs": unknown},
            )

        # ---- record pre-change definitions before writing anything ----
        # Only fields covered by this batch are recorded; unrelated fields are
        # untouched by the undo.
        old_snapshot: dict[str, dict] = {}
        for node in payload_nodes:
            old_groups = conn.execute(
                "SELECT id FROM candidate_groups WHERE node_name = ?",
                (node.name,),
            ).fetchall()
            groups: list[list[str]] = []
            for row in old_groups:
                inputs = conn.execute(
                    "SELECT input_name FROM group_inputs WHERE group_id = ? "
                    "ORDER BY position",
                    (row["id"],),
                ).fetchall()
                groups.append([r["input_name"] for r in inputs])
            old_snapshot[node.name] = {
                "existed": node.name in existing_kinds,
                "kind": existing_kinds.get(node.name),
                "groups": groups,
            }

        # ---- upsert phase 1: all nodes must exist before any edge is written ----
        for node in payload_nodes:
            conn.execute(
                "INSERT INTO nodes(name, kind) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind",
                (node.name, node.kind),
            )

        # ---- upsert phase 2: replace candidate groups and their inputs ----
        for node in payload_nodes:
            # Replace all previously registered groups of this node.
            _replace_groups(conn, node.name, node.candidate_groups)

        # ---- whole-graph cycle check; roll back everything on a cycle ----
        kinds, groups = load_graph(conn)
        cycle = find_cycle(kinds, groups)
        if cycle is not None:
            raise CycleDetectedError(
                "登记将引入循环依赖，整批不生效",
                {"cycle": cycle},
            )

        # ---- persist the undo record in the same transaction ----
        cur = conn.execute(
            "INSERT INTO batches(created_at, node_count) VALUES(?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                len(payload_nodes),
            ),
        )
        batch_id = cur.lastrowid
        for node in payload_nodes:
            old = old_snapshot[node.name]
            conn.execute(
                "INSERT INTO batch_nodes(batch_id, node_name, existed, "
                "old_kind, old_groups, new_kind, new_groups) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    node.name,
                    int(old["existed"]),
                    old["kind"],
                    json.dumps(old["groups"], ensure_ascii=False),
                    node.kind,
                    json.dumps(node.candidate_groups, ensure_ascii=False),
                ),
            )

    return {
        "batch_id": batch_id,
        "registered": len(payload_nodes),
        "nodes": sorted(node.name for node in payload_nodes),
        "message": "登记成功（已提交完整血缘快照，可按 batch_id 撤销）",
    }


# --------------------------------------------------------------------------- #
# Batch undo (all-or-nothing, guarded against later dependent batches)
# --------------------------------------------------------------------------- #

def undo_batch(batch_id: int, db_path: str | None = None) -> dict:
    """Restore every field covered by ``batch_id`` to its pre-batch definition.

    * Fields newly introduced by the batch are deleted (unless a later batch
      still references them).
    * Fields overwritten by the batch get their old kind / candidate groups.
    * Fields outside the batch are never touched.
    * If any covered field was redefined by a later batch, or a later batch
      (still present) references a field this batch introduced, the undo is
      rejected and the lineage is left unchanged.
    * Repeating an undo replays the first successful result.
    """
    with db.immediate_write(db_path) as conn:
        batch = conn.execute(
            "SELECT id, undone, undo_result FROM batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise NotFoundLineageError(
                "登记批次不存在", {"batch_id": batch_id}
            )
        if batch["undone"]:
            # Idempotent: replay the exact first result.
            return json.loads(batch["undo_result"])

        rows = conn.execute(
            "SELECT node_name, existed, old_kind, old_groups, "
            "new_kind, new_groups FROM batch_nodes "
            "WHERE batch_id = ? ORDER BY node_name",
            (batch_id,),
        ).fetchall()

        kinds, groups = load_graph(conn)

        # ---- guard 1: covered fields must still have this batch's definition ----
        modified = sorted(
            row["node_name"]
            for row in rows
            if kinds.get(row["node_name"]) != row["new_kind"]
            or groups.get(row["node_name"], [])
            != json.loads(row["new_groups"])
        )
        if modified:
            raise UndoConflictError(
                "字段定义已被后续批次修改，无法撤销该批次",
                {"batch_id": batch_id, "modified_since_batch": modified},
            )

        # ---- guard 2: no surviving later reference to a newly added field ----
        covered = {row["node_name"] for row in rows}
        added = {
            row["node_name"] for row in rows if not row["existed"]
        }
        if added:
            referencing = sorted(
                name
                for name, node_groups in groups.items()
                if name not in covered
                and any(inp in added for g in node_groups for inp in g)
            )
            if referencing:
                raise UndoConflictError(
                    "该批新增字段仍被后续登记引用，无法撤销该批次",
                    {
                        "batch_id": batch_id,
                        "added_fields": sorted(added),
                        "referencing_nodes": referencing,
                    },
                )

        # ---- apply the inverse, atomically ----
        # Restore first: this drops every reference made by covered fields to
        # nodes that the batch introduced, so the later deletes are safe.
        restored: list[str] = []
        deleted: list[str] = []
        for row in rows:
            if not row["existed"]:
                continue
            name = row["node_name"]
            conn.execute(
                "INSERT INTO nodes(name, kind) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind",
                (name, row["old_kind"]),
            )
            _replace_groups(conn, name, json.loads(row["old_groups"]))
            restored.append(name)

        for row in rows:
            if row["existed"]:
                continue
            # Clear the deleted node's own outgoing edges first so pairs of
            # batch-added nodes never block each other's removal.
            _replace_groups(conn, row["node_name"], [])

        for row in rows:
            if row["existed"]:
                continue
            name = row["node_name"]
            # Cascades remove any remaining inputs that still point here.
            conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
            deleted.append(name)

        # Defensive invariant: an undo must never leave a cyclic graph.
        kinds, groups = load_graph(conn)
        cycle = find_cycle(kinds, groups)
        if cycle is not None:  # pragma: no cover - should be unreachable
            raise CycleDetectedError(
                "撤销将引入循环依赖，整批不生效",
                {"cycle": cycle},
            )

        result = {
            "batch_id": batch_id,
            "undone": True,
            "restored": sorted(restored),
            "deleted": sorted(deleted),
            "message": "批次已撤销，字段恢复为登记前定义",
        }
        conn.execute(
            "UPDATE batches SET undone = 1, undo_result = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), batch_id),
        )
        return result


# --------------------------------------------------------------------------- #
# Recursive impact analysis
# --------------------------------------------------------------------------- #

def analyze_impact(
    retiring: list[str], db_path: str | None = None
) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)

    unknown = sorted(name for name in retiring if name not in kinds)
    if unknown:
        raise NotFoundLineageError(
            "存在未登记的字段名", {"unknown": unknown}
        )

    retiring_set = set(retiring)
    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)

    # availability memo: True if at least one candidate group is fully usable.
    available: dict[str, bool] = {}
    # For every unavailable node, the deepest retired causes behind it.
    root_causes: dict[str, list[str]] = {}

    def is_available(node: str) -> bool:
        if node in available:
            return available[node]
        if node in retiring_set:
            available[node] = False
            root_causes[node] = [node]
            return False

        node_groups = groups.get(node, [])
        if not node_groups:  # source node not retired
            available[node] = True
            return True

        group_ok = any(
            all(is_available(inp) for inp in group) for group in node_groups
        )
        available[node] = group_ok
        if not group_ok:
            causes: set[str] = set()
            for group in node_groups:
                for inp in group:
                    if not is_available(inp):
                        causes.update(root_causes.get(inp, [inp]))
            root_causes[node] = sorted(causes)
        return group_ok

    for name in kinds:
        is_available(name)

    unavailable = sorted(name for name, ok in available.items() if not ok)

    failed_columns = []
    for name in sorted(kinds):
        if kinds[name] != "report" or available[name]:
            continue
        failed_groups = []
        for idx, group in enumerate(groups.get(name, [])):
            missing = [
                inp for inp in group if not available[inp]
            ]
            if not missing:
                continue
            failed_groups.append(
                {
                    "group_index": idx,
                    "inputs": list(group),
                    "missing": [
                        {
                            "name": inp,
                            "kind": kinds[inp],
                            "root_causes": root_causes.get(inp, [inp]),
                        }
                        for inp in missing
                    ],
                }
            )
        failed_columns.append(
            {"name": name, "failed_groups": failed_groups}
        )

    return {
        "retiring": sorted(retiring_set),
        "failed_report_columns": failed_columns,
        "unavailable_count": len(unavailable),
        "unavailable_nodes": unavailable,
    }


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #

def get_graph(db_path: str | None = None) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)
    nodes = [
        {
            "name": name,
            "kind": kinds[name],
            "candidate_groups": groups.get(name, []),
        }
        for name in sorted(kinds)
    ]
    counts: dict[str, int] = {"source": 0, "derived": 0, "report": 0}
    for kind in kinds.values():
        counts[kind] += 1
    return {"nodes": nodes, "counts": counts}
