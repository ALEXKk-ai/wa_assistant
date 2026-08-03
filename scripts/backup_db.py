"""Local Database Backup Tool for PostgreSQL.

Reads DATABASE_URL from settings / .env and creates a timestamped SQL dump
in the wa_assistant_backups directory on your Desktop.

Run manually:
    python -m scripts.backup_db

Or schedule with Windows Task Scheduler / Cron.
"""
import os
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

from app.config import get_settings


def create_backup() -> str:
    settings = get_settings()
    db_url = settings.database_url

    # Strip async driver prefix if present (postgresql+asyncpg:// -> postgresql://)
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "")

    parsed = urlparse(db_url)

    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    user = parsed.username or ""
    password = parsed.password or ""
    dbname = parsed.path.lstrip("/")

    backup_dir = os.path.expanduser("~/Desktop/wa_assistant_backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"wa_assistant_backup_{timestamp}.sql"
    filepath = os.path.join(backup_dir, filename)

    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password

    # Find pg_dump executable in common Windows installation paths if not on PATH
    pg_dump_cmd = "pg_dump"
    possible_paths = [
        r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe",
        r"C:\Program Files\PostgreSQL\15\bin\pg_dump.exe",
        r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
        r"C:\Program Files\DBeaver\plugins\org.jkiss.dbeaver.ext.postgresql\pg_dump.exe",
    ]
    for path in possible_paths:
        if os.path.exists(path):
            pg_dump_cmd = f'"{path}"'
            break

    cmd = f'{pg_dump_cmd} -h {host} -p {port} -U {user} -d {dbname} -f "{filepath}"'

    print(f"Connecting to {host}...")
    print(f"Creating backup file: {filepath}")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            print(f"✅ Success! Backup saved ({file_size} bytes).")
            return filepath
        else:
            print(f"⚠️ pg_dump failed: {result.stderr}")
            print("Notice: Make sure pg_dump is installed or added to system PATH.")
            return ""
    except Exception as exc:
        print(f"❌ Backup error: {exc}")
        return ""


if __name__ == "__main__":
    create_backup()
