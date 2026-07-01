# reason: token_budget_exhausted:max_tokens

def plan_migration(current: dict, target: dict, constraints=None) -> list:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = current.get("tables", {}) or {}
    target_tables = target.get("tables", {}) or {}

    ops = []

    def make_id(op_type, table, *parts):
        suffix = "_".join(str(p) for p in parts if p not in (None, ""))
        return f"{op_type}:{table}" + (f":{suffix}" if suffix else "")

    def idx_key(idx):
        name = idx.get("name")
        if name:
            return ("name", name)
        return ("cols", tuple(idx.get("columns", [])))

    def idx_name(idx):
        return idx.get("name") or "_".join(idx.get("columns", []))

    def fk_key(fk):
        name = fk.get("name")
        if name:
            return ("name", name)
        return ("cols", tuple(fk.get("columns", [])), fk.get("ref_table"),
                 tuple(fk.get("ref_columns", [])))

    def fk_name(fk):
        return fk.get("name") or f"fk_{'_'.join(fk.get('columns', []))}_{fk.get('ref_table')}"

    def topo_sort(names, tables_dict):
        names_set = set(names)
        deps = {}
        for t in names:
            d = set()
            for fk in (tables_dict.get(t, {}).get("foreign_keys", []) or []):
                ref = fk.get("ref_table")
                if ref in names_set and ref != t:
                    d.add(ref)
            deps[t] = d
        result = []
        remaining = set(names)
        while remaining:
            ready = sorted(t for t in remaining if not (deps[t] & remaining))
            if not ready:
                ready = sorted(remaining)
            chosen = ready[0]
            result.append(chosen)
            remaining.discard(chosen)
        return result

    def drop_risk(obj):
        risk = "high"
        deferred = False
        if zero_downtime:
            defer_drop = obj.get("defer_drop", True)
            if defer_drop:
                risk = "medium"
                deferred = True
        return risk, deferred

    new_table_names = sorted(set(target_tables) - set(current_tables))
    dropped_table_names = sorted(set(current_tables) - set(target_tables))
    common_table_names = sorted(set(current_tables) & set(target_tables))

    new_table_order = topo_sort(new_table_names, target_tables)

    # Phase 1: add_table (parents before children)
    for t in new_table_order:
        tdef = target_tables[t]
        ops.append({
            "id": make_id("add_table", t),
            "type": "add_table",
            "table": t,
            "risk": "low",
            "rollback": f"DROP TABLE {t}",
            "columns": tdef.get("columns", {}),
            "primary_key": tdef.get("primary_key", []),
        })

    columns_to_drop = []  # (table, column_name)

    def process_columns(t, current_cols, target_cols):
        consumed_current = set()
        for cname in sorted(target_cols.keys()):
            col = target_cols[cname]
            if cname in current_cols:
                cur_col = current_cols[cname]
                cur_nullable = cur_col.get("nullable", True)
                tgt_nullable = col.get("nullable", True)
                if cur_nullable and not tgt_nullable:
                    ops.append({
                        "id": make_id("backfill_column", t, cname),
                        "type": "backfill_column",
                        "table": t,
                        "column": cname,
                        "risk": "medium",
                        "rollback": "no-op (backfill not reversible)",
                    })
                    ops.append({
                        "id": make_id("alter_nullable", t, cname),
                        "type": "alter_nullable",
                        "table": t,
                        "column": cname,
                        "nullable": False,
                        "risk": "medium",
                        "rollback": f"ALTER TABLE {t} ALTER COLUMN {cname} DROP NOT NULL",
                    })
                elif (not cur_nullable) and tgt_nullable:
                    ops.append({
                        "id": make_id("alter_nullable", t, cname),
                        "type": "alter_nullable",
                        "table": t,
                        "column": cname,
                        "nullable": True,
                        "risk": "low",
                        "rollback": f"ALTER TABLE {t} ALTER COLUMN {cname} SET NOT NULL",
                    })
                continue

            renamed_from = col.get("renamed_from")
            if renamed_from and renamed_from in current_cols:
                consumed_current.add(renamed_from)
                ops.append({
                    "id": make_id("add_column", t, cname),
                    "type": "add_column",
                    "table": t,
                    "column": cname,
                    "column_type": col.get("type"),
                    "nullable": True,
                    "default": col.get("default"),
                    "risk": "low",
                    "rollback": f"ALTER TABLE {t} DROP COLUMN {cname}",
                })
                ops.append({
                    "id": make_id("backfill_column", t, cname),
                    "type": "backfill_column",
                    "table": t,
                    "column": cname,
                    "source_column": renamed_from,
                    "risk": "medium",
                    "rollback": "no-op (backfill not reversible)",
                })
                if not col.get("nullable", True):
                    ops.append({
                        "id": make_id("alter_nullable", t, cname),
                        "type": "alter_nullable",
                        "table": t,
                        "column": cname,
                        "nullable": False,
                        "risk": "medium",
                        "rollback": f"ALTER TABLE {t} ALTER COLUMN {cname} DROP NOT NULL",
                    })
                columns_to_drop.append((t, renamed_from))
            else:
                nullable = col.get("nullable", True)
                default = col.get("default")
                if (not nullable) and default is None:
                    ops.append({
                        "id": make_id("add_column", t, cname),
                        "type": "add_column",
                        "table": t,
                        "column": cname,
                        "column_type": col.get("type"),
                        "nullable": True,
                        "default": None,
                        "risk": "low",
                        "rollback": f"ALTER TABLE {t} DROP COLUMN {cname}",
                    })
                    ops.append({
                        "id": make_id("backfill_column", t, cname),
                        "type": "backfill_column",
                        "table": t,
                        "column": cname,
                        "risk": "medium",
                        "rollback": "no-op (backfill not reversible)",
                    })
                    ops.append({
                        "id": make_id("alter_nullable", t, cname),
                        "type": "alter_nullable",
                        "table": t,
                        "column": cname,
                        "nullable": False,
                        "risk": "medium",
                        "rollback": f"ALTER TABLE {t} ALTER COLUMN {cname} DROP NOT NULL",
                    })
                else:
                    ops.append({
                        "id": make_id("add_column", t, cname),
                        "type": "add_column",
                        "table": t,
                        "column": cname,
                        "column_type": col.get("type"),
                        "nullable": nullable,
                        "default": default,
                        "risk": "low",
                        "rollback": f"ALTER TABLE {t} DROP COLUMN {cname}",
