"""Worker 进程入口：python -m app.workers.main

与 API 进程共享同一份代码（同仓库、同 import），只是启动入口不同。
长流程、批量任务、定时任务跑在这里，不占用 API 的请求处理能力。

下面用 Temporal 作为 durable workflow 引擎的骨架示意。
跨境电商的"多平台批量刊登"是典型场景：可能跑几分钟，中途某个平台
API 失败要能重试、能从断点恢复、能记录到哪一步 —— 这正是 Temporal 擅长的，
也是 LangGraph / agent loop 不负责的那一层。
"""
import asyncio

# from temporalio.client import Client
# from temporalio.worker import Worker
# from app.domains.publishing.workflows import BulkPublishWorkflow
# from app.domains.publishing.activities import publish_to_platform, mark_done


async def main():
    # client = await Client.connect("localhost:7233")
    # worker = Worker(
    #     client,
    #     task_queue="publishing",
    #     workflows=[BulkPublishWorkflow],
    #     activities=[publish_to_platform, mark_done],
    # )
    # await worker.run()
    print("worker started (configure Temporal client to activate)")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
