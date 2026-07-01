# reason: TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

def plan_migration(current: dict, target: dict, constraints: dict | None = None) -> list[dict]:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    cur_tables = current.get("tables", {}) or {}
    tgt_tables = target.get("tables", {}) or {}

    new_table_names = set(tgt_tables) - set(cur_tables)
    dropped_table_names = sorted(set(cur_tables) - set(tgt_tables))
    common_table_names = sorted(set(cur_tables) & set(tgt_tables))

    def topo_sort(names, tables):
        result = []
        visited = set()
        names_set = set(names)

        def visit(n):
            if n in visited:
                return
            visited.add(n)
            fks = tables[n].get("foreign_keys", []) or []
            deps = sorted({
                fk["ref_table"] for fk in fks
                if fk.get("ref_table") in names_set and fk.get("ref_table") != n
            })
            for d in deps:
                visit(d)
            result.append(n)

        for n in sorted(names):
            visit(n)
        return result

    sorted_new = topo_sort(new_table_names, tgt_tables)

    def fk_key(fk):
        return (
            fk.get("name") or "",
            tuple(fk.get("columns", [])),
            fk.get("ref_table") or "",
            tuple(fk.get("ref_columns", [])),
        )

    def fk_name(t, fk):
        return fk.get("name") or f"fk_{t}_{'_'.join(fk.get('columns', []))}"

    def idx_key(idx):
        return (idx.get("name") or "", tuple(idx.get("columns", [])))

    def idx_name(t, idx):
        return idx.get("name") or f"idx_{t}_{'_'.join(idx.get('columns', []))}"

    add_table_ops = []
    add_column_ops = []
    backfill_ops = []
    alter_nullable_ops = []
    add_index_ops = []
    add_fk_ops = []
    validate_fk_ops = []
    drop_ops = []

    # Phase A: add new tables (parents first)
    for t in sorted_new:
        add_table_ops.append({
            "id": f"add_table:{t}",
            "type": "add_table",
            "table": t,
            "risk": "low",
            "rollback": f"drop_table {t}",
        })

    # Phase B: column additions on existing tables
    for t in common_table_names:
        cur_cols = cur_tables[t].get("columns", {}) or {}
        tgt_cols = tgt_tables[t].get("columns", {}) or {}

        for col_name in sorted(set(tgt_cols) - set(cur_cols)):
            col_def = tgt_cols[col_name] or {}
            if col_def.get("renamed_from"):
                continue  # rename handled elsewhere; not in required op list
            nullable = col_def.get("nullable", True)
            default = col_def.get("default", None)

            if not nullable and default is None:
                add_column_ops.append({
                    "id": f"add_column:{t}.{col_name}",
                    "type": "add_column",
                    "table": t,
                    "column": col_name,
                    "nullable": True,
                    "risk": "low",
                    "rollback": f"drop_column {t}.{col_name}",
                })
                backfill_ops.append({
                    "id": f"backfill_column:{t}.{col_name}",
                    "type": "backfill_column",
                    "table": t,
                    "column": col_name,
                    "risk": "medium",
                    "rollback": "no-op (data backfill)",
                })
                alter_nullable_ops.append({
                    "id": f"alter_nullable:{t}.{col_name}",
                    "type": "alter_nullable",
                    "table": t,
                    "column": col_name,
                    "nullable": False,
                    "risk": "medium",
                    "rollback": f"alter_nullable {t}.{col_name} -> true",
                })
            else:
                add_column_ops.append({
                    "id": f"add_column:{t}.{col_name}",
                    "type": "add_column",
                    "table": t,
                    "column": col_name,
                    "nullable": bool(nullable),
                    "risk": "low",
                    "rollback": f"drop_column {t}.{col_name}",
                })

    # Phase C: indexes (new tables first, then common tables)
    for t in sorted_new:
        indexes = tgt_tables[t].get("indexes", []) or []
        for idx in sorted(indexes, key=idx_key):
            name = idx_name(t, idx)
            add_index_ops.append({
                "id": f"add_index:{t}.{name}",
                "type": "add_index",
                "table": t,
                "index": name,
                "columns": list(idx.get("columns", [])),
                "risk": "low",
                "rollback": f"drop_index {name}",
            })

    for t in common_table_names:
        cur_idx = cur_tables[t].get("indexes", []) or []
        tgt_idx = tgt_tables[t].get("indexes", []) or []
        cur_keys = {idx_key(i) for i in cur_idx}
        cur_names = {i.get("name") for i in cur_idx if i.get("name")}
        for idx in sorted(tgt_idx, key=idx_key):
            if idx_key(idx) in cur_keys:
                continue
            if idx.get("name") and idx.get("name") in cur_names:
                continue
            name = idx_name(t, idx)
            add_index_ops.append({
                "id": f"add_index:{t}.{name}",
                "type": "add_index",
                "table": t,
                "index": name,
                "columns": list(idx.get("columns", [])),
                "risk": "low",
                "rollback": f"drop_index {name}",
            })

    # Phase D: foreign keys (add then validate)
    fk_entries = []
    for t in sorted_new:
        fks = tgt_tables[t].get("foreign_keys", []) or []
        for fk in sorted(fks, key=fk_key):
            fk_entries.append((t, fk))
    for t in common_table_names:
        cur_fks = cur_tables[t].get("foreign_keys", []) or []
        tgt_fks = tgt_tables[t].get("foreign_keys", []) or []
        cur_fk_keys = {fk_key(fk) for fk in cur_fks}
        for fk in sorted(tgt_fks, key=fk_key):
            if fk_key(fk) in cur_fk_keys:
                continue
            fk_entries.append((t, fk))

    for t, fk in fk_entries:
        name = fk_name(t, fk)
        add_fk_ops.append({
            "id": f"add_foreign_key:{t}.{name}",
            "type": "add_foreign_key",
            "table": t,
            "name": name,
            "columns": list(fk.get("columns", [])),
            "ref_table": fk.get("ref_table"),
            "ref_columns": list(fk.get("ref_columns", [])),
            "risk": "medium",
            "rollback": f"drop_foreign_key {t}.{name}",
        })
        validate_fk_ops.append({
            "id": f"validate_foreign_key:{t}.{name}",
            "type": "validate_foreign_key",
            "table": t,
            "name": name,
            "risk": "low",
            "rollback": "no-op (validation only)",
        })

    # Phase E: drops
    for t in common_table_names:
        cur_cols = cur_tables[t].get("columns", {}) or {}
        tgt_cols = tgt_tables[t].get("columns", {}) or {}
        renamed_sources = {
            (col_def or {}).get("renamed_from")
            for col_def in tgt_cols.values()
            if (col_def or {}).get("renamed_from")
        }

        for col_name in sorted(set(cur_cols) - set(tgt_cols)):
            if col_name in renamed_sources:
                continue
            cur_col = cur_cols.get(col_name, {}) or {}
            defer_drop = cur_col.get("defer_drop", True)
            if zero_downtime and defer_drop is not False:
                drop_ops.append({
                    "id": f"drop_column:{t}.{col_name}",
                    "type": "drop_column",
                    "table": t,
                    "column": col_name,
                    "risk": "medium",
                    "deferred": True,
                    "rollback": f"re-add column {t}.{col_name}",
                })
            else:
                drop_ops.append({
                    "id": f"drop_column:{t}.{col_name}",
                    "type": "drop_column",
                    "table": t,
                    "column": col_name,
                    "risk": "high",
                    "deferred": False,
                    "rollback": f"re-add column {t}.{col_name}",
                })

    for t in dropped_table_names:
        cur_table = cur_tables.get(t, {}) or {}
        defer_drop = cur_table.get("defer_drop", True)
        if zero_downtime and defer_drop is not False:
            drop_ops.append({
                "id": f"drop_table:{t}",
                "type": "drop_table",
                "table": t,
                "risk": "medium",
                "deferred": True,
                "rollback": f"recreate table {t}",
            })
        else:
            drop_ops.append({
                "id": f"drop_table:{t}",
                "type": "drop_table",
                "table": t,
                "risk": "high",
                "deferred": False,
                "rollback": f"recreate table {t}",
            })

    return (
        add_table_ops
        + add_column_ops
        + backfill_ops
        + alter_nullable_ops
        + add_index_ops
        + add_fk_ops
        + validate_fk_ops
        + drop_ops
    )
