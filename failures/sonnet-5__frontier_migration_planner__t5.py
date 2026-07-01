# reason: token_budget_exhausted:max_tokens

import itertools


def plan_migration(current: dict, target: dict, constraints=None) -> list:
    """
    Compute a deterministic, ordered list of migration operations to move a
    database schema from `current` to `target`.
    """
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = current.get("tables", {}) or {}
    target_tables = target.get("tables", {}) or {}

    _counter = itertools.count(1)

    def make_op(op_type, table, risk, rollback, **extra):
        op = {
            "id": f"{op_type}_{next(_counter):04d}",
            "type": op_type,
            "table": table,
            "risk": risk,
            "rollback": rollback,
        }
        if extra:
            op.update(extra)
        return op

    def fk_key(fk):
        return (
            tuple(fk.get("columns", [])),
            fk.get("ref_table"),
            tuple(fk.get("ref_columns", [])),
        )

    def idx_columns(idx):
        if isinstance(idx, str):
            return [idx]
        if "columns" in idx:
            return list(idx["columns"])
        if "column" in idx:
            return [idx["column"]]
        return []

    def idx_key(idx):
        if isinstance(idx, str):
            return (tuple([idx]), False)
        return (tuple(idx_columns(idx)), bool(idx.get("unique", False)))

    def topo_sort(tables, table_defs):
        graph = {t: set() for t in tables}
        table_set = set(tables)
        for t in tables:
            for fk in table_defs.get(t, {}).get("foreign_keys", []) or []:
                ref = fk.get("ref_table")
                if ref in table_set and ref != t:
                    graph[t].add(ref)
        result = []
        remaining = set(tables)
        result_set = set()
        while remaining:
            ready = sorted(t for t in remaining if graph[t] <= result_set)
            if not ready:
                # break potential cycle deterministically
                ready = sorted(remaining)
            for t in ready:
                result.append(t)
                result_set.add(t)
                remaining.discard(t)
        return result

    def resolve_drop(kind, table, col=None):
        if kind == "table":
            obj_def = current_tables.get(table, {}) or {}
        else:
            obj_def = (
                current_tables.get(table, {}).get("columns", {}).get(col, {}) or {}
            )
        defer_flag = obj_def.get("defer_drop", True)
        if zero_downtime and defer_flag is not False:
            return "medium", True
        return "high", False

    ops = []

    new_tables = sorted(t for t in target_tables if t not in current_tables)
    dropped_tables = sorted(t for t in current_tables if t not in target_tables)
    existing_tables = sorted(t for t in target_tables if t in current_tables)

    # ---- Phase 1: add new tables (parents before children) ----
    ordered_new_tables = topo_sort(new_tables, target_tables)
    for t in ordered_new_tables:
        tdef = target_tables[t]
        ops.append(
            make_op(
                "add_table",
                t,
                "low",
                f"DROP TABLE {t}",
                columns=sorted((tdef.get("columns", {}) or {}).keys()),
                primary_key=tdef.get("primary_key", []),
            )
        )

    # ---- Phase 2: column level changes for existing tables ----
    drop_column_ops = []  # (table, column) to be dropped later

    for t in existing_tables:
        cur_cols = current_tables[t].get("columns", {}) or {}
        tgt_cols = target_tables[t].get("columns", {}) or {}

        renamed_pairs = sorted(
            [
                (cdef.get("renamed_from"), cname)
                for cname, cdef in tgt_cols.items()
                if cdef.get("renamed_from") and cdef.get("renamed_from") in cur_cols
            ],
            key=lambda p: p[1],
        )
        renamed_old = {rf for rf, _ in renamed_pairs}
        renamed_new = {nn for _, nn in renamed_pairs}

        # -- renames: add new column, backfill from old, tighten nullability --
        for old_name, new_name in renamed_pairs:
            cdef = tgt_cols[new_name]
            nullable_target = cdef.get("nullable", True)
            default = cdef.get("default")
            ops.append(
                make_op(
                    "add_column",
                    t,
                    "low",
                    f"ALTER TABLE {t} DROP COLUMN {new_name}",
                    column=new_name,
                    nullable=True,
                    default=default,
                    data_type=cdef.get("type"),
                )
            )
            ops.append(
                make_op(
                    "backfill_column",
                    t,
                    "medium",
                    f"-- no automatic rollback for backfill of {new_name}",
                    column=new_name,
                    source_column=old_name,
                    note=f"copy data from {old_name} to {new_name}",
                )
            )
            if not nullable_target:
                ops.append(
                    make_op(
                        "alter_nullable",
                        t,
                        "medium",
                        f"ALTER TABLE {t} ALTER COLUMN {new_name} DROP NOT NULL",
                        column=new_name,
                        nullable=False,
                    )
                )
            drop_column_ops.append((t, old_name))

        # -- genuinely new columns --
        added_cols = sorted(
            c for c in tgt_cols if c not in cur_cols and c not in renamed_new
        )
        for c in added_cols:
            cdef = tgt_cols[c]
            nullable = cdef.get("nullable", True)
            default = cdef.get("default")
            if not nullable and default is None:
                ops.append(
                    make_op(
                        "add_column",
                        t,
                        "low",
                        f"ALTER TABLE {t} DROP COLUMN {c}",
                        column=c,
                        nullable=True,
                        default=default,
                        data_type=cdef.get("type"),
                    )
                )
                ops.append(
                    make_op(
                        "backfill_column",
                        t,
                        "medium",
                        f"-- no automatic rollback for backfill of {c}",
                        column=c,
                        note=f"populate backfill value for {c}",
                    )
                )
                ops.append(
                    make_op(
                        "alter_nullable",
                        t,
                        "medium",
                        f"ALTER TABLE {t} ALTER COLUMN {c} DROP NOT NULL",
                        column=c,
                        nullable=False,
                    )
                )
            else:
                ops.append(
                    make_op(
                        "add_column",
                        t,
                        "low",
                        f"ALTER TABLE {
