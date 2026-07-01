# reason: AssertionError: ['text', 'backfill_column', 'alter_nullable']

from typing import Optional


def plan_migration(current: dict, target: dict, constraints=None) -> list:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime", False))

    current_tables = (current or {}).get("tables", {}) or {}
    target_tables = (target or {}).get("tables", {}) or {}

    operations = []
    counter = [0]

    def make_id(type_, table, *extra):
        counter[0] += 1
        parts = [f"{counter[0]:04d}", type_, table]
        parts.extend(str(e) for e in extra if e is not None)
        return "-".join(parts)

    def emit(type_, table, risk, rollback, **extra):
        op = {
            "id": make_id(type_, table, *(extra.get("_id_extra") or [])),
            "type": type_,
            "table": table,
            "risk": risk,
            "rollback": rollback,
        }
        for k, v in extra.items():
            if k == "_id_extra":
                continue
            op[k] = v
        operations.append(op)
        return op

    # Determine ordering of tables for deterministic output.
    new_table_names = sorted(t for t in target_tables if t not in current_tables)
    dropped_table_names = sorted(t for t in current_tables if t not in target_tables)
    common_table_names = sorted(t for t in target_tables if t in current_tables)

    def topo_sort_new_tables(names):
        names_set = set(names)
        ordered = []
        visited = set()
        temp = set()

        def visit(name):
            if name in visited:
                return
            if name in temp:
                # cycle; break deterministically
                return
            temp.add(name)
            spec = target_tables.get(name, {}) or {}
            deps = sorted(
                {
                    fk.get("ref_table")
                    for fk in (spec.get("foreign_keys") or [])
                    if fk.get("ref_table") in names_set
                    and fk.get("ref_table") != name
                }
            )
            for dep in deps:
                visit(dep)
            temp.discard(name)
            visited.add(name)
            ordered.append(name)

        for n in sorted(names):
            visit(n)
        return ordered

    ordered_new_tables = topo_sort_new_tables(new_table_names)

    def column_has_default(col):
        return col.get("default", None) is not None

    def col_is_nullable(col):
        return bool(col.get("nullable", True))

    # ---- Additive: new tables ----
    for tname in ordered_new_tables:
        spec = target_tables[tname] or {}
        columns = spec.get("columns", {}) or {}
        pk = spec.get("primary_key")
        emit(
            "add_table",
            tname,
            "low",
            f"DROP TABLE {tname}",
            columns=dict(columns),
            primary_key=pk,
        )

    # ---- Additive: new columns on existing tables ----
    for tname in common_table_names:
        cur_spec = current_tables[tname] or {}
        tgt_spec = target_tables[tname] or {}
        cur_cols = cur_spec.get("columns", {}) or {}
        tgt_cols = tgt_spec.get("columns", {}) or {}

        new_cols = sorted(c for c in tgt_cols if c not in cur_cols)
        for cname in new_cols:
            col = tgt_cols[cname] or {}
            renamed_from = col.get("renamed_from")
            # If renamed_from refers to existing column, still treat as add (rename handled as add+drop conceptually)
            not_null = not col_is_nullable(col)
            has_default = column_has_default(col)

            if not_null and not has_default:
                # add as nullable, backfill, alter to not null
                emit(
                    "add_column",
                    tname,
                    "low",
                    f"ALTER TABLE {tname} DROP COLUMN {cname}",
                    column=cname,
                    nullable=True,
                    type=col.get("type"),
                    default=col.get("default"),
                    renamed_from=renamed_from,
                )
                emit(
                    "backfill_column",
                    tname,
                    "medium",
                    f"-- no-op rollback for backfill of {tname}.{cname}",
                    column=cname,
                    renamed_from=renamed_from,
                )
                emit(
                    "alter_nullable",
                    tname,
                    "medium",
                    f"ALTER TABLE {tname} ALTER COLUMN {cname} DROP NOT NULL",
                    column=cname,
                    nullable=False,
                )
            else:
                emit(
                    "add_column",
                    tname,
                    "low",
                    f"ALTER TABLE {tname} DROP COLUMN {cname}",
                    column=cname,
                    nullable=col_is_nullable(col),
                    type=col.get("type"),
                    default=col.get("default"),
                    renamed_from=renamed_from,
                )

    # ---- Additive: indexes ----
    def existing_indexes(spec):
        return {idx_key(i): i for i in (spec.get("indexes") or [])}

    def idx_key(idx):
        if isinstance(idx, dict):
            name = idx.get("name")
            cols = tuple(idx.get("columns") or [])
            return (name, cols)
        return (None, tuple(idx))

    # Track which (table, columns) indexes are added so FK validation ordering can rely on them.
    added_index_cols = set()

    for tname in common_table_names + ordered_new_tables:
        if tname in target_tables:
            tgt_spec = target_tables[tname] or {}
        else:
            continue
        cur_spec = current_tables.get(tname, {}) or {}
        cur_idx = existing_indexes(cur_spec)
        tgt_idx_list = tgt_spec.get("indexes") or []

        # deterministic ordering
        def sort_key(idx):
            k = idx_key(idx)
            return (str(k[0]), tuple(k[1]))

        for idx in sorted(tgt_idx_list, key=sort_key):
            k = idx_key(idx)
            if k not in cur_idx:
                cols = list(k[1])
                name = k[0]
                emit(
                    "add_index",
                    tname,
                    "low",
                    f"DROP INDEX {name}" if name else f"DROP INDEX ON {tname} ({', '.join(cols)})",
                    columns=cols,
                    name=name,
                )
                added_index_cols.add((tname, tuple(cols)))

    # ---- Additive: foreign keys ----
    def fk_key(fk):
        return (
            fk.get("name"),
            tuple(fk.get("columns") or []),
            fk.get("ref_table"),
            tuple(fk.get("ref_columns") or []),
        )

    for tname in ordered_new_tables + common_table_names:
        tgt_spec = target_tables.get(tname, {}) or {}
        cur_spec = current_tables.get(tname, {}) or {}
        cur_fks = {fk_key(f) for f in (cur_spec.get("foreign_keys") or [])}
        tgt_fks = tgt_spec.get("foreign_keys") or []

        def fk_sort_key(fk):
            k = fk_key(fk)
            return (str(k[0]), tuple(k[1]), str(k[2]), tuple(k[3]))

        for fk in sorted(tgt_fks, key=fk_sort_key):
            k = fk_key(fk)
            if k in cur_fks:
                continue
            name = fk.get("name")
            cols = list(fk.get("columns") or [])
            ref_table = fk.get("ref_table")
            ref_cols = list(fk.get("ref_columns") or [])
            emit(
                "add_foreign_key",
                tname,
                "medium",
                f"ALTER TABLE {tname} DROP CONSTRAINT {name}"
                if name
                else f"ALTER TABLE {tname} DROP FOREIGN KEY ({', '.join(cols)})",
                columns=cols,
                ref_table=ref_table,
                ref_columns=ref_cols,
                name=name,
            )
            emit(
                "validate_foreign_key",
                tname,
                "low",
                f"-- no-op rollback for FK validation on {tname}",
                columns=cols,
                ref_table=ref_table,
                ref_columns=ref_cols,
                name=name,
            )

    # ---- Destructive: drop columns ----
    for tname in common_table_names:
        cur_spec = current_tables[tname] or {}
        tgt_spec = target_tables[tname] or {}
        cur_cols = cur_spec.get("columns", {}) or {}
        tgt_cols = tgt_spec.get("columns", {}) or {}

        # columns that are being renamed are preserved (their renamed_from is the old name)
        renamed_sources = {
            (c.get("renamed_from"))
            for c in tgt_cols.values()
            if c and c.get("renamed_from")
        }

        dropped_cols = sorted(
            c
            for c in cur_cols
            if c not in tgt_cols and c not in renamed_sources
        )
        for cname in dropped_cols:
            target_obj = cur_cols.get(cname, {}) or {}
            _emit_drop(
                emit,
                zero_downtime,
                op_type="drop_column",
                table=tname,
                target_obj=tgt_spec.get("columns", {}).get(cname),
                rollback=f"ALTER TABLE {tname} ADD COLUMN {cname}",
                column=cname,
            )

    # ---- Destructive: drop tables ----
    for tname in dropped_table_names:
        _emit_drop(
            emit,
            zero_downtime,
            op_type="drop_table",
            table=tname,
            target_obj=target_tables.get(tname),
            rollback=f"CREATE TABLE {tname} ...",
        )

    return operations


def _emit_drop(emit, zero_downtime, op_type, table, target_obj, rollback, **extra):
    # Determine defer_drop setting from target object if present.
    defer_flag = None
    if isinstance(target_obj, dict):
        defer_flag = target_obj.get("defer_drop", None)

    if zero_downtime and defer_flag is not False:
        emit(
            op_type,
            table,
            "medium",
            rollback,
            deferred=True,
            **extra,
        )
    else:
        emit(
            op_type,
            table,
            "high",
            rollback,
            deferred=False,
            **extra,
        )
