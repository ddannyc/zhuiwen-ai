"""数据库初始化（对标 rails db:create + 角色/授权）。幂等，可重复跑。

职责（迁移之外的一次性准备）：
  1. 建业务库 xborder（owner=postgres）
  2. 建受 RLS 约束的业务角色 app（非超级用户、无 BYPASSRLS）
  3. 授 app 在 public schema 的 DML 权 + 默认权限
     （之后 alembic 以 postgres 建的表会自动授予 app）

随后跑：uv run alembic upgrade head

连接取自 app.core.config.database_admin_url（postgres 超级权限）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from psycopg import sql

from app.core.config import get_settings


def _admin_dsn(dbname: str) -> str:
    # postgresql+asyncpg://postgres:postgres@host:5432/xborder → psycopg DSN，指定库
    url = get_settings().database_admin_url
    url = url.replace("postgresql+asyncpg://", "postgresql://").replace("+psycopg", "")
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


def _biz_role_pw() -> tuple[str, str, str]:
    # 从 database_url 解析业务角色名/密码/库名：postgresql+asyncpg://app:app@host/xborder
    url = get_settings().database_url
    tail = url.split("://", 1)[1]
    creds, hostpart = tail.split("@", 1)
    user, _, pw = creds.partition(":")
    dbname = hostpart.rsplit("/", 1)[1]
    return user, pw, dbname


def main() -> int:
    role, pw, dbname = _biz_role_pw()

    # 1) 连 maintenance 库 postgres，建库 + 角色（autocommit：CREATE DATABASE 不能在事务里）
    with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if not cur.fetchone():
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(pw)))
            print(f"✓ 建角色 {role}")
        else:
            print(f"· 角色 {role} 已存在")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if not cur.fetchone():
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER postgres").format(
                sql.Identifier(dbname)))
            print(f"✓ 建库 {dbname}")
        else:
            print(f"· 库 {dbname} 已存在")

    # 2) 连业务库，授权 + 默认权限（alembic 之后建的表自动授予 app）
    with psycopg.connect(_admin_dsn(dbname), autocommit=True) as conn:
        cur = conn.cursor()
        rid = sql.Identifier(role)
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(rid))
        cur.execute(sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(rid))
        cur.execute(sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(rid))
        print(f"✓ 授 {role} public schema DML 权 + 默认权限")

    print("\n下一步：uv run alembic upgrade head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
