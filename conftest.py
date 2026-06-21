"""仓库根 conftest。

两件事：
1. 把仓库根加进 sys.path（隐式：本文件在根，pytest 自动加入），让 `import app` 可用。
2. ★ 测试库隔离：所有测试打独立库 xborder_test，绝不碰开发库 xborder。

隔离原理：在 import app 之前就把 DATABASE_URL/DATABASE_ADMIN_URL 写进 os.environ
（指向 xborder_test）。pydantic-settings 中 os.environ 优先级高于 .env，且 get_settings
是 lru_cache——这里先 cache_clear，之后 app.core.database 首次取到的就是测试库 url。
session 开始时 drop+create xborder_test、装 vector 扩展、镜像 app 角色授权、跑 alembic
到 head。开发库的数据（如 rules_kb 语料）从此不受测试影响。
"""
import os

from app.core.config import get_settings


def _swap_db(url: str, db: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + db


# 在任何 app 模块（含 app.core.database 建 engine）import 前切换到测试库。
_dev = get_settings()
os.environ["DATABASE_URL"] = _swap_db(_dev.database_url, "xborder_test")
os.environ["DATABASE_ADMIN_URL"] = _swap_db(_dev.database_admin_url, "xborder_test")
get_settings.cache_clear()


import psycopg  # noqa: E402 —— 在 env 切换后再 import


def _bootstrap_test_db() -> None:
    """建并迁移 xborder_test。必须在「模块级 import 时」跑，而非 fixture：
    各测试文件用 `pytestmark = skipif(not _db_reachable())`，在收集期就评估——
    那时若测试库还没建，会全被误 skip。故在 conftest import 时（早于收集）建好。
    DB 不可达则静默跳过（测试文件的 _db_reachable 随后自然 False → 优雅 skip，CI 无 PG 不挂）。"""
    admin_test = get_settings().database_admin_url.replace("postgresql+asyncpg://", "postgresql://")
    maint = _swap_db(admin_test, "postgres")  # 维护库：用它 drop/create xborder_test
    try:
        with psycopg.connect(maint, autocommit=True, connect_timeout=3) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'xborder_test' AND pid <> pg_backend_pid()"
            )
            c.execute("DROP DATABASE IF EXISTS xborder_test")
            c.execute("CREATE DATABASE xborder_test")
    except Exception:  # noqa: BLE001 —— 无 DB：让测试自然 skip
        return

    # 镜像 docker/postgres/init/01-init.sql 的 per-db 部分（app 角色 cluster 级已存在）。
    with psycopg.connect(admin_test, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("GRANT CONNECT ON DATABASE xborder_test TO app")
        c.execute("GRANT USAGE ON SCHEMA public TO app")
        c.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app"
        )
        c.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO app"
        )

    # 跑迁移到 head（env.py 用 database_admin_url=测试库）。
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


_bootstrap_test_db()
