import argparse
from pathlib import Path

from sqlalchemy import MetaData, URL, create_engine, func, insert, select, text

from app.config import BASE_DIR, Config


_ALEMBIC_TABLE = "alembic_version"


def _reset_postgresql_sequences(connection, tables):
    for table in tables:
        for column in table.primary_key.columns:
            sequence_name = connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table.fullname, "column_name": column.name},
            ).scalar_one_or_none()
            if sequence_name is None:
                continue

            last_value = connection.execute(select(func.max(column))).scalar_one()
            if last_value is not None:
                connection.execute(
                    text(
                        "SELECT setval(CAST(:sequence_name AS regclass), "
                        ":last_value, true)"
                    ),
                    {"sequence_name": sequence_name, "last_value": last_value},
                )


def copy_database_data(source_engine, target_engine, *, batch_size=500):
    if source_engine.dialect.name != "sqlite":
        raise ValueError("The source database must be SQLite.")
    if batch_size < 1:
        raise ValueError("The batch size must be greater than zero.")

    source_metadata = MetaData()
    target_metadata = MetaData()

    with source_engine.connect() as source, target_engine.connect() as target:
        with source.begin(), target.begin():
            source_metadata.reflect(bind=source)
            target_metadata.reflect(bind=target)
            source_tables = set(source_metadata.tables) - {_ALEMBIC_TABLE}
            target_tables = set(target_metadata.tables) - {_ALEMBIC_TABLE}
            if source_tables != target_tables:
                missing = sorted(source_tables - target_tables)
                extra = sorted(target_tables - source_tables)
                raise ValueError(
                    "Source and target schemas differ. "
                    f"Target is missing {missing}; source is missing {extra}. "
                    "Upgrade both schemas to the same Alembic revision before copying."
                )

            for name in sorted(source_tables):
                source_columns = set(source_metadata.tables[name].columns.keys())
                target_columns = set(target_metadata.tables[name].columns.keys())
                if source_columns != target_columns:
                    raise ValueError(
                        f"Column definitions differ for {name}; "
                        "upgrade both schemas to the same Alembic revision before copying."
                    )

            tables = [
                table
                for table in target_metadata.sorted_tables
                if table.name in target_tables
            ]
            for table in tables:
                existing_rows = target.scalar(select(func.count()).select_from(table))
                if existing_rows:
                    raise ValueError(
                        f"Target table {table.name} is not empty; no data was copied."
                    )

            source_counts = {}
            for target_table in tables:
                source_table = source_metadata.tables[target_table.name]
                result = source.execute(select(source_table))
                copied = 0
                while rows := result.mappings().fetchmany(batch_size):
                    target.execute(insert(target_table), [dict(row) for row in rows])
                    copied += len(rows)
                source_counts[target_table.name] = copied

            if target_engine.dialect.name == "postgresql":
                _reset_postgresql_sequences(target, tables)

            for table in tables:
                target_count = target.scalar(select(func.count()).select_from(table))
                if target_count != source_counts[table.name]:
                    raise RuntimeError(
                        f"Row-count verification failed for {table.name}; the transaction was rolled back."
                    )

    return source_counts


def main():
    parser = argparse.ArgumentParser(
        description="Copy rows from the current SQLite schema into an empty PostgreSQL or MySQL database."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=BASE_DIR / "instance" / "attendance.db",
        help="Path to the SQLite database (defaults to instance/attendance.db).",
    )
    parser.add_argument(
        "--confirm-empty-target",
        action="store_true",
        help="Required confirmation that the target database is empty.",
    )
    args = parser.parse_args()

    if not args.confirm_empty_target:
        parser.error("Pass --confirm-empty-target after verifying the target database is empty.")

    source_path = args.source.resolve()
    if not source_path.is_file():
        parser.error(f"SQLite source database does not exist: {source_path}")

    target_url = Config.SQLALCHEMY_DATABASE_URI
    source_engine = create_engine(URL.create("sqlite", database=str(source_path)))
    target_engine = create_engine(target_url, **Config.SQLALCHEMY_ENGINE_OPTIONS)
    try:
        if target_engine.dialect.name not in {"mysql", "postgresql"}:
            parser.error("Set DATABASE_URL to a PostgreSQL or MySQL URL.")
        row_counts = copy_database_data(source_engine, target_engine)
    finally:
        source_engine.dispose()
        target_engine.dispose()

    total_rows = sum(row_counts.values())
    print(f"Copied and verified {total_rows} rows across {len(row_counts)} tables.")


if __name__ == "__main__":
    main()
