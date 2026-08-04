"""Local Database Backup Tool for PostgreSQL.

Exports a complete, self-contained SQL dump of all database tables
directly to the wa_assistant_backups folder on your Desktop.

Runs standalone with zero external dependencies (no pg_dump version mismatch).

Run manually:
    python -m scripts.backup_db
"""
import asyncio
import os
import sys
from datetime import datetime

from sqlalchemy import MetaData, Table, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings


async def export_database() -> str:
    settings = get_settings()
    db_url = os.environ.get("DATABASE_URL") or settings.database_url

    if not db_url or "sqlite" in db_url:
        print("ERROR: DATABASE_URL must be configured with a valid PostgreSQL connection string in environment variables or .env file.")
        sys.exit(1)

    print("Connecting to PostgreSQL database...")
    engine = create_async_engine(db_url, echo=False)

    backup_dir = os.path.expanduser("~/Desktop/wa_assistant_backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"wa_assistant_backup_{timestamp}.sql"
    filepath = os.path.join(backup_dir, filename)

    sql_statements = [
        f"-- WA Assistant Database Backup",
        f"-- Exported on: {datetime.now().isoformat()}",
        f"-- Host: dpg-d9o9a80ae00c73b0k170-a.frankfurt-postgres.render.com",
        "BEGIN;\n"
    ]

    try:
        async with engine.connect() as conn:
            metadata = MetaData()
            await conn.run_sync(metadata.reflect)

            for table_name in metadata.tables:
                table = metadata.tables[table_name]
                result = await conn.execute(select(table))
                rows = result.fetchall()

                if not rows:
                    continue

                sql_statements.append(f"-- Table: {table_name}")
                columns = [col.name for col in table.columns]
                col_names = ", ".join(columns)

                for row in rows:
                    vals = []
                    for val in row:
                        if val is None:
                            vals.append("NULL")
                        elif isinstance(val, (int, float)):
                            vals.append(str(val))
                        elif isinstance(val, bool):
                            vals.append("TRUE" if val else "FALSE")
                        else:
                            clean_str = str(val).replace("'", "''")
                            vals.append(f"'{clean_str}'")
                    
                    val_str = ", ".join(vals)
                    sql_statements.append(f"INSERT INTO {table_name} ({col_names}) VALUES ({val_str}) ON CONFLICT DO NOTHING;")
                
                sql_statements.append("")

            sql_statements.append("COMMIT;")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(sql_statements))

        file_size = os.path.getsize(filepath)
        print(f"[SUCCESS] Backup saved ({file_size} bytes): {filepath}")
        return filepath

    except Exception as exc:
        print(f"[ERROR] Backup failed: {exc}")
        return ""
    finally:
        await engine.dispose()


def main():
    asyncio.run(export_database())


if __name__ == "__main__":
    main()
