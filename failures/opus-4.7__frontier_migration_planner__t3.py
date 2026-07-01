# reason: TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

def plan_migration(current: dict, target: dict, constraints: dict | None = None) -> list[dict]:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = current.get("tables", {}) or {}
    target_tables = target.get("tables", {}) or {}

    ops: list[dict] = []
    _counter = [0]

    def new_id() -> str:
        _counter[0] += 1
        return f"op_{_counter[0]:04d}"

    def fk_name(table: str, fk: dict) -> str:
        if fk.get("name"):
            return fk["name"]
        cols = "_".join(fk.get("columns", []) or [])
        return f"fk_{table}_{cols}"

    def fk_key(fk: dict) -> tuple:
        return (
            tuple(fk.get("columns", []) or []),
            fk.get("ref_table"),
            tuple(fk.get("ref_columns", []) or []),
        )

    added_tables = sorted(set(target_tables) - set(current_tables))
    dropped_tables = sorted(set(current_tables) - set(target_tables))
    common_tables = sorted(set(current_tables) & set(target_tables))

    # ----- Phase 1: add new tables (columns + PK only; FKs deferred) -----
    for t in added_tables:
        tdef = target_tables[t]
        ops.append({
            "id": new_id(),
            "type": "add_table",
            "table": t,
            "columns": dict(tdef.get("columns", {}) or {}),
            "primary_key": tdef.get("primary_key"),
            "risk": "low",
            "rollback": f"DROP TABLE {t}",
        })

    # ----- Phase 2: add columns / backfill / alter nullable on existing tables -----
    add_col_ops: list[dict] = []
    backfill_ops: list[dict] = []
    alter_nullable_ops: list[dict] = []
    drop_col_specs: list[tuple] = []  # (table, column, column_spec)

    for t in common_tables:
        cur_cols = current_tables[t].get("columns", {}) or {}
        tgt_cols = target_tables[t].get("columns", {}) or {}

        renamed_from = {
            c: spec.get("renamed_from")
            for c, spec in tgt_cols.items()
            if isinstance(spec, dict) and spec.get("renamed_from")
        }
        renamed_old = set(renamed_from.values())

        for c in sorted(set(tgt_cols) - set(cur_cols)):
            spec = tgt_cols[c] or {}
            # Skip renames (old name will not be dropped)
            if spec.get("renamed_from") and spec["renamed_from"] in cur_cols:
                continue
            nullable = spec.get("nullable", True)
            default = spec.get("default")
            col_type = spec.get("type")

            if not nullable and default is None:
                add_col_ops.append({
                    "id": new_id(),
                    "type": "add_column",
                    "table": t,
                    "column": c,
                    "column_type": col_type,
                    "nullable": True,
                    "default": None,
                    "risk": "low",
                    "rollback": f"ALTER TABLE {t} DROP COLUMN {c}",
                })
                backfill_ops.append({
                    "id": new_id(),
                    "type": "backfill_column",
                    "table": t,
                    "column": c,
                    "risk": "medium",
                    "rollback": f"-- backfill of {t}.{c} cannot be automatically rolled back",
                })
                alter_nullable_ops.append({
                    "id": new_id(),
                    "type": "alter_nullable",
                    "table": t,
                    "column": c,
                    "nullable": False,
                    "risk": "medium",
                    "rollback": f"ALTER TABLE {t} ALTER COLUMN {c} DROP NOT NULL",
                })
            else:
                add_col_ops.append({
                    "id": new_id(),
                    "type": "add_column",
                    "table": t,
                    "column": c,
                    "column_type": col_type,
                    "nullable": nullable,
                    "default": default,
                    "risk": "low",
                    "rollback": f"ALTER TABLE {t} DROP COLUMN {c}",
                })

        # Nullability changes on existing columns
        for c in sorted(set(cur_cols) & set(tgt_cols)):
            cur_spec = cur_cols[c] or {}
            tgt_spec = tgt_cols[c] or {}
            cur_nullable = cur_spec.get("nullable", True) if isinstance(cur_spec, dict) else True
            tgt_nullable = tgt_spec.get("nullable", True) if isinstance(tgt_spec, dict) else True
            if cur_nullable != tgt_nullable:
                alter_nullable_ops.append({
                    "id": new_id(),
                    "type": "alter_nullable",
                    "table": t,
                    "column": c,
                    "nullable": tgt_nullable,
                    "risk": "medium",
                    "rollback": (
                        f"ALTER TABLE {t} ALTER COLUMN {c} "
                        + ("DROP NOT NULL" if not cur_nullable else "SET NOT NULL")
                    ),
                })

        for c in sorted(set(cur_cols) - set(tgt_cols)):
            if c in renamed_old:
                continue
            drop_col_specs.append((t, c, cur_cols[c] if isinstance(cur_cols[c], dict) else {}))

    ops.extend(add_col_ops)
    ops.extend(backfill_ops)
    ops.extend(alter_nullable_ops)

    # ----- Phase 3: add indexes -----
    new_indexes: list[tuple] = []
    for t in sorted(target_tables):
        tgt_idx = target_tables[t].get("indexes", {}) or {}
        cur_idx = (current_tables.get(t, {}) or {}).get("indexes", {}) or {}
        for idx_name in sorted(set(tgt_idx) - set(cur_idx)):
            new_indexes.append((t, idx_name, tgt_idx[idx_name]))

    for t, idx_name, idx_def in new_indexes:
        if isinstance(idx_def, dict):
            idx_cols = idx_def.get("columns")
        else:
            idx_cols = list(idx_def) if idx_def is not None else None
        ops.append({
            "id": new_id(),
            "type": "add_index",
            "table": t,
            "index": idx_name,
            "columns": idx_cols,
            "risk": "low",
            "rollback": f"DROP INDEX {idx_name}",
        })

    # ----- Phase 4: add foreign keys then validate -----
    new_fks: list[tuple] = []  # (table, fk)
    for t in sorted(target_tables):
        tgt_fks = target_tables[t].get("foreign_keys", []) or []
        cur_fks = (current_tables.get(t, {}) or {}).get("foreign_keys", []) or []
        cur_keys = {fk_key(fk) for fk in cur_fks}
        for fk in tgt_fks:
            if fk_key(fk) not in cur_keys:
                new_fks.append((t, fk))

    new_fks.sort(key=lambda x: (
        x[0],
        x[1].get("ref_table") or "",
        tuple(x[1].get("columns", []) or []),
        fk_name(x[0], x[1]),
    ))

    fk_records: list[tuple] = []  # (table, name, fk)
    for t, fk in new_fks:
        name = fk_name(t, fk)
        fk_records.append((t, name, fk))
        ops.append({
            "id": new_id(),
            "type": "add_foreign_key",
            "table": t,
            "name": name,
            "columns": fk.get("columns"),
            "ref_table": fk.get("ref_table"),
            "ref_columns": fk.get("ref_columns"),
            "risk": "medium",
            "rollback": f"ALTER TABLE {t} DROP CONSTRAINT {name}",
        })

    for t, name, fk in fk_records:
        ops.append({
            "id": new_id(),
            "type": "validate_foreign_key",
            "table": t,
            "name": name,
            "columns": fk.get("columns"),
            "ref_table": fk.get("ref_table"),
            "risk": "low",
            "rollback": f"-- validation of {name} cannot be rolled back",
        })

    # ----- Phase 5: drops (after all additive changes) -----
    def emit_drop_column(t: str, c: str, spec: dict) -> None:
        defer_drop = spec.get("defer_drop") if isinstance(spec, dict) else None
        rollback = f"ALTER TABLE {t} ADD COLUMN {c}"
        if zero_downtime and defer_drop is not False:
            ops.append({
                "id": new_id(),
                "type": "drop_column",
                "table": t,
                "column": c,
                "deferred": True,
                "risk": "medium",
                "rollback": rollback,
            })
        else:
            ops.append({
                "id": new_id(),
                "type": "drop_column",
                "table": t,
                "column": c,
                "deferred": False,
                "risk": "high",
                "rollback": rollback,
            })

    drop_col_specs.sort(key=lambda x: (x[0], x[1]))
    for t, c, spec in drop_col_specs:
        emit_drop_column(t, c, spec)

    for t in dropped_tables:
        spec = current_tables[t] if isinstance(current_tables[t], dict) else {}
        defer_drop = spec.get("defer_drop")
        rollback = f"-- recreate table {t} from prior schema"
        if zero_downtime and defer_drop is not False:
            ops.append({
                "id": new_id(),
                "type": "drop_table",
                "table": t,
                "deferred": True,
                "risk": "medium",
                "rollback": rollback,
            })
        else:
            ops.append({
                "id": new_id(),
                "type": "drop_table",
                "table": t,
                "deferred": False,
                "risk": "high",
                "rollback": rollback,
            })

    return ops
