"""Worker 进程入口：python -m app.workers.main

与 API 进程共享同一份代码（同仓库、同 import），只是启动入口不同。
长流程、批量任务跑在这里，不占用 API 的请求处理能力。

当前注册 sourcing 域的采集长流程（CollectWorkflow + activities）。商品采集可能
拖很久、中途插件失败要能重试、断点恢复 —— 正是 Temporal 擅长、LangGraph/agent
loop 不负责的那一层。Temporal 未启动时本进程仅打印提示并空转，不影响 API 降级运行。
"""
import asyncio
import logging

from app.core.config import get_settings
from app.domains.sourcing.activities import ALL_ACTIVITIES
from app.domains.sourcing.workflows import CollectWorkflow

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def main():
    settings = get_settings()
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker

        client = await Client.connect(settings.temporal_host)
        worker = Worker(
            client,
            task_queue=settings.sourcing_task_queue,
            workflows=[CollectWorkflow],
            activities=ALL_ACTIVITIES,
        )
        log.info("sourcing worker 启动：task_queue=%s", settings.sourcing_task_queue)
        await worker.run()
    except Exception as e:
        log.warning("Temporal 不可达（%s），worker 空转。API 仍以降级模式处理采集任务。", e)
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
