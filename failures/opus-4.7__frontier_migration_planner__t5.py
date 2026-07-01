# reason: TypeError: list indices must be integers or slices, not dict

def _fk_key(fk):
    return (fk.get('name'), tuple(fk['columns']), fk['ref_table'], tuple(fk['ref_columns']))


def _fk_name(table, fk):
    return fk.get('name') or f"fk_{table}_{'_'.join(fk['columns'])}"


def plan_migration(current: dict, target: dict, constraints=None) -> list:
    constraints = constraints or {}
    zero_downtime = bool(constraints.get('zero_downtime', False))

    ops = []
    _n = [0]

    def _id():
        _n[0] += 1
        return f"op_{_n[0]:04d}"

    cur_tables = current.get('tables', {})
    tgt_tables = target.get('tables', {})

    new_tables = sorted(set(tgt_tables) - set(cur_tables))
    dropped_tables = sorted(set(cur_tables) - set(tgt_tables))
    common_tables = sorted(set(tgt_tables) & set(cur_tables))

    # Phase 1: add new tables (structure + PK, no FKs yet)
    for t in new_tables:
        ops.append({
            'id': _id(),
            'type': 'add_table',
            'table': t,
            'columns': dict(tgt_tables[t].get('columns', {})),
            'primary_key': list(tgt_tables[t].get('primary_key', [])),
            'risk': 'low',
            'rollback': f'drop_table {t}',
        })

    # Phase 2: column diffs on existing tables
    add_col_specs = []   # (table, col, col_def)
    drop_col_specs = []  # (table, col)
    renamed_old_by_table = {}

    for t in common_tables:
        cur_cols = cur_tables[t].get('columns', {})
        tgt_cols = tgt_tables[t].get('columns', {})

        renamed_old = set()
        for new_name, col_def in tgt_cols.items():
            rf = col_def.get('renamed_from')
            if rf and rf in cur_cols and new_name not in cur_cols:
                renamed_old.add(rf)
        renamed_old_by_table[t] = renamed_old

        for col_name in sorted(tgt_cols):
            col_def = tgt_cols[col_name]
            if col_name in cur_cols:
                continue
            if col_def.get('renamed_from') in cur_cols:
                continue
            add_col_specs.append((t, col_name, col_def))

        for col_name in sorted(cur_cols):
            if col_name in tgt_cols:
                continue
            if col_name in renamed_old:
                continue
            drop_col_specs.append((t, col_name))

    # add_column (nullable first if needed)
    needs_two_step = []
    for t, col, col_def in add_col_specs:
        nullable = col_def.get('nullable', True)
        default = col_def.get('default', None)
        two_step = (not nullable) and default is None
        ops.append({
            'id': _id(),
            'type': 'add_column',
            'table': t,
            'column': col,
            'column_type': col_def.get('type'),
            'nullable': True if two_step else nullable,
            'default': default,
            'risk': 'low',
            'rollback': f'drop_column {t}.{col}',
        })
        if two_step:
            needs_two_step.append((t, col))

    # backfill_column
    for t, col in needs_two_step:
        ops.append({
            'id': _id(),
            'type': 'backfill_column',
            'table': t,
            'column': col,
            'risk': 'medium',
            'rollback': f'no-op (backfill of {t}.{col} cannot be reversed automatically)',
        })

    # alter_nullable -> NOT NULL
    for t, col in needs_two_step:
        ops.append({
            'id': _id(),
            'type': 'alter_nullable',
            'table': t,
            'column': col,
            'nullable': False,
            'risk': 'medium',
            'rollback': f'alter_nullable {t}.{col} to true',
        })

    # Phase 3: indexes (new tables and new on existing)
    new_indexes = []
    for t in new_tables:
        for idx_name in sorted(tgt_tables[t].get('indexes', {}) or {}):
            new_indexes.append((t, idx_name, tgt_tables[t]['indexes'][idx_name]))
    for t in common_tables:
        cur_idx = cur_tables[t].get('indexes', {}) or {}
        tgt_idx = tgt_tables[t].get('indexes', {}) or {}
        for idx_name in sorted(tgt_idx):
            if idx_name not in cur_idx:
                new_indexes.append((t, idx_name, tgt_idx[idx_name]))

    for t, idx_name, idx_def in new_indexes:
        ops.append({
            'id': _id(),
            'type': 'add_index',
            'table': t,
            'index': idx_name,
            'columns': list(idx_def.get('columns', [])),
            'unique': bool(idx_def.get('unique', False)),
            'risk': 'low',
            'rollback': f'drop_index {idx_name} on {t}',
        })

    # Phase 4: foreign keys
    new_fks = []
    for t in new_tables:
        fks = tgt_tables[t].get('foreign_keys', []) or []
        for fk in sorted(fks, key=_fk_key):
            new_fks.append((t, fk))
    for t in common_tables:
        cur_fks = {_fk_key(fk): fk for fk in (cur_tables[t].get('foreign_keys', []) or [])}
        tgt_fks = {_fk_key(fk): fk for fk in (tgt_tables[t].get('foreign_keys', []) or [])}
        for k in sorted(tgt_fks):
            if k not in cur_fks:
                new_fks.append((t, tgt_fks[k]))

    for t, fk in new_fks:
        name = _fk_name(t, fk)
        ops.append({
            'id': _id(),
            'type': 'add_foreign_key',
            'table': t,
            'name': name,
            'columns': list(fk['columns']),
            'ref_table': fk['ref_table'],
            'ref_columns': list(fk['ref_columns']),
            'risk': 'medium',
            'rollback': f'drop_foreign_key {name} on {t}',
        })

    for t, fk in new_fks:
        name = _fk_name(t, fk)
        ops.append({
            'id': _id(),
            'type': 'validate_foreign_key',
            'table': t,
            'name': name,
            'risk': 'low',
            'rollback': f'no-op (validation of {name})',
        })

    # Phase 5: drops (after additive changes)
    for t, col in drop_col_specs:
        cur_col_def = cur_tables[t].get('columns', {}).get(col, {}) or {}
        defer_drop = cur_col_def.get('defer_drop', True)
        if zero_downtime and defer_drop:
            ops.append({
                'id': _id(),
                'type': 'drop_column',
                'table': t,
                'column': col,
                'deferred': True,
                'risk': 'medium',
                'rollback': f'add_column {t}.{col}',
            })
        else:
            ops.append({
                'id': _id(),
                'type': 'drop_column',
                'table': t,
                'column': col,
                'deferred': False,
                'risk': 'high',
                'rollback': f'add_column {t}.{col}',
            })

    for t in dropped_tables:
        cur_tbl = cur_tables[t] or {}
        defer_drop = cur_tbl.get('defer_drop', True)
        if zero_downtime and defer_drop:
            ops.append({
                'id': _id(),
                'type': 'drop_table',
                'table': t,
                'deferred': True,
                'risk': 'medium',
                'rollback': f'add_table {t}',
            })
        else:
            ops.append({
                'id': _id(),
                'type': 'drop_table',
                'table': t,
                'deferred': False,
                'risk': 'high',
                'rollback': f'add_table {t}',
            })

    return ops
