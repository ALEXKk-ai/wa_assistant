"""Async SQLAlchemy engine and session factory.

Uses SQLite (aiosqlite driver) for v1. To move to Postgres later: swap
DATABASE_URL to a postgres+asyncpg:// DSN, add Alembic, and add a connection
pool size setting - no application code changes required, since nothing
outside this module and models.py knows the engine is SQLite.
"""
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables if they don't exist. For v1 this replaces Alembic;
    once you move to Postgres, replace this call with `alembic upgrade head`
    in your deploy step instead."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)

    try:
        from scripts.seed_demo_shops import seed
        await seed(run_init=False)
    except Exception as e:
        print(f"Auto-seed notification: {e}")


def _migrate_schema(connection) -> None:
    """Lightweight additive migrations for existing SQLite databases."""
    import sqlalchemy as sa

    inspector = sa.inspect(connection)
    tables = inspector.get_table_names()
    if "bookings" in tables:
        columns = {col["name"] for col in inspector.get_columns("bookings")}
        if "proposed_slot_start" not in columns:
            connection.execute(sa.text("ALTER TABLE bookings ADD COLUMN proposed_slot_start DATETIME"))
        if "proposed_slot_end" not in columns:
            connection.execute(sa.text("ALTER TABLE bookings ADD COLUMN proposed_slot_end DATETIME"))

    if "businesses" in tables:
        biz_columns = {col["name"] for col in inspector.get_columns("businesses")}
        if "address_text" not in biz_columns:
            connection.execute(sa.text("ALTER TABLE businesses ADD COLUMN address_text VARCHAR(500)"))
        if "extra_info_text" not in biz_columns:
            connection.execute(sa.text("ALTER TABLE businesses ADD COLUMN extra_info_text TEXT"))
        if "fulfillment_mode" not in biz_columns:
            connection.execute(sa.text("ALTER TABLE businesses ADD COLUMN fulfillment_mode VARCHAR(20) DEFAULT 'both'"))

    if "services" in tables:
        srv_columns = {col["name"] for col in inspector.get_columns("services")}
        if "deposit_flat_amount" not in srv_columns:
            connection.execute(sa.text("ALTER TABLE services ADD COLUMN deposit_flat_amount NUMERIC(10, 2)"))
        if "deposit_percentage" not in srv_columns:
            connection.execute(sa.text("ALTER TABLE services ADD COLUMN deposit_percentage NUMERIC(5, 2)"))

    if "products" in tables:
        prod_columns = {col["name"] for col in inspector.get_columns("products")}
        if "deposit_flat_amount" not in prod_columns:
            connection.execute(sa.text("ALTER TABLE products ADD COLUMN deposit_flat_amount NUMERIC(10, 2)"))
        if "deposit_percentage" not in prod_columns:
            connection.execute(sa.text("ALTER TABLE products ADD COLUMN deposit_percentage NUMERIC(5, 2)"))


@asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
