# reason: AttributeError: 'list' object has no attribute 'keys'

from typing import Optional


def plan_migration(current: dict, target: dict, constraints=None) -> list:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = current.get("tables", {}) or {}
    target_tables = target.get("tables", {}) or {}

    operations = []
    counter = [0]

    def next_id(kind: str) -> str:
        counter[0] += 1
        return "op_{:04d}_{}".format(counter[0], kind)

    def emit(kind, table, risk, rollback, **extra):
        op = {
            "id": next_id(kind),
            "type": kind,
            "table": table,
            "risk": risk,
            "rollback": rollback,
        }
        op.update(extra)
        operations.append(op)

    def col_is_not_null(coldef):
        return not bool(coldef.get("nullable", True))

    def col_has_default(coldef):
        return coldef.get("default", None) is not None

    # Determine table sets deterministically
    target_names = sorted(target_tables.keys())
    current_names = sorted(current_tables.keys())

    tables_to_add = [t for t in target_names if t not in current_tables]
    tables_to_drop = [t for t in current_names if t not in target_tables]
    tables_common = [t for t in target_names if t in current_tables]

    # ---- Ordering of new tables: parents before children ----
    add_set = set(tables_to_add)

    def order_new_tables(names):
        ordered = []
        visiting = set()
        placed = set()

        def visit(name):
            if name in placed or name in visiting:
                return
            visiting.add(name)
            tdef = target_tables.get(name, {})
            fks = tdef.get("foreign_keys", []) or []
            deps = sorted(
                {
                    fk.get("ref_table")
                    for fk in fks
                    if fk.get("ref_table") in add_set
                    and fk.get("ref_table") != name
                }
            )
            for dep in deps:
                visit(dep)
            visiting.discard(name)
            if name not in placed:
                placed.add(name)
                ordered.append(name)

        for n in sorted(names):
            visit(n)
        return ordered

    ordered_new_tables = order_new_tables(tables_to_add)

    # ============ ADDITIVE PHASE ============

    # 1. Add new tables (without foreign keys), then their indexes
    for table in ordered_new_tables:
        tdef = target_tables[table]
        emit(
            "add_table",
            table,
            "low",
            "DROP TABLE {}".format(table),
            columns=tdef.get("columns", {}),
            primary_key=tdef.get("primary_key", []),
        )

    for table in ordered_new_tables:
        tdef = target_tables[table]
        indexes = tdef.get("indexes", {}) or {}
        for idx_name in sorted(indexes.keys()):
            emit(
                "add_index",
                table,
                "low",
                "DROP INDEX {}".format(idx_name),
                index=idx_name,
                columns=indexes[idx_name],
            )

    # 2. Add columns to existing tables
    for table in tables_common:
        cur = current_tables[table]
        tgt = target_tables[table]
        cur_cols = cur.get("columns", {}) or {}
        tgt_cols = tgt.get("columns", {}) or {}

        new_cols = [c for c in sorted(tgt_cols.keys()) if c not in cur_cols]
        for col in new_cols:
            coldef = tgt_cols[col]
            renamed_from = coldef.get("renamed_from")
            not_null = col_is_not_null(coldef)
            has_default = col_has_default(coldef)

            if not_null and not has_default:
                # add nullable, backfill, then alter to not null
                emit(
                    "add_column",
                    table,
                    "low",
                    "ALTER TABLE {} DROP COLUMN {}".format(table, col),
                    column=col,
                    definition=coldef,
                    nullable=True,
                    renamed_from=renamed_from,
                )
                emit(
                    "backfill_column",
                    table,
                    "medium",
                    "UPDATE {} SET {} = NULL".format(table, col),
                    column=col,
                )
                emit(
                    "alter_nullable",
                    table,
                    "medium",
                    "ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL".format(table, col),
                    column=col,
                    nullable=False,
                )
            else:
                emit(
                    "add_column",
                    table,
                    "low",
                    "ALTER TABLE {} DROP COLUMN {}".format(table, col),
                    column=col,
                    definition=coldef,
                    nullable=coldef.get("nullable", True),
                    renamed_from=renamed_from,
                )

    # 3. Add new indexes on existing tables
    for table in tables_common:
        cur = current_tables[table]
        tgt = target_tables[table]
        cur_idx = cur.get("indexes", {}) or {}
        tgt_idx = tgt.get("indexes", {}) or {}
        new_idx = [i for i in sorted(tgt_idx.keys()) if i not in cur_idx]
        for idx_name in new_idx:
            emit(
                "add_index",
                table,
                "low",
                "DROP INDEX {}".format(idx_name),
                index=idx_name,
                columns=tgt_idx[idx_name],
            )

    # 4. Foreign keys: add then validate. Indexes needed before validation.
    def fk_key(fk):
        return (
            fk.get("name") or "",
            fk.get("ref_table") or "",
            tuple(fk.get("columns", []) or []),
        )

    def existing_fks(tdef):
        return {fk_key(fk) for fk in (tdef.get("foreign_keys", []) or [])}

    # add_foreign_key for all new fks first, then validate
    pending_validations = []

    for table in target_names:
        tgt = target_tables[table]
        tgt_fks = tgt.get("foreign_keys", []) or []
        if table in current_tables:
            cur_fks = existing_fks(current_tables[table])
        else:
            cur_fks = set()

        new_fks = [fk for fk in tgt_fks if fk_key(fk) not in cur_fks]
        new_fks_sorted = sorted(new_fks, key=fk_key)
        for fk in new_fks_sorted:
            fk_name = fk.get("name") or "fk_{}_{}".format(
                table, "_".join(fk.get("columns", []) or [])
            )
            emit(
                "add_foreign_key",
                table,
                "medium",
                "ALTER TABLE {} DROP CONSTRAINT {}".format(table, fk_name),
                fk_name=fk_name,
                columns=fk.get("columns", []),
                ref_table=fk.get("ref_table"),
                ref_columns=fk.get("ref_columns", []),
            )
            pending_validations.append((table, fk_name, fk))

    for table, fk_name, fk in pending_validations:
        emit(
            "validate_foreign_key",
            table,
            "medium",
            "-- no-op: revalidation not required",
            fk_name=fk_name,
            columns=fk.get("columns", []),
            ref_table=fk.get("ref_table"),
            ref_columns=fk.get("ref_columns", []),
        )

    # ============ DESTRUCTIVE PHASE (after additive) ============

    # Drop columns from existing tables
    for table in tables_common:
        cur = current_tables[table]
        tgt = target_tables[table]
        cur_cols = cur.get("columns", {}) or {}
        tgt_cols = tgt.get("columns", {}) or {}

        # columns that were renamed are not "dropped"
        renamed_sources = {
            coldef.get("renamed_from")
            for coldef in tgt_cols.values()
            if coldef.get("renamed_from")
        }

        drop_cols = [
            c
            for c in sorted(cur_cols.keys())
            if c not in tgt_cols and c not in renamed_sources
        ]
        for col in drop_cols:
            _emit_drop(
                emit,
                zero_downtime,
                target_object=tgt_cols.get(col, {}),
                kind="drop_column",
                table=table,
                rollback="-- restore column {}.{} from backup".format(table, col),
                column=col,
            )

    # Drop tables
    for table in tables_to_drop:
        _emit_drop(
            emit,
            zero_downtime,
            target_object=current_tables.get(table, {}),
            kind="drop_table",
            table=table,
            rollback="-- restore table {} from backup".format(table),
        )

    return operations


def _emit_drop(emit, zero_downtime, target_object, kind, table, rollback, **extra):
    defer_drop = target_object.get("defer_drop", None) if isinstance(target_object, dict) else None

    if zero_downtime and defer_drop is not False:
        # deferred, medium risk
        emit(
            kind,
            table,
            "medium",
            rollback,
            deferred=True,
            **extra,
        )
    else:
        emit(
            kind,
            table,
            "high",
            rollback,
            deferred=False,
            **extra,
        )
