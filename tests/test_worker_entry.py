"""Phase5 T5.1：worker 入口改 procrastinate（替代 Temporal worker）。

验收：worker 入口不再 import/引用 Temporal，跑 procrastinate run_worker_async；
sourcing task（post_process + requeue periodic）注册在 queue_app 上。
"""
import inspect


def test_worker_main_uses_procrastinate_not_temporal():
    import app.workers.main as m

    src = inspect.getsource(m)
    assert "temporal" not in src.lower()
    assert "run_worker_async" in src


def test_sourcing_tasks_registered_on_queue():
    import app.domains.sourcing.tasks  # noqa: F401 —— 触发注册

    from app.shared.queue import queue_app

    assert "sourcing.post_process" in queue_app.tasks
    assert "sourcing.requeue_stale" in queue_app.tasks
