# reason: AssertionError: []

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
        return f"op_{counter[0]:04d}_{kind}"

    def column_has_default(col: dict) -> bool:
        return col.get("default", None) is not None

    def is_not_null(col: dict) -> bool:
        return not col.get("nullable", True)

    def fk_key(fk: dict):
        return (
            tuple(fk.get("columns", []) or []),
            fk.get("ref_table"),
            tuple(fk.get("ref_columns", []) or []),
        )

    def fk_name(fk: dict, table: str) -> str:
        name = fk.get("name")
        if name:
            return name
        cols = "_".join(fk.get("columns", []) or [])
        return f"fk_{table}_{cols}"

    def index_key(idx: dict):
        return (
            idx.get("name"),
            tuple(idx.get("columns", []) or []),
            bool(idx.get("unique", False)),
        )

    def index_name(idx: dict, table: str) -> str:
        name = idx.get("name")
        if name:
            return name
        cols = "_".join(idx.get("columns", []) or [])
        return f"idx_{table}_{cols}"

    # Determine table-level changes.
    current_table_names = set(current_tables)
    target_table_names = set(target_tables)

    added_table_names = sorted(target_table_names - current_table_names)
    dropped_table_names = sorted(current_table_names - target_table_names)
    common_table_names = sorted(current_table_names & target_table_names)

    # Topologically order added tables so that parents come before children.
    def order_added_tables(names):
        name_set = set(names)
        deps = {}
        for name in names:
            tdef = target_tables.get(name, {}) or {}
            fks = tdef.get("foreign_keys", []) or []
            d = set()
            for fk in fks:
                ref = fk.get("ref_table")
                if ref in name_set and ref != name:
                    d.add(ref)
            deps[name] = d

        ordered = []
        placed = set()
        remaining = list(sorted(names))
        # Deterministic topological sort.
        progress = True
        while remaining and progress:
            progress = False
            still = []
            for name in remaining:
                if deps[name] <= placed:
                    ordered.append(name)
                    placed.add(name)
                    progress = True
                else:
                    still.append(name)
            remaining = still
        # Any remaining (cycles) appended in sorted order.
        ordered.extend(sorted(remaining))
        return ordered

    ordered_added_tables = order_added_tables(added_table_names)

    # 1) Add new tables (without their foreign keys first; fks added later).
    new_table_fks = []  # list of (table, fk)
    for name in ordered_added_tables:
        tdef = target_tables.get(name, {}) or {}
        operations.append({
            "id": next_id("add_table"),
            "type": "add_table",
            "table": name,
            "risk": "low",
            "rollback": f"DROP TABLE {name}",
        })
        for fk in (tdef.get("foreign_keys", []) or []):
            new_table_fks.append((name, fk))

    # 2) Column changes on common tables.
    column_drops = []  # (table, column)
    table_drops_existing = []  # tables being dropped entirely

    new_fk_from_existing = []  # (table, fk)
    new_indexes = []  # (table, idx)

    for name in common_table_names:
        cur = current_tables.get(name, {}) or {}
        tgt = target_tables.get(name, {}) or {}

        cur_cols = cur.get("columns", {}) or {}
        tgt_cols = tgt.get("columns", {}) or {}

        cur_col_names = set(cur_cols)
        tgt_col_names = set(tgt_cols)

        # Resolve renamed_from to know which target columns are truly new.
        added_cols = sorted(tgt_col_names - cur_col_names)

        for col_name in added_cols:
            col = tgt_cols.get(col_name, {}) or {}
            renamed_from = col.get("renamed_from")
            not_null = is_not_null(col)
            has_default = column_has_default(col)

            if renamed_from and renamed_from in cur_col_names:
                # Treat as rename: add new column then drop old.
                operations.append({
                    "id": next_id("add_column"),
                    "type": "add_column",
                    "table": name,
                    "risk": "low",
                    "rollback": f"ALTER TABLE {name} DROP COLUMN {col_name}",
                })
                operations.append({
                    "id": next_id("backfill_column"),
                    "type": "backfill_column",
                    "table": name,
                    "risk": "medium",
                    "rollback": (
                        f"UPDATE {name} SET {col_name} = NULL "
                        f"-- revert backfill from {renamed_from}"
                    ),
                })
                if not_null:
                    operations.append({
                        "id": next_id("alter_nullable"),
                        "type": "alter_nullable",
                        "table": name,
                        "risk": "medium",
                        "rollback": (
                            f"ALTER TABLE {name} ALTER COLUMN "
                            f"{col_name} DROP NOT NULL"
                        ),
                    })
                column_drops.append((name, renamed_from))
                continue

            if not_null and not has_default:
                # add as nullable, backfill, then enforce not null.
                operations.append({
                    "id": next_id("add_column"),
                    "type": "add_column",
                    "table": name,
                    "risk": "low",
                    "rollback": f"ALTER TABLE {name} DROP COLUMN {col_name}",
                })
                operations.append({
                    "id": next_id("backfill_column"),
                    "type": "backfill_column",
                    "table": name,
                    "risk": "medium",
                    "rollback": f"UPDATE {name} SET {col_name} = NULL",
                })
                operations.append({
                    "id": next_id("alter_nullable"),
                    "type": "alter_nullable",
                    "table": name,
                    "risk": "medium",
                    "rollback": (
                        f"ALTER TABLE {name} ALTER COLUMN "
                        f"{col_name} DROP NOT NULL"
                    ),
                })
            else:
                operations.append({
                    "id": next_id("add_column"),
                    "type": "add_column",
                    "table": name,
                    "risk": "low",
                    "rollback": f"ALTER TABLE {name} DROP COLUMN {col_name}",
                })

        # Columns dropped on existing table.
        renamed_sources = {
            (tgt_cols.get(c, {}) or {}).get("renamed_from")
            for c in added_cols
        }
        for col_name in sorted(cur_col_names - tgt_col_names):
            if col_name in renamed_sources:
                continue  # already scheduled as part of rename
            column_drops.append((name, col_name))

        # New foreign keys on existing tables.
        cur_fks = {fk_key(fk): fk for fk in (cur.get("foreign_keys", []) or [])}
        for fk in (tgt.get("foreign_keys", []) or []):
            if fk_key(fk) not in cur_fks:
                new_fk_from_existing.append((name, fk))

        # New indexes on existing tables.
        cur_idx = {index_key(i) for i in (cur.get("indexes", []) or [])}
        for idx in (tgt.get("indexes", []) or []):
            if index_key(idx) not in cur_idx:
                new_indexes.append((name, idx))

    # Collect new indexes for newly added tables too.
    for name in ordered_added_tables:
        tgt = target_tables.get(name, {}) or {}
        for idx in (tgt.get("indexes", []) or []):
            new_indexes.append((name, idx))

    # 3) Add indexes (before validating fks).
    new_indexes_sorted = sorted(new_indexes, key=lambda p: (p[0], index_key(p[1])))
    for table, idx in new_indexes_sorted:
        iname = index_name(idx, table)
        operations.append({
            "id": next_id("add_index"),
            "type": "add_index",
            "table": table,
            "risk": "low",
            "rollback": f"DROP INDEX {iname}",
        })

    # 4) Add + validate foreign keys (new table fks, then existing table fks).
    all_new_fks = []
    for table, fk in new_table_fks:
        all_new_fks.append((table, fk))
    for table, fk in sorted(new_fk_from_existing, key=lambda p: (p[0], fk_key(p[1]))):
        all_new_fks.append((table, fk))

    for table, fk in all_new_fks:
        name = fk_name(fk, table)
        operations.append({
            "id": next_id("add_foreign_key"),
            "type": "add_foreign_key",
            "table": table,
            "risk": "medium",
            "rollback": f"ALTER TABLE {table} DROP CONSTRAINT {name}",
        })
        operations.append({
            "id": next_id("validate_foreign_key"),
            "type": "validate_foreign_key",
            "table": table,
            "risk": "low",
            "rollback": f"-- no-op: re-validate constraint {name} on {table}",
        })

    # 5/6) Drops happen after additive changes.
    def emit_drop_column(table, column):
        col_def = (
            (target_tables.get(table, {}) or {}).get("columns", {}) or {}
        )
        # The dropped column no longer exists in target; defer_drop info, if
        # present, would live in current schema metadata.
        meta = (
            (current_tables.get(table, {}) or {}).get("columns", {}) or {}
        ).get(column, {}) or {}
        defer_drop = meta.get("defer_drop", None)
        if zero_downtime and defer_drop is not False:
            operations.append({
                "id": next_id("drop_column"),
                "type": "drop_column",
                "table": table,
                "risk": "medium",
                "rollback": f"ALTER TABLE {table} ADD COLUMN {column}",
            })
        else:
            operations.append({
                "id": next_id("drop_column"),
                "type": "drop_column",
                "table": table,
                "risk": "high",
                "rollback": f"ALTER TABLE {table} ADD COLUMN {column}",
            })

    def emit_drop_table(table):
        meta = current_tables.get(table, {}) or {}
        defer_drop = meta.get("defer_drop", None)
        if zero_downtime and defer_drop is not False:
            operations.append({
                "id": next_id("drop_table"),
                "type": "drop_table",
                "table": table,
                "risk": "medium",
                "rollback": f"-- recreate table {table} from backup",
            })
        else:
            operations.append({
                "id": next_id("drop_table"),
                "type": "drop_table",
                "table": table,
                "risk": "high",
                "rollback": f"-- recreate table {table} from backup",
            })

    for table, column in sorted(column_drops):
        emit_drop_column(table, column)

    for table in dropped_table_names:
        emit_drop_table(table)

    return operations
