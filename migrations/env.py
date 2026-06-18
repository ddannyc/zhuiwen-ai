"""Alembic 环境。

连接来源：app.core.config 的 database_admin_url（超级权限连接）——
迁移/RLS 策略必须用 admin 角色跑，运行期业务才用受 RLS 约束的 app 角色。
URL 里的 +asyncpg 驱动换成同步 +psycopg，供 Alembic 同步执行。

迁移为手写（op.execute 原始 SQL）：RLS / DEFAULT current_setting / FORCE RLS /
策略无法 autogenerate，故 target_metadata 留空，不做自动比对。
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # 手写迁移，不 autogenerate


def _sync_admin_url() -> str:
    # postgresql+asyncpg://... → postgresql+psycopg://...（Alembic 用同步驱动）
    return get_settings().database_admin_url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_admin_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_admin_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
