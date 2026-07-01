# reason: token_budget_exhausted:max_tokens

def plan_migration(current: dict, target: dict, constraints=None) -> list:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = (current or {}).get("tables", {}) or {}
    target_tables = (target or {}).get("tables", {}) or {}

    ops = []

    def add_op(type_, table, risk, rollback, **extra):
        op = {
            "id": f"op_{len(ops) + 1:04d}",
            "type": type_,
            "table": table,
            "risk": risk,
            "rollback": rollback,
        }
        op.update(extra)
        ops.append(op)
        return op

    def index_key(idx):
        if idx.get("name"):
            return ("name", idx["name"])
        return ("cols", tuple(idx.get("columns", [])), bool(idx.get("unique", False)))

    def fk_key(fk):
        return (
            tuple(fk.get("columns", [])),
            fk.get("ref_table"),
            tuple(fk.get("ref_columns", [])),
            fk.get("name"),
        )

    def topo_order(tables_list, tables_def):
        tables_set = set(tables_list)
        deps = {t: set() for t in tables_list}
        for t in tables_list:
            fks = tables_def.get(t, {}).get("foreign_keys", []) or []
            for fk in fks:
                ref = fk.get("ref_table")
                if ref in tables_set and ref != t:
                    deps[t].add(ref)

        result = []
        result_set = set()
        remaining = set(tables_list)
        while remaining:
            ready = sorted(t for t in remaining if deps[t] <= result_set)
            if not ready:
                # break a cycle deterministically
                ready = [sorted(remaining)[0]]
            for t in ready:
                if t in remaining:
                    result.append(t)
                    result_set.add(t)
                    remaining.discard(t)
        return result

    new_tables = sorted(t for t in target_tables if t not in current_tables)
    dropped_tables = sorted(t for t in current_tables if t not in target_tables)
    common_tables = sorted(t for t in target_tables if t in current_tables)

    new_tables_ordered = topo_order(new_tables, target_tables)
    all_tables_order = new_tables_ordered + common_tables

    # ---------- Phase A: add new tables ----------
    for t in new_tables_ordered:
        tdef = target_tables[t]
        add_op(
            "add_table",
            t,
            "low",
            f"DROP TABLE {t}",
            columns=tdef.get("columns", {}),
            primary_key=tdef.get("primary_key", []),
        )

    # ---------- Phase B: columns on existing tables ----------
    for t in common_tables:
        cur_cols = current_tables[t].get("columns", {}) or {}
        tgt_cols = target_tables[t].get("columns", {}) or {}

        new_col_names = sorted(c for c in tgt_cols if c not in cur_cols)
        for cname in new_col_names:
            cdef = tgt_cols[cname]
            renamed_from = cdef.get("renamed_from")
            is_rename = bool(renamed_from and renamed_from in cur_cols)
            target_nullable = cdef.get("nullable", True)
            target_default = cdef.get("default")
            needs_backfill = is_rename or (not target_nullable and target_default is None)

            if needs_backfill:
                add_op(
                    "add_column",
                    t,
                    "low",
                    f"ALTER TABLE {t} DROP COLUMN {cname}",
                    column=cname,
                    col_type=cdef.get("type"),
                    nullable=True,
                    default=target_default,
                )
                add_op(
                    "backfill_column",
                    t,
                    "medium",
                    "Backfilled data is not automatically reversible; restore from backup if needed.",
                    column=cname,
                    source_column=renamed_from if is_rename else None,
                )
                if not target_nullable:
                    add_op(
                        "alter_nullable",
                        t,
                        "medium",
                        f"ALTER TABLE {t} ALTER COLUMN {cname} DROP NOT NULL",
                        column=cname,
                        nullable=False,
                    )
            else:
                add_op(
                    "add_column",
                    t,
                    "low",
                    f"ALTER TABLE {t} DROP COLUMN {cname}",
                    column=cname,
                    col_type=cdef.get("type"),
                    nullable=target_nullable,
                    default=target_default,
                )

    # ---------- Phase C: indexes ----------
    for t in all_tables_order:
        tgt_indexes = target_tables[t].get("indexes", []) or []
        if t in new_tables:
            cur_keys = set()
        else:
            cur_indexes = current_tables[t].get("indexes", []) or []
            cur_keys = {index_key(idx) for idx in cur_indexes}

        to_add = [idx for idx in tgt_indexes if index_key(idx) not in cur_keys]
        to_add_sorted = sorted(to_add, key=index_key)

        for idx in to_add_sorted:
            idx_name = idx.get("name") or "_".join(idx.get("columns", []))
            add_op(
                "add_index",
                t,
                "low",
                f"DROP INDEX {idx_name} ON {t}",
                index=idx,
            )

    # ---------- Phase D: foreign keys ----------
    for t in all_tables_order:
        tgt_fks = target_tables[t].get("foreign_keys", []) or []
        if t in new_tables:
            cur_keys = set()
        else:
            cur_fks = current_tables[t].get("foreign_keys", []) or []
            cur_keys = {fk_key(fk) for fk in cur_fks}

        to_add = [fk for fk in tgt_fks if fk_key(fk) not in cur_keys]
        to_add_sorted = sorted(to_add, key=fk_key)

        for fk in to_add_sorted:
            fk_name = fk.get("name") or f"fk_{t}_{'_'.join(fk.get('columns', []))}"
            add_op(
                "add_foreign_key",
                t,
                "medium",
                f"ALTER TABLE {t} DROP CONSTRAINT {fk_name}",
                foreign_key=fk,
            )
            add_op(
                "validate_foreign_key",
                t,
                "medium",
                "No rollback needed; validation is non-destructive.",
                foreign_key=fk,
            )

    # ---------- Phase F: drop columns ----------
    for t in common_tables:
        cur_cols = current_tables[t].get("columns", {}) or {}
        tgt_cols = target_tables[t].get("columns", {}) or {}
        dropped_cols = sorted(c for c in cur_cols if c not in tgt_cols)

        for col in dropped_cols:
            col_def = cur_cols.get(col, {}) or {}
            defer_flag = col_def.get("defer_drop", True)
            deferred = zero_downtime and defer_flag is not False
            risk = "medium" if deferred else "high"
            rollback = (
                f"Cancel scheduled drop of column {col} on {t}"
                if deferred
                else f"Re
