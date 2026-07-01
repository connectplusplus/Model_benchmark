# reason: TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

def plan_migration(current: dict, target: dict, constraints: dict | None = None) -> list[dict]:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get('zero_downtime', False))

    ops: list[dict] = []
    _counter = 0

    def _new_id() -> str:
        nonlocal _counter
        _counter += 1
        return f"op_{_counter:04d}"

    def emit(**kwargs) -> dict:
        op = {'id': _new_id()}
        op.update(kwargs)
        ops.append(op)
        return op

    current_tables = current.get('tables', {}) or {}
    target_tables = target.get('tables', {}) or {}

    new_table_names = sorted(set(target_tables) - set(current_tables))
    dropped_table_names = sorted(set(current_tables) - set(target_tables))
    common_table_names = sorted(set(current_tables) & set(target_tables))

    # 1. Add new tables (columns + primary_key only; FKs added later).
    for t in new_table_names:
        tdef = target_tables[t]
        emit(
            type='add_table',
            table=t,
            risk='low',
            rollback=f"drop table {t}",
            columns=tdef.get('columns', {}),
            primary_key=tdef.get('primary_key'),
        )

    # 2. Add columns on existing tables, with backfill/alter_nullable for NOT NULL no-default.
    pending_backfill: list[tuple[str, str, dict]] = []
    pending_alter_nullable: list[tuple[str, str]] = []

    for t in common_table_names:
        cur_cols = current_tables[t].get('columns', {}) or {}
        tgt_cols = target_tables[t].get('columns', {}) or {}
        for c in sorted(set(tgt_cols) - set(cur_cols)):
            col_def = tgt_cols[c] or {}
            renamed_from = col_def.get('renamed_from')
            if renamed_from and renamed_from in cur_cols:
                # treated as a rename; skip add/drop pair
                continue
            nullable = col_def.get('nullable', True)
            has_default = 'default' in col_def and col_def.get('default') is not None
            needs_backfill = (not nullable) and (not has_default)

            if needs_backfill:
                effective_def = dict(col_def)
                effective_def['nullable'] = True
                emit(
                    type='add_column',
                    table=t,
                    column=c,
                    risk='low',
                    rollback=f"drop column {t}.{c}",
                    definition=effective_def,
                )
                pending_backfill.append((t, c, col_def))
                pending_alter_nullable.append((t, c))
            else:
                emit(
                    type='add_column',
                    table=t,
                    column=c,
                    risk='low',
                    rollback=f"drop column {t}.{c}",
                    definition=col_def,
                )

    # 3. Backfills.
    for t, c, col_def in pending_backfill:
        emit(
            type='backfill_column',
            table=t,
            column=c,
            risk='medium',
            rollback=f"no-op (backfill of {t}.{c} is idempotent)",
            default=col_def.get('default'),
        )

    # 4. Alter nullable -> NOT NULL.
    for t, c in pending_alter_nullable:
        emit(
            type='alter_nullable',
            table=t,
            column=c,
            risk='low',
            rollback=f"alter {t}.{c} set nullable",
            nullable=False,
        )

    # 5. Add new indexes (new tables: all indexes; common tables: diff).
    new_indexes: list[tuple[str, str, object]] = []
    for t in new_table_names:
        idxs = target_tables[t].get('indexes', {}) or {}
        for name in sorted(idxs):
            new_indexes.append((t, name, idxs[name]))
    for t in common_table_names:
        cur_idxs = current_tables[t].get('indexes', {}) or {}
        tgt_idxs = target_tables[t].get('indexes', {}) or {}
        for name in sorted(set(tgt_idxs) - set(cur_idxs)):
            new_indexes.append((t, name, tgt_idxs[name]))

    for t, name, idx_def in new_indexes:
        emit(
            type='add_index',
            table=t,
            index=name,
            risk='low',
            rollback=f"drop index {name} on {t}",
            definition=idx_def,
        )

    # 6. Add foreign keys, then validate (after indexes).
    def _fk_key(fk: dict) -> tuple:
        return (
            fk.get('name'),
            tuple(fk.get('columns', []) or []),
            fk.get('ref_table'),
            tuple(fk.get('ref_columns', []) or []),
        )

    new_fks: list[tuple[str, dict]] = []
    for t in new_table_names:
        for fk in target_tables[t].get('foreign_keys', []) or []:
            new_fks.append((t, fk))
    for t in common_table_names:
        cur_fks = {_fk_key(fk) for fk in (current_tables[t].get('foreign_keys', []) or [])}
        for fk in target_tables[t].get('foreign_keys', []) or []:
            if _fk_key(fk) not in cur_fks:
                new_fks.append((t, fk))

    new_fks.sort(key=lambda x: (
        x[0],
        x[1].get('name') or '',
        tuple(x[1].get('columns', []) or []),
        x[1].get('ref_table') or '',
    ))

    fk_records: list[tuple[str, dict, str]] = []
    for t, fk in new_fks:
        cols = list(fk.get('columns', []) or [])
        name = fk.get('name') or f"fk_{t}_{'_'.join(cols)}"
        emit(
            type='add_foreign_key',
            table=t,
            name=name,
            risk='medium',
            rollback=f"drop foreign key {name} on {t}",
            columns=cols,
            ref_table=fk.get('ref_table'),
            ref_columns=list(fk.get('ref_columns', []) or []),
        )
        fk_records.append((t, fk, name))

    for t, fk, name in fk_records:
        emit(
            type='validate_foreign_key',
            table=t,
            name=name,
            risk='low',
            rollback=f"no-op (validate of {name} is read-only)",
            columns=list(fk.get('columns', []) or []),
            ref_table=fk.get('ref_table'),
        )

    # 7. Drop columns (after additive changes).
    dropped_columns: list[tuple[str, str, dict]] = []
    for t in common_table_names:
        cur_cols = current_tables[t].get('columns', {}) or {}
        tgt_cols = target_tables[t].get('columns', {}) or {}
        renamed_sources: set[str] = set()
        for _, col_def in tgt_cols.items():
            if isinstance(col_def, dict):
                rn = col_def.get('renamed_from')
                if rn and rn in cur_cols:
                    renamed_sources.add(rn)
        for c in sorted(set(cur_cols) - set(tgt_cols) - renamed_sources):
            dropped_columns.append((t, c, cur_cols[c] if isinstance(cur_cols[c], dict) else {}))

    for t, c, col_def in dropped_columns:
        defer_drop = col_def.get('defer_drop', True) if isinstance(col_def, dict) else True
        if zero_downtime and defer_drop is not False:
            emit(
                type='drop_column',
                table=t,
                column=c,
                risk='medium',
                rollback=f"restore column {t}.{c}",
                deferred=True,
            )
        else:
            emit(
                type='drop_column',
                table=t,
                column=c,
                risk='high',
                rollback=f"restore column {t}.{c}",
                deferred=False,
            )

    # 8. Drop tables (after column drops).
    for t in dropped_table_names:
        cur_def = current_tables[t] if isinstance(current_tables[t], dict) else {}
        defer_drop = cur_def.get('defer_drop', True)
        if zero_downtime and defer_drop is not False:
            emit(
                type='drop_table',
                table=t,
                risk='medium',
                rollback=f"restore table {t}",
                deferred=True,
            )
        else:
            emit(
                type='drop_table',
                table=t,
                risk='high',
                rollback=f"restore table {t}",
                deferred=False,
            )

    return ops
