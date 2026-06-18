// ==UserScript==
// @name         Ozon 运营规范采集器 → rules_kb
// @namespace    xborder.rules_kb
// @version      1.3
// @description  在真实浏览器里采集 Ozon 公开 seller 文档（过服务端 403/地域拦截 + 等 JS 渲染），导出 JSONL 供离线 DeepSeek 抽取。仅抓公开页、只导文本不调 LLM。锁定卖家英文帮助区。
// @match        https://global-help.ozon.com/en*
// @match        https://docs.ozon.ru/global/en/*
// @run-at       document-idle
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// ==/UserScript==

/*
 v1.2 锁定卖家文档区 /global/en/：
   - @match 收窄到 https://docs.ozon.ru/global/en/*（job-ff-help 等劳务/非卖家区不再加载）。
   - SCOPE 常量：采集 + 链接发现双重过滤，只收 pathname 以 /global/en/ 开头的页。
   - 删掉 v1.1 的「放宽收任意 ozon.ru 路径」兜底——正是它把爬虫拖进 job-ff-help 仓库工人文档。

 v1.1 修复「脚本不运行」：
   - @grant 改用 GM_*（TM 沙箱执行，绕过 Ozon 页面 CSP；@grant none 会被 CSP 拦掉静默不跑）。
   - 多 @match（docs / seller-edu / *.ozon.ru），防文档站跳别的子域导致不匹配。
   - SPA 路由检测：docs 站是单页应用，仅靠 document-idle 只触发一次；这里 hook history + 轮询
     URL 变化，路由切换也重新采集。
   - 控制台 banner：装好刷新页，F12 Console 看到 "[rules_kb] 已加载" 即确认在跑。

 用法：
  1. Tampermonkey/篡改猴 → 新建脚本，粘贴本文件，保存，确认脚本「已启用」。
  2. 打开 Ozon seller 文档页（如 docs.ozon.ru/global/en/...），右下角出现面板=在跑。
     看不到面板 → F12 Console 查 "[rules_kb]" 日志 / 报错；多半是 host 没匹配或 TM 没启用。
  3. 「自动遍历」顺左侧目录逐页走，或手动点页；「导出 JSONL」下载 ozon_pages.jsonl。
  4. 离线抽取：export DEEPSEEK_API_KEY=sk-...; python scripts/ozon_crawler.py --from-dump ozon_pages.jsonl

 红线：仅采公开帮助页；只导文本不在浏览器调 LLM；原文仅离线抽取中间物不入库；结果 needs_review。
*/
(function () {
  "use strict";
  console.log("[rules_kb] 已加载 @", location.href);

  const BUF = "ozon_rk_buffer", QUEUE = "ozon_rk_queue", AUTO = "ozon_rk_auto", SEEDED = "ozon_rk_seeded";

  // 目标区锁：按 host 配前缀，只采卖家英文帮助区，挡掉 job-ff-help（仓库工人）等非卖家区
  const SCOPE = {
    "global-help.ozon.com": "/en",        // 新卖家帮助站
    "docs.ozon.ru": "/global/en/",        // 旧卖家文档站
  };
  const inScope = (host, path) => {
    const p = SCOPE[host];
    return !!p && path.startsWith(p);
  };
  // 归一化 URL：丢 query（站点跳转会自动加 ?region=...&__rr=1）+ 去尾斜杠。
  // buf key、队列、链接发现必须用同一形式，否则已访问页查不中 → 反复重入队 → 死循环。
  const normURL = (href) => {
    try {
      const u = new URL(href, location.href);
      const path = u.pathname.length > 1 ? u.pathname.replace(/\/+$/, "") : u.pathname;
      return u.origin + path;
    } catch (e) { return href; }
  };

  // 存储：优先 GM_*（跨沙箱稳），回退 localStorage
  const hasGM = typeof GM_setValue !== "undefined";
  const get = (k, d) => {
    try {
      if (hasGM) { const v = GM_getValue(k); return v === undefined ? d : JSON.parse(v); }
      const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v);
    } catch (e) { return d; }
  };
  const set = (k, v) => { const s = JSON.stringify(v); hasGM ? GM_setValue(k, s) : localStorage.setItem(k, s); };
  const del = (k) => { hasGM ? GM_deleteValue(k) : localStorage.removeItem(k); };

  function extractText() {
    // 尝试多种选择器找到主容器
    const selectors = [
      "main", 
      "article", 
      "[role='main']",
      "[class*='content']",
      "[class*='Content']",
      "[class*='article']",
      "[class*='Article']",
      "[class*='markdown']",
      "[class*='doc']",
      "[class*='page']",
      "[class*='Page']",
      "body"
    ];
    
    let main = null;
    for (const sel of selectors) {
      main = document.querySelector(sel);
      if (main && main.innerText && main.innerText.length > 50) {
        console.log("[rules_kb] 用选择器找到内容:", sel);
        break;
      }
    }
    
    if (!main) main = document.body;
    
    const clone = main.cloneNode(true);
    clone.querySelectorAll("script,style,nav,header,footer,noscript,svg,form,button,[class*='nav'],[class*='Nav'],[class*='sidebar'],[class*='Sidebar'],[class*='breadcrumb'],[class*='toc'],[class*='Toc'],[class*='menu'],[class*='Menu']").forEach(n => n.remove());
    const text = (clone.innerText || "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
    console.log("[rules_kb] 提取结果 - 选择器:", main.tagName, "文本长度:", text.length);
    return text;
  }

  function collectCurrent() {
    if (!inScope(location.host, location.pathname)) {
      console.log("[rules_kb] ⛔ 非卖家帮助区，跳过采集:", location.host + location.pathname);
      return false;
    }
    const text = extractText();
    console.log("[rules_kb] 提取文本长度:", text.length);
    if (text.length < 150) {
      console.log("[rules_kb] ⚠ 文本过短（<150），跳过采集");
      return false;
    }
    const buf = get(BUF, {});
    const key = normURL(location.href);
    buf[key] = {
      url: key,
      title: (document.title || "").replace(/\s*[|–-].*$/, "").trim(),
      text: text,
      collected_at: new Date().toISOString(),
    };
    set(BUF, buf);
    console.log("[rules_kb] ✓ 采集成功：" + buf[key].title);
    return true;
  }

  function docLinks() {
    const out = new Set();
    console.log("[rules_kb] 搜索链接（限卖家帮助区）...");

    // 只收 SCOPE 内 (host,前缀) 链接。无兜底放宽——避免爬进 job-ff-help 等非卖家区。
    document.querySelectorAll("a[href]").forEach(a => {
      try {
        const u = new URL(a.href, location.href);
        if (inScope(u.host, u.pathname)) {
          out.add(normURL(u.href)); // 与 buf key 同一归一化形式，保证 dedup 命中
        }
      } catch (e) {}
    });

    const links = [...out];
    console.log("[rules_kb] 发现链接数:", links.length, "示例:", links.slice(0, 2));
    return links;
  }

  function exportJSONL() {
    const buf = get(BUF, {});
    if (Object.keys(buf).length === 0) {
      alert("缓冲区为空");
      console.log("[rules_kb] 导出失败：缓冲区为空");
      return;
    }
    
    const lines = Object.values(buf).map(r => JSON.stringify(r)).join("\n");
    const data = lines + "\n";
    console.log("[rules_kb] 导出准备 - 共", Object.keys(buf).length, "页，数据大小:", data.length, "字节");
    
    // 方案1：用原生 Blob + MouseEvent 触发
    try {
      const blob = new Blob([data], { type: "application/x-ndjson;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "ozon_pages.jsonl";
      link.style.cssText = "display:none;position:fixed;left:-9999px";
      document.body.appendChild(link);
      
      console.log("[rules_kb] 尝试 Blob + MouseEvent 下载...");
      link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      
      setTimeout(() => {
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        console.log("[rules_kb] ✓ Blob 下载已完成");
      }, 500);
      return;
    } catch (e) {
      console.log("[rules_kb] Blob 下载失败:", e.message);
    }
    
    // 方案2：用 window.open 和 data URI（如果 Blob 失败）
    try {
      const dataUri = "data:application/x-ndjson;charset=utf-8," + encodeURIComponent(data);
      console.log("[rules_kb] 尝试 window.open...");
      const w = window.open(dataUri, "_blank");
      if (w) {
        console.log("[rules_kb] ✓ window.open 已打开");
      }
    } catch (e) {
      console.log("[rules_kb] window.open 失败:", e.message);
      alert("导出失败，数据已在浏览器存储，请尝试手动刷新页面再试");
    }
  }

  function autoStep() {
    if (!get(AUTO, false)) return;
    const buf = get(BUF, {});
    let q = get(QUEUE, []) || [];

    // BFS：每访问一页都把该页发现的 in-scope 链接并入队列（含 category→article 子链接）。
    // 旧版只在首页种一次队列，永远停在 category 层进不了 article 层——这里修正。
    const found = docLinks();
    const seen = new Set(q);
    let added = 0;
    for (const u of found) {
      if (!buf[u] && !seen.has(u)) { q.push(u); seen.add(u); added++; }
    }
    q = q.filter(u => !buf[u]);
    set(QUEUE, q);
    set(SEEDED, true);
    console.log("[rules_kb] BFS：本页新增", added, "链接，队列剩", q.length, "已采", Object.keys(buf).length);

    if (q.length === 0) {
      set(AUTO, false);
      console.log("[rules_kb] 遍历完成，采集", Object.keys(buf).length, "页");
      alert("自动遍历完成，共采集 " + Object.keys(buf).length + " 页，点「导出 JSONL」。");
      panel(); return;
    }
    const next = q.shift();
    set(QUEUE, q);
    console.log("[rules_kb] 遍历下一页:", next);
    setTimeout(() => { location.href = next; }, 1500 + Math.floor(Math.random() * 1000));
  }

  function panel() {
    let box = document.getElementById("ozon_rk_panel");
    if (!box) {
      box = document.createElement("div");
      box.id = "ozon_rk_panel";
      box.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:2147483647;background:#111;color:#eee;font:12px/1.5 system-ui;padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.4);min-width:180px";
      (document.body || document.documentElement).appendChild(box);
    }
    const buf = get(BUF, {}), auto = get(AUTO, false);
    box.innerHTML =
      "<b>rules_kb 采集器</b><br>已采集 <b>" + Object.keys(buf).length + "</b> 页" +
      "<div style='margin-top:6px;display:flex;gap:6px;flex-wrap:wrap'>" +
      "<button id='rk_exp'>导出 JSONL</button>" +
      "<button id='rk_auto'>" + (auto ? "停止遍历" : "自动遍历") + "</button>" +
      "<button id='rk_clr'>清空</button></div>";
    box.querySelector("#rk_exp").onclick = exportJSONL;
    box.querySelector("#rk_clr").onclick = () => { if (confirm("清空缓冲区？")) { del(BUF); del(QUEUE); del(SEEDED); set(AUTO, false); panel(); } };
    box.querySelector("#rk_auto").onclick = () => {
      const on = !get(AUTO, false); set(AUTO, on);
      if (on) { del(QUEUE); del(SEEDED); } panel(); if (on) autoStep();
    };
  }

  function run() {
    console.log("[rules_kb] run() 执行，URL:", location.href);
    const collected = collectCurrent();
    console.log("[rules_kb] collectCurrent 返回:", collected);
    panel(); 
    autoStep(); 
  }

  function waitAndRun(delayMs = 500, maxRetries = 3) {
    let retry = 0;
    const attempt = () => {
      console.log("[rules_kb] waitAndRun 尝试 #" + (retry + 1));
      const text = extractText();
      if (text.length < 150 && retry < maxRetries) {
        retry++;
        console.log("[rules_kb] 内容不足，等待并重试...");
        setTimeout(attempt, delayMs);
      } else {
        run();
      }
    };
    attempt();
  }

  // 首次加载 - 等待并重试
  setTimeout(() => waitAndRun(800, 5), 500);

  // SPA 路由变化也重跑（docs 是单页应用）
  let last = location.href;
  const onChange = () => { if (location.href !== last) { last = location.href; setTimeout(() => waitAndRun(800, 3), 1000); } };
  ["pushState", "replaceState"].forEach(m => {
    const orig = history[m];
    history[m] = function () { const r = orig.apply(this, arguments); onChange(); return r; };
  });
  window.addEventListener("popstate", onChange);
  setInterval(onChange, 1200);
})();
