"""多平台批量刊登的 durable workflow（Temporal 骨架）。

体现长流程编排该有的能力：每个平台一个 activity，自带重试；
workflow 状态由 Temporal 持久化，进程崩了也能恢复到当前步骤。
这与 listing/agent.py 里的 LangGraph 是两层不同的东西 —— 别混用。
"""
from datetime import timedelta

# from temporalio import workflow, activity


# @activity.defn
# async def publish_to_platform(tenant_id: str, listing_id: str, platform: str) -> str:
#     """调用某平台（Amazon/eBay/Temu...）的刊登 API。失败由 Temporal 按策略重试。"""
#     ...
#     return "published"


# @workflow.defn
# class BulkPublishWorkflow:
#     @workflow.run
#     async def run(self, tenant_id: str, listing_id: str, platforms: list[str]) -> dict:
#         results = {}
#         for platform in platforms:
#             results[platform] = await workflow.execute_activity(
#                 publish_to_platform,
#                 args=[tenant_id, listing_id, platform],
#                 start_to_close_timeout=timedelta(minutes=5),
#                 # 重试策略：跨境平台 API 经常限流/超时，重试是常态
#                 retry_policy=workflow.RetryPolicy(maximum_attempts=5),
#             )
#         return results


# 说明：Temporal 的 workflow 跨进程运行，租户上下文不能靠 ContextVar 传递，
# 必须把 tenant_id 作为显式参数贯穿 workflow 和 activity（如上）。
# 在 activity 内部访问 DB 时，用 tenant_id 调用 current_tenant_id.set(...) 后再开 session。
