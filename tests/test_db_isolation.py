"""测试库隔离验证：测试必须打 xborder_test，绝不碰开发库 xborder。"""
from app.core.config import get_settings


def test_active_db_is_test_db():
    s = get_settings()
    assert s.database_url.endswith("/xborder_test"), s.database_url
    assert s.database_admin_url.endswith("/xborder_test"), s.database_admin_url


def test_engine_points_at_test_db():
    from app.core.database import engine

    assert engine.url.database == "xborder_test"
