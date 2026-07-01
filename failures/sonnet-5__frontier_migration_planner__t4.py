# reason: token_budget_exhausted:max_tokens

import heapq


def plan_migration(current: dict, target: dict, constraints=None) -> list:
    """
    Produce a deterministic, ordered list of migration operations that
    transform `current` schema into `target` schema.

    Each operation is a dict with at least:
        id, type, table, risk (low|medium|high), rollback (str)
    """
    constraints = constraints or {}
    zero_downtime = bool(constraints.get("zero_downtime"))

    cur_tables = current.get("tables", {}) or {}
    tgt_tables = target.get("tables", {}) or {}

    ops = []

    # ---------- Phase 1: create brand new tables (parents before children) ----------
    new_table_names = sorted(t for t in tgt_tables if t not in cur_tables)
    creation_order = _topo_sort(new_table_names, tgt_tables)
    for t in creation_order:
        ops.append(_make_add_table_op(t))

    existing_tables = sorted(t for t in tgt_tables if t in cur_tables)
    all_tables_in_process_order = creation_order + existing_tables

    add_column_ops = []
    backfill_ops = []
    alter_ops = []
    index_ops = []
    fk_add_ops = []
    fk_validate_ops = []
    drop_col_ops = []

    for t in all_tables_in_process_order:
        tgt_table = tgt_tables[t]
        cur_table = cur_tables.get(t)
        is_new_table = cur_table is None

        tgt_columns = tgt_table.get("columns", {}) or {}
        cur_columns = (cur_table.get("columns", {}) or {}) if cur_table else {}

        rename_sources = {}
        for cname, cdef in tgt_columns.items():
            rf = cdef.get("renamed_from")
            if rf:
                rename_sources[cname] = rf

        if not is_new_table:
            for cname in sorted(tgt_columns.keys()):
                cdef = tgt_columns[cname]

                if cname in cur_columns:
                    cur_def = cur_columns[cname]
                    cur_nullable = cur_def.get("nullable", True)
                    tgt_nullable = cdef.get("nullable", True)
                    if cur_nullable and not tgt_nullable:
                        if cdef.get("default") is None:
                            backfill_ops.append(_make_backfill_op(t, cname, None))
                        alter_ops.append(_make_alter_nullable_op(t, cname, not_null=True))
                    elif (not cur_nullable) and tgt_nullable:
                        alter_ops.append(_make_alter_nullable_op(t, cname, not_null=False))
                    continue

                # column does not currently exist -> new or renamed
                rf = rename_sources.get(cname)
                source_old = rf if rf and rf in cur_columns else None
                tgt_nullable = cdef.get("nullable", True)
                has_default = cdef.get("default") is not None

                if source_old:
                    add_column_ops.append(_make_add_column_op(t, cname, cdef, force_nullable=True))
                    backfill_ops.append(_make_backfill_op(t, cname, f"{t}.{source_old}"))
                    if not tgt_nullable:
                        alter_ops.append(_make_alter_nullable_op(t, cname, not_null=True))
                else:
                    if (not tgt_nullable) and (not has_default):
                        add_column_ops.append(_make_add_column_op(t, cname, cdef, force_nullable=True))
                        backfill_ops.append(_make_backfill_op(t, cname, None))
                        alter
