# 飞猴 Chat 对话功能 — 业务逻辑分析

> 反推自 `tmp/zhuiwen_web.py`。「飞猴」是聚焦 Ozon 与 TikTok Shop 跨境电商的 AI 助手（选品、竞品分析、Listing、定价、客服话术）。

## 1. 总览

对话不是单纯问答，而是**自然语言操控层**：用户一句话，系统判断是「闲聊问答」还是「在系统里执行动作」，路由后执行并把结果以 Markdown 回灌到对话窗。

三条核心入口（HTTP POST，均需登录态，飞书回调除外）：

| 接口 | 处理函数 | 用途 |
|------|----------|------|
| `POST /api/agent/act` | `agent_act()` | 主入口。自然语言 → 路由 → 执行动作或闲聊 |
| `POST /api/chat` | `chat()` | 纯电商问答（不操作系统） |
| `POST /api/chat/vision` | `chat_vision()` | 上传图片 → Qwen-VL 视觉理解（选品/listing 视角） |
| `POST /api/plan` | `agent_plan()` | 旁路：为这句话生成可执行任务计划（并行调用） |
| `POST /api/feishu/event` | `feishu_event()` | 飞书收消息 → 复用 `agent_act` → 回飞书 |

全部 LLM 调用统一走**阿里通义千问（DashScope 兼容模式）**，由 `_ali_chat()` 收口，不再依赖 DeepSeek/网关。需 `DASHSCOPE_API_KEY`；默认对话模型 `qwen-plus`（可配 `ALI_CHAT_MODEL`），视觉模型 `qwen3-vl-plus`（可配 `QWEN_VL_MODEL`）。

---

## 2. 底层 LLM 调用 `_ali_chat()`

`_ali_chat(messages, max_tokens, temperature, model, timeout)` — 唯一对外 LLM 出口。

- 读 `DASHSCOPE_API_KEY`，无 key 直接返回 `""`（上层据此报「未配置/无额度」）。
- POST `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`，OpenAI 风格 body。
- 取 `choices[0].message.content`；任何异常吞掉返回 `""`（静默降级，不抛栈）。

衍生封装：
- `chat()` — 闲聊，温度 0.6，max 2200，带系统人设 `_CHAT_SYS`。
- `_agent_llm_json()` — 路由/结构化提取，温度 0.1，输出走 `_loose_json()` 宽松解析为 JSON 或 None。
- `_agent()` — 分析报告类，温度 0.6，max 2600。

---

## 3. 纯问答 `chat()`

```
chat(message, session, history=None)
```

业务规则：
1. 注入系统人设 `_CHAT_SYS`（务实、结论先行、简体中文、必要时表格）。
2. 拼接最近 **6 条** history（仅 user/assistant，每条截断 **1500** 字）。
3. 当前 message 截断 **3000** 字。
4. 温度 0.6、max_tokens 2200、超时 120s。
5. 空返回 → `{ok:false, error:"阿里模型无返回，请确认 Key 与额度"}`；否则 `{ok:true, reply}`。

无服务端会话持久化——history 由前端携带（见 §8）。

---

## 4. 视觉问答 `chat_vision(b)`

- 入参 `images`（dataURL 数组），最多取 **3** 张，但**只分析第 1 张**（多图时在 prompt 里注明「共 N 张，先析第 1」）。
- 无 message 时用默认 prompt：从跨境电商选品/listing 优化角度描述商品图。
- 调 `studio.qwen_vl(key, prompt, img, model, timeout=90)`。
- 无 key → 报「未配置阿里云百炼 Key」；无返回 → 「图片理解无返回，请重试或换图」。

---

## 5. 核心：指令路由 `agent_act()`

这是对话的大脑，把一句话变成系统动作。

### 5.1 启发式短路（先于 LLM）

若消息**不含「采集箱」**且命中下列任一正则，直接判为「从 0 采集/选品」，跳过路由模型（因路由模型常把这类误判为 chat 去反问）：
- `(自动|全自动|帮我|根据|按|去).{0,14}(采集|选品|找货|采品)`
- `采集?\s*\d+\s*个`
- 含「采集」且含（关键词/蓝海/热销/上架）任一

命中后用 `_AUTOCOLLECT_SYS` 提关键词（重试 2 次，兼容模型返回 dict/数组/嵌套三种形态），再 `_parse_collect_params` 解析开关，调 `_do_auto_collect`。

### 5.2 LLM 路由

未短路则 `_agent_llm_json(_ACT_SYSTEM, message, history)`，要求模型输出严格 JSON：

```json
{"action":"动作", "params":{...}, "say":"给用户的简短中文说明"}
```

`say` 会作为前缀 `pre` 拼在结果前。无法解析时 `action` 默认 `chat`。

### 5.3 动作表（业务能力清单）

| action | 业务含义 | 关键 params |
|--------|----------|-------------|
| `box.count` | 采集箱商品数 | — |
| `box.list` | 列采集箱（可按 keyword 过滤，最多 30 条） | `limit`, `keyword` |
| `box.delete_chinese` | 删除标题仍是中文（未翻译）的商品 | — |
| `box.delete_all` | 清空采集箱 | — |
| `box.translate` | 翻译采集箱标题/图片并写回 | `scope`(all/chinese), `lang`, `images` |
| `box.list_tiktok` | 采集箱商品上架 TikTok | `scope`, `auto`(是否直接发布) |
| `pipeline` | 一条龙：翻译 + 上架 | `scope`, `lang`, `images`, `auto` |
| `auto_collect` | 从 0 全自动采集（下发采集任务给插件） | `keywords`, `perKw`, `topN`, `score`, `translate`, `lang`, `listTiktok`, `tkAuto` |
| `analyze` | 选品/竞品分析 | `keyword`, `type` |
| `chat` | 普通电商问答（兜底） | — |

`box.*` 范围由 `scope != "all"` 判断是否只取中文标题（`_box_all_ids(chinese_only=...)`）。

上架依赖默认 TikTok 店铺 `_default_tk_shop()`，未配则提示去模板配置选店铺；站点默认 `MY`（取 `templates.claim.site`）。上架结果回报 预填/发布/失败/总数；未开 `auto` 提示去后台确认类目后发布。

### 5.4 兜底

任一动作执行抛异常 → `执行「动作」时出错：...`。无匹配动作 → 落回 `chat()` 闲聊。

---

## 6. 全自动采集 `_do_auto_collect()`

最复杂的动作，融合「实时热销榜 + 用户方向 + 模型提词 + 参数解析 + 任务下发」。

流程：
1. **关键词**：取 params.keywords（≤10）。若消息含 `热销|热卖|爆款|蓝海|榜|趋势|热门|大盘|实时|数据`，则：
   - `_site_from_msg` 从文案识别站点（马来/印尼/美国… 默认美国）。
   - `_hot_to_keywords(site, focus)`：拉真实 TK 热销榜 `hot_query(sort=daily_sales)`，连同用户方向喂 Qwen，**提炼能在 1688 直接搜到的具体产品词**（强约束：禁用「热销商品/蓝海产品/爆款」等抽象词）。
   - 生成 `hot_md`：展示 Top10 真实数据 + 提炼的关键词，回灌对话。
2. **参数**：`_parse_collect_params(message, kws)` 用正则从原话解析开关（覆盖路由模型可能漏项）：
   - `perKw`（各/每词 N 个，默认 10）、`topN`（前/top N）、`lang`（马来/俄/英）。
   - `translate`(含「翻译")、`transImages`(图片翻译)、`optimize`(默认开，「不优化」关)、`oneClick`(一键采集)、`listTiktok`(上架/发布/tk)、`tkAuto`(直接/自动/立即发布)。
3. **蓝海速评**：若消息含 `分析|蓝海|机会|可行|竞争`，再调一次 Qwen 对每个关键词一行点评 → `ana_md`。
4. 组装 `job`（含 threshold、score、fast 等），`collect_job_create(job)` 入队。
5. 返回：`pre + hot_md + ana_md + "✅ 已下发采集任务…插件会按默认配置自动执行 采集→评分→优化→翻译→上架"`。

> 注意：服务端**不执行**采集，只下发任务到队列；真正干活的是浏览器插件轮询。

### 采集任务队列（内存）

`_COLLECT_JOBS`（内存数组，上限 50），插件侧轮询消费：

| 接口 | 函数 | 作用 |
|------|------|------|
| `POST /api/collect-job/create` | `collect_job_create` | 入队，status=pending |
| `POST /api/collect-job/poll` | `collect_job_poll` | 取第一个 pending，置 running 返回 |
| `POST /api/collect-job/done` | `collect_job_done` | 回填 result，置 done |

---

## 7. 智能分析 `analyze()`

`analyze(kw, atype)` → 按 `atype` 取模板 prompt（`ANALYSIS_PROMPTS`），调 `_agent()` 出 Markdown 报告。

支持类型：`blue_ocean`(蓝海挖掘)、`voc`(差评 VOC 量化)、`feasibility`(可行性，默认)、`compare`(竞品对比)、`listing`(标题+五点卖点中英)、`pricing`(定价利润测算)。统一要求「结论先行、表格、可执行」。

`_score_candidates()` 是采集流里的选品打分（TikTok 趋势热度25/利润25/视觉20/物流15/竞争15，总分100），优先直连，失败再走 OpenClaw 网关兜底。

---

## 8. 前端对话流（`send()`）

1. 支持附件：图片（缩放为 dataURL）、文本文件（取前 8000 字塞进 message）、其他文件（仅记文件名）。
2. **有图** → `/api/chat/vision`。
3. **无图** → 并行两请求：
   - `/api/plan`（旁路，生成任务计划，失败忽略）。
   - `/api/agent/act`，带 `history: chatHist.slice(-6)`。
4. 渲染：`action !== 'chat'` 时显示「⚡ 已执行」徽标；失败显「执行失败」。
5. history 由前端维护 `chatHist`，每轮 push user/assistant，`saveSession()` 存本地。
6. 动作后联动 UI：`box.*`/`pipeline`→ 切采集箱 tab 并刷新；`analyze`→ 分析 tab；`auto_pipeline`→ 弹采集确认框（可改开关，10s 倒计时自动开始）。

> 服务端无会话存储，history 完全由浏览器携带 → 多端不同步、刷新依赖本地存储。

---

## 9. 飞书（Lark）接入

网页配置 App → 飞书 webhook 收消息 → **复用 `agent_act`** → 回飞书 + 同步网页。

- `feishu_event(b)`：处理 `url_verification`(challenge) 与 `im.message.receive_v1`；用 `FEISHU_VERIFY_TOKEN` 校验，`event_id` 去重（`_FEISHU_SEEN`，上限 500）。
- 去掉 `@_user_\d+` 占位 → 取纯文本 → `agent_act(text)` → `_feishu_send(chat_id, reply)`。
- `_feishu_token()` 缓存 tenant_access_token（提前 120s 过期）。
- `_FEISHU_LOG`（内存上限 200）记录会话，`POST /api/feishu/messages?since=` 供网页轮询同步。

> 飞书走的是**同一个智能体大脑**，所以「采集/翻译/上架/分析」在飞书里同样可用，且回灌网页。

---

## 10. 关键约束与隐患

- **无服务端持久化**：会话历史、采集任务队列、飞书日志全是**内存数组**，进程重启即丢；多副本不共享。
- **静默降级**：`_ali_chat` 吞所有异常返回空串，错误信息对用户是「无返回/无额度」，排障需看后台。
- **截断策略**：闲聊 history 6 条 ×1500 字；路由 4 条 ×800 字；文本附件 8000 字；当前消息 3000 字。
- **路由不稳兜底**：启发式正则短路 + 模型多形态兼容 + 正则参数解析三层叠加，弥补路由模型把「采集」误判为「闲聊反问」。
- **采集是异步外包**：服务端只下发 job，执行靠浏览器插件轮询，对话里的「✅ 已下发」≠ 已完成。
- **统计** `_usage_bump`：chat 成功计 1 次对话，token 数粗估为 `len(reply)//3`。
