"""procrastinate 队列 schema（替代 Temporal）。

注入 procrastinate 自带 schema（4 表 + 2 枚举 + 触发器/函数），供后处理队列用。
schema SQL 取自 procrastinate 包（版本随依赖走），不手抄——升级 procrastinate 时
新增一条迁移调新版 get_schema，不改本文件。

授权：迁移以 postgres（admin）跑，建出的表/序列经 init.sql 的 ALTER DEFAULT PRIVILEGES
自动授 app；函数 EXECUTE 默认 PUBLIC。仍显式 GRANT 兜底（防 default privileges 缺失）。
procrastinate 表不挂 RLS——它是基建队列，租户由 job 参数携带、task 内 set_config 走 RLS。

schema 是多语句大 blob，SQLAlchemy/alembic op.execute 走 psycopg3 扩展协议只接单语句
（报 f405）。改用 procrastinate 自带的 SchemaManager.apply_schema 经 SyncPsycopgConnector
施加（procrastinate 官方路径，自行处理多语句 + % 转义），与 alembic 事务分离、独立提交。

Revision ID: 0004_procrastinate
Revises: 0003_sourcing
"""
from alembic import op

from app.core.config import get_settings

revision = "0004_procrastinate"
down_revision = "0003_sourcing"
branch_labels = None
depends_on = None


def _admin_conninfo() -> str:
    # 迁移以 admin（postgres）跑——建出的表经 init.sql 默认权限自动授 app。
    return get_settings().database_admin_url.replace("postgresql+asyncpg://", "postgresql://")


def upgrade() -> None:
    import procrastinate
    from procrastinate.schema import SchemaManager

    connector = procrastinate.SyncPsycopgConnector(conninfo=_admin_conninfo())
    connector.open()
    try:
        SchemaManager(connector=connector).apply_schema()
    finally:
        connector.close()

    # 显式授权兜底（business app 角色跑 worker：fetch/defer 需读写表 + 用序列）。
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app")


def downgrade() -> None:
    # 1) 动态删所有 procrastinate_* 例程（函数/过程），CASCADE 连带触发器。
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT oid::regprocedure AS sig FROM pg_proc WHERE proname LIKE 'procrastinate\\_%'
          LOOP
            EXECUTE 'DROP ROUTINE IF EXISTS ' || r.sig || ' CASCADE';
          END LOOP;
        END $$;
        """
    )
    # 2) 删表（连带行类型）。
    op.execute(
        "DROP TABLE IF EXISTS procrastinate_events, procrastinate_periodic_defers, "
        "procrastinate_jobs, procrastinate_workers CASCADE"
    )
    # 3) 删表后残留的 procrastinate_* 类型——枚举 + 独立复合类型（如 *_job_to_defer_v1）。
    #    表行类型已随表消失，此处只剩需显式 DROP 的。不写死名字，随版本变化自适应。
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT typname FROM pg_type WHERE typname LIKE 'procrastinate\\_%'
          LOOP
            EXECUTE 'DROP TYPE IF EXISTS ' || quote_ident(r.typname) || ' CASCADE';
          END LOOP;
        END $$;
        """
    )
