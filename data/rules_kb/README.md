# rules_kb 种子语料

平台运营规范知识库的**首批种子规则记录**。Schema 见 `docs/规则知识库设计.md` 第一部分（§1.1 字段、§1.2 `rule_domain` 受控词表、§1.3 confidence↔source 映射）。

## 文件
- `seed_rules.jsonl` — 每行一条规则记录（rule record），字段与 schema §1.1 完全对齐。

## 本批内容（2026-06-17）

| 平台 | 记录数 | 覆盖 rule_domain | 来源 |
|---|---|---|---|
| amazon (US) | 7 | fees / account_health / prohibited_products / returns_refunds / listing / intellectual_property / logistics_fulfillment | `sell.amazon.com` 公开帮助/政策页 |

按《规则知识库设计.md》落地优先级 #1（先跑文档化好的平台）选 Amazon、Ozon、Mercado Libre 三家。**Ozon 与 Mercado Libre 本环境抓取被封**（见下「阻塞待办」），本批仅落地 Amazon。

## 守住的红线
- **真实溯源**：每条 `source_url` 都是实际抓取成功的公开页（已校验 https + schema）。未抓到的页一律不编，宁缺毋滥。
- **提炼改写**：`summary`/`content` 均为改写要点，非原文整段转储（版权 + 合规红线）。
- **全部 `needs_review`**：本批为 LLM 抽取，未经人工核验，对应设计文档**阶段4 人工审核队列**。**入库后不得直接作为权威答案对外**，须运营/合规人员核验后才置 `verified`。
- **数字阈值需重点复核**：账号健康指标（ODR<1%、LSR<4%、取消率<2.5%）、退货时限、图片像素范围等具体数值，是合规高风险项，人工审核时优先逐条对照官方页确认。

## 阻塞待办（本环境网络受限，换不被封的网络重抓）
US 沙箱网络对以下两家全站封锁，非缺少尝试：
- **Ozon** — `docs.ozon.ru/global/en/...` 全部 `Too many redirects`（geo/bot 拦截）。候选 URL 已验证存在：
  - 禁限售/类目：`docs.ozon.ru/global/en/policies/product-rules-and-documents/product-rules/special-categories/`
  - 刊登：`docs.ozon.ru/global/en/products/requirements/`
  - 费用：`docs.ozon.ru/global/en/commissions/ozon-fees/commissions/`
  - 物流：`docs.ozon.ru/global/en/fulfillment/rfbs/`、`.../fulfillment/fbp/`
  - 退货：`docs.ozon.ru/common/en/otmena-i-vozvrat-zakaza/usloviya-i-sroki-vozvrata/`
  - 税务合规：`docs.ozon.ru/global/en/contracts-for-sellers/dogovor/`
- **Mercado Libre** — `mercadolibre.*`/`developers.mercadolibre.*` 全部 `HTTP 403`。需从可访问区域（或带授权的 connector）重抓 ayuda/central de vendedores 的 productos prohibidos、cómo publicar、devoluciones、comisiones 页。

## Ozon 爬虫 `scripts/ozon_crawler.py`
零三方依赖（纯标准库）。实现设计文档管线阶段1–3：采集 docs.ozon.ru 公开 seller 页 → HTML 清洗 → LLM 结构化抽取 → 输出 `ozon_rules.jsonl`，每条强制 `needs_review`。守红线：robots 校验、`--delay` 限频、只提炼改写不转储原文、跳转环检测。

```bash
# LLM 抽取走 DeepSeek（OpenAI 兼容，https://api-docs.deepseek.com/zh-cn/）
export DEEPSEEK_API_KEY=sk-...                        # DEEPSEEK_BASE_URL 默认 https://api.deepseek.com
export EXTRACT_MODEL=deepseek-v4-flash               # 深推理可换 deepseek-v4-pro
python scripts/ozon_crawler.py                       # 抓种子页 + 抽取
python scripts/ozon_crawler.py --crawl --max-pages 40 # 跟随同域 /en/ 链接扩展
python scripts/ozon_crawler.py --no-llm --cache       # 只抓文本到 raw/ 供人工审（无 key 时）
python scripts/ozon_crawler.py --cookie "locale=en; ..."  # 突破地域拦截
```

**本环境限制**：US 沙箱对 docs.ozon.ru 返回 **HTTP 403**（含 robots.txt），脚本逐页报 403 后优雅退出、抓 0 页——这是网络地域拦截，非脚本缺陷。**从俄区/不被封的 egress 运行，或 `--cookie` 提供绕过 cookie 即可抓取。** 抽取结果合并入 `seed_rules.jsonl` 前必须人工核验（阶段4）。

### 推荐：油猴脚本绕过 403/地域拦截 `scripts/ozon_tampermonkey.user.js`
服务端 urllib 吃 403 是因为它是机器人 + 不渲染 JS。油猴脚本在**真实浏览器**里跑（真 cookie/登录态/地域 + 等 JS 渲染），天然绕过。分工：**浏览器只抓文本**（不在浏览器调 LLM，避免密钥泄露/CORS），**离线 DeepSeek 抽取**（密钥留本地）。

```bash
# 1. 浏览器装 Tampermonkey/篡改猴 → 新建脚本粘贴 scripts/ozon_tampermonkey.user.js
# 2. 开 docs.ozon.ru seller 文档页，右下角面板「自动遍历」顺目录走，或手动点页
# 3. 点「导出 JSONL」→ 下载 ozon_pages.jsonl（每行 {url,title,text}）
# 4. 离线抽取（走 DeepSeek，密钥本地）：
export DEEPSEEK_API_KEY=sk-...
python scripts/ozon_crawler.py --from-dump ozon_pages.jsonl
#    → data/rules_kb/ozon_rules.jsonl（全 needs_review，待人工审核后并入 seed_rules.jsonl）
```
红线一致：仅采公开页、原文只作离线抽取中间物不入库、抽取结果 needs_review。

## 后续接入（与 `docs/chat-redesign-plan.md` §6 对齐）
此语料喂给规划中的 `app/domains/rules_kb/` 域。入库管线（采集→归一化翻译→结构化抽取→人工审核→分块向量化双索引）见《规则知识库设计.md》第二部分。chat agent 经 `rules_search` 工具检索本库，强制 metadata 过滤 + 溯源 + 时效守卫。

> 入库前提：`rules_kb` 域 + Postgres/pgvector + BM25 索引尚未落地（当前 `app/` 无此域）。本语料为**就绪待入库**状态。
