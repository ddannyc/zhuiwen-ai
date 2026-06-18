"""渠道适配器接缝（本期空壳，不实现）。

将来接飞书/Lark 等多渠道时，适配器职责：
  1. 解析渠道事件（飞书 im.message.receive_v1），去重、剥占位符取纯文本；
  2. 把 渠道 app_id / chat_id 映射到 tenant_id（设置租户上下文，使 RLS 生效）
     与 user_id（会话归属人）；
  3. 复用 ChatService.converse(conversation_id, text) —— 同一个 agent 大脑，
     不另起逻辑（对标旧 feishu_event 复用 agent_act）；
  4. 把 reply 回发渠道，并按需回灌网页。

本期只留接缝：渠道→租户/用户映射与 webhook 校验后续实现。
"""
