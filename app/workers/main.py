"""Worker 进程入口：python -m app.workers.main

procrastinate worker：跑 sourcing 后处理长流程（post_process：妙手 fetch→评分→翻译→上架）
+ cron 兜底（requeue_stale 每分钟扫掉队批重投）。替代旧的 workflow 引擎 worker。

与 API 进程共享同一份代码、同一 Postgres，只是启动入口不同：长流程跑这里，不占 API
请求处理能力。导入 sourcing.tasks 触发 task 注册；run_worker_async 自动调度 periodic。
"""
import asyncio
import logging

import app.domains.sourcing.tasks  # noqa: F401 —— 注册 post_process + requeue periodic
from app.shared.queue import queue_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def main() -> None:
    log.info("sourcing procrastinate worker 启动：queues=all")
    async with queue_app.open_async():
        await queue_app.run_worker_async(install_signal_handlers=True)


if __name__ == "__main__":
    asyncio.run(main())
