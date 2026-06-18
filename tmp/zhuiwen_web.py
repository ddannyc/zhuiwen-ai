#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞猴 · OpenClaw 网页前端（盒子本地聊天入口 + 设置）
- 纯 Python 3 标准库实现，无第三方依赖（适合小内存盒子）
- 对话：经 `openclaw agent --json` 走盒子里的 OpenClaw 智能体
- 设置：模型/中转站切换、平台凭证（供飞猴技能使用）、网关与模型状态
访问：默认监听 127.0.0.1:8080（仅本机），经 SSH 隧道在浏览器打开。
环境变量：ZHIWEN_PORT(默认8080) ZHIWEN_BIND(默认127.0.0.1) OPENCLAW_AGENT(默认main)
"""
import os, re, json, shutil, subprocess, threading, sys, time, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zhuiwen_studio as studio  # AI 创作工作室（主图/换装/抠图/详情，移植自千相工坊）

PORT  = int(os.environ.get("ZHIWEN_PORT", "8080"))
BIND  = os.environ.get("ZHIWEN_BIND", "127.0.0.1")
AGENT = os.environ.get("OPENCLAW_AGENT", "main")
CREDS_PATH = os.path.expanduser("~/.openclaw/zhuiwen_creds.json")
SELECT_PY  = os.path.expanduser("~/.openclaw/skills/zhuiwen-product-selection/scripts/select.py")
HOT_PY     = os.path.expanduser("~/.openclaw/skills/zhuiwen-hot-selection/scripts/hot.py")
# 发行(已编译)盒子用 ~/bin 下的二进制(无源码)；开发环境无二进制时回退 python3 + .py
SELECT_BIN = os.path.expanduser("~/bin/zhuiwen-select")
HOT_BIN    = os.path.expanduser("~/bin/zhuiwen-hot")
SELECT_CMD = [SELECT_BIN] if os.path.exists(SELECT_BIN) else ["python3", SELECT_PY]
HOT_CMD    = [HOT_BIN] if os.path.exists(HOT_BIN) else ["python3", HOT_PY]
DATA_DIR   = os.path.expanduser("~/zhuiwen-data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
MEDIA_DIR  = os.path.join(DATA_DIR, "media")
LOGO_PATH  = os.path.expanduser("~/zhuiwen-data/brand_logo.png")  # 后台上传的品牌logo（全站共用）
_LOGO_SVG  = ("<svg viewBox='0 0 100 100' xmlns='http://www.w3.org/2000/svg'><rect width='100' height='100' rx='20' "
              "fill='#ED7D1C'/><path fill='#fff' d='M20 30 L31 30 L53 50 L31 70 L20 70 L42 50 Z'/>"
              "<path fill='#fff' d='M50 30 L62 30 L80 70 L68 70 Z'/><path fill='#fff' d='M68 30 L80 30 L62 70 L50 70 Z'/></svg>")

# 让子进程能找到 openclaw / node（systemd 用户服务 PATH 较精简）
for d in ("/usr/local/bin", "/usr/bin", "/usr/local/sbin", "/usr/sbin",
          os.path.expanduser("~/.npm-global/bin")):
    if d not in os.environ.get("PATH", "").split(os.pathsep) and os.path.isdir(d):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
OPENCLAW = shutil.which("openclaw") or "openclaw"
# 服务作用域：云服务器=user(systemctl --user)；Windows 盒子(WSL,root)=system
_SCTL = ["systemctl"] if os.environ.get("ZHI_SVC_SCOPE", "user") == "system" else ["systemctl", "--user"]
# 网页登录密码：设了 ZHI_WEB_PASS 则用户端 8080 需登录（公网暴露时用）；留空=不鉴权
ZHI_WEB_PASS = os.environ.get("ZHI_WEB_PASS", "")
_WEB_COOKIE = hashlib.sha256(("zhweb|" + ZHI_WEB_PASS).encode()).hexdigest()[:24]
# 媒体公网地址（供妙手抓取翻译/上传的图片）：如 http://47.94.47.212:8080
MEDIA_PUBLIC_BASE = os.environ.get("MEDIA_PUBLIC_BASE", "")
WEB_LOGIN_HTML = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                  "<title>飞猴 · 登录</title><style>body{margin:0;font-family:system-ui,Microsoft YaHei,sans-serif;"
                  "background:linear-gradient(135deg,#ecfdf5,#e0f2fe)}#b{max-width:340px;margin:16vh auto;background:#fff;"
                  "border:1px solid #e5e7eb;border-radius:16px;padding:28px;box-shadow:0 10px 40px rgba(0,0,0,.08)}"
                  "h1{font-size:20px;margin:0 0 2px}.s{color:#6b7280;font-size:13px;margin-bottom:14px}"
                  "input{width:100%;box-sizing:border-box;padding:11px 13px;border:1px solid #e5e7eb;border-radius:10px;font-size:14px}"
                  "button{width:100%;margin-top:12px;padding:11px;background:#10b981;color:#fff;border:0;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer}"
                  ".m{color:#b91c1c;font-size:12px;margin-top:8px;min-height:14px}</style>"
                  "<div id=b><h1>飞猴 · 跨境电商智能体</h1><div class=s>请输入访问密码</div>"
                  "<input id=p type=password placeholder=密码 onkeydown=\"if(event.key=='Enter')lg()\">"
                  "<button onclick=lg()>登录</button><div class=m id=m></div></div>"
                  "<script>function lg(){fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                  "body:JSON.stringify({pass:document.getElementById('p').value})}).then(r=>r.json()).then(j=>{"
                  "if(j.ok)location.reload();else document.getElementById('m').textContent='密码错误';});}</script>")

# 售卖版激活页：未激活/到期/被停用时整站只显示这一页
ACTIVATE_HTML = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                 "<title>飞猴 · 盒子激活</title><style>body{margin:0;font-family:system-ui,Microsoft YaHei,sans-serif;"
                 "background:linear-gradient(135deg,#ecfdf5,#e0f2fe)}#b{max-width:400px;margin:12vh auto;background:#fff;"
                 "border:1px solid #e5e7eb;border-radius:16px;padding:28px;box-shadow:0 10px 40px rgba(0,0,0,.08)}"
                 "h1{font-size:20px;margin:0 0 2px;display:flex;align-items:center;gap:9px}"
                 "h1 img{width:30px;height:30px;border-radius:8px}"
                 ".s{color:#6b7280;font-size:13px;margin-bottom:14px;line-height:1.6}"
                 "label{display:block;font-size:12.5px;font-weight:700;margin:11px 0 4px}"
                 "input{width:100%;box-sizing:border-box;padding:11px 13px;border:1px solid #e5e7eb;border-radius:10px;font-size:14px}"
                 "button{width:100%;margin-top:14px;padding:11px;background:#10b981;color:#fff;border:0;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer}"
                 ".m{font-size:12.5px;margin-top:9px;min-height:14px}.m.bad{color:#b91c1c}.m.ok{color:#0e9f6e}"
                 ".bid{font-family:Consolas,monospace;font-size:12px;color:#6b7280;background:#f8fafc;border-radius:8px;padding:7px 10px;margin-top:12px}</style>"
                 "<div id=b><h1><img src='/logo' alt=''>飞猴 · 盒子激活</h1>"
                 "<div class=s id=st>本盒子尚未激活。请输入卖家提供的激活码完成激活后使用。</div>"
                 "<div id=cwrap style='display:none'><label>中心服务器地址（卖家提供）</label>"
                 "<input id=cen placeholder='http://服务器IP:9000'></div>"
                 "<label>激活码</label><input id=code placeholder='FH-XXXX-XXXX-XXXX' "
                 "style='text-transform:uppercase;letter-spacing:1px' onkeydown=\"if(event.key=='Enter')act()\">"
                 "<button id=go onclick=act()>立即激活</button><div class=m id=m></div>"
                 "<div class=bid>盒子ID：<span id=bid>-</span>（联系卖家时报这个ID）</div></div>"
                 "<script>"
                 "fetch('/api/license',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})"
                 ".then(r=>r.json()).then(function(j){document.getElementById('bid').textContent=j.box_id||'-';"
                 "if(!j.center)document.getElementById('cwrap').style.display='block';"
                 "var st=document.getElementById('st');"
                 "if(j.status==='expired')st.textContent='本盒子授权已到期。请联系卖家续期后自动恢复，或输入新的激活码。';"
                 "else if(j.status==='disabled')st.textContent='本盒子授权已被停用，请联系卖家恢复。';"
                 "else if(j.code)st.textContent='当前激活码 '+j.code+' 状态异常（'+(j.status||'未知')+'），请联系卖家或更换激活码。';"
                 "}).catch(function(){});"
                 "function act(){var m=document.getElementById('m'),g=document.getElementById('go');"
                 "var code=document.getElementById('code').value.trim();if(!code){m.className='m bad';m.textContent='请输入激活码';return;}"
                 "var body={code:code};var c=document.getElementById('cen');if(c&&c.value.trim())body.center=c.value.trim();"
                 "g.disabled=true;m.className='m';m.textContent='激活中…';"
                 "fetch('/api/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})"
                 ".then(r=>r.json()).then(function(j){if(j.ok){m.className='m ok';m.textContent='✓ '+(j.msg||'激活成功');"
                 "setTimeout(function(){location.reload();},900);}else{m.className='m bad';m.textContent='✗ '+(j.error||'激活失败');g.disabled=false;}})"
                 ".catch(function(e){m.className='m bad';m.textContent='✗ '+e;g.disabled=false;});}"
                 "</script>")


def _run(cmd, timeout=180, stdin_text=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=stdin_text)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时"
    except Exception as e:
        return 1, "", str(e)


# ── OpenClaw 桥接 ────────────────────────────────────────────────────
PRIORITY_KEYS = ("reply", "responseText", "text", "message", "content",
                 "output", "assistant", "answer", "result")

def _extract_reply(stdout: str) -> str:
    s = (stdout or "").strip()
    if not s:
        return ""
    data = None
    try:
        data = json.loads(s)
    except Exception:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line); break
                except Exception:
                    pass
    if data is None:
        return s

    # OpenClaw 信封：真正的回复在 result.payloads[].text
    try:
        payloads = (data.get("result") or {}).get("payloads") or []
        texts = [p.get("text", "") for p in payloads if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(texts).strip()
    except Exception:
        pass

    def find(o):
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            for k in PRIORITY_KEYS:
                if k in o:
                    r = find(o[k])
                    if r:
                        return r
            best = ""
            for v in o.values():
                r = find(v)
                if r and len(r) > len(best):
                    best = r
            return best
        if isinstance(o, list):
            best = ""
            for v in o:
                r = find(v)
                if r and len(r) > len(best):
                    best = r
            return best
        return ""
    return find(data) or s


_CHAT_SYS = ("你是「飞猴」——专注 Ozon 与 TikTok Shop 跨境电商的 AI 助手，擅长选品、竞品分析、"
             "Listing 与卖点、定价、客服话术。回答务实、结论先行、用简体中文，必要时用表格。")


def chat(message: str, session: str, history=None) -> dict:
    """对话统一走阿里通义千问（DashScope），不再依赖网关/DeepSeek。"""
    msgs = [{"role": "system", "content": _CHAT_SYS}]
    for h in (history or [])[-6:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) and h["content"].strip():
            msgs.append({"role": h["role"], "content": h["content"][:1500]})
    msgs.append({"role": "user", "content": (message or "")[:3000]})
    txt = _ali_chat(msgs, max_tokens=2200, temperature=0.6, timeout=120)
    if not txt:
        return {"ok": False, "error": "阿里模型无返回，请到「设置/后台」确认 DashScope Key 与额度。"}
    return {"ok": True, "reply": txt}


def status() -> dict:
    # 对话/分析统一走阿里通义千问；展示真实对话模型而非内部网关模型（也省去每次心跳的慢查询）
    model = "通义千问" if _creds_raw().get("DASHSCOPE_API_KEY") else "未配置"
    g = subprocess.run(_SCTL + ["is-active", "openclaw-gateway"],
                       capture_output=True, text=True)
    gateway = (g.stdout or "").strip() or "unknown"
    return {"model": model, "gateway": gateway, "agent": AGENT}


def set_relay(base_url, api_key, model, compat) -> dict:
    """把模型切到中转站（OpenAI/Anthropic 兼容的自定义 provider）。"""
    if not base_url or not model:
        return {"ok": False, "error": "base_url 与 model 必填"}
    cmd = [OPENCLAW, "onboard", "--non-interactive", "--accept-risk",
           "--flow", "quickstart", "--auth-choice", "custom-api-key",
           "--custom-base-url", base_url, "--custom-model-id", model,
           "--custom-compatibility", (compat or "openai"),
           "--custom-text-input",
           "--no-install-daemon", "--skip-channels", "--skip-skills",
           "--skip-search", "--skip-ui"]
    if api_key:
        cmd += ["--custom-api-key", api_key]
    code, out, err = _run(cmd, timeout=120)
    if code != 0:
        return {"ok": False, "error": (err or out or "切换失败").strip().splitlines()[-1]}
    # 重启网关使配置生效
    subprocess.run(_SCTL + ["restart", "openclaw-gateway"],
                   capture_output=True, text=True)
    return {"ok": True, "msg": "已切换到中转站并重启网关"}


def _creds_raw() -> dict:
    """读取明文凭证（仅服务端内部使用，如调用生图/视频 API）。"""
    try:
        with open(CREDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_creds() -> dict:
    # 给前端用：不回传密钥明文，只回传是否已填；非密钥项（如中心地址）回传原文以便回显/编辑
    plain = {"HOT_CENTRAL_URL", "HOT_UPLOAD_KEY"}
    return {k: (v if k in plain else ("******" if v else "")) for k, v in _creds_raw().items()}


def save_creds(d: dict) -> dict:
    try:
        cur = {}
        if os.path.exists(CREDS_PATH):
            with open(CREDS_PATH, "r", encoding="utf-8") as f:
                cur = json.load(f)
        clearable = {"HOT_CENTRAL_URL", "HOT_UPLOAD_KEY"}  # 这些键允许提交空值以清除（如改回用本盒子热销）
        for k, v in (d or {}).items():
            if v and v != "******":
                cur[k] = v
            elif k in clearable and (v == "" or v is None):
                cur.pop(k, None)
        os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
        tmp = CREDS_PATH + ".tmp"   # 原子写：避免并发读到半截文件（否则 _box_id 会误判无ID而换新身份）
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CREDS_PATH)
        os.chmod(CREDS_PATH, 0o600)
        return {"ok": True, "msg": "平台凭证已保存（供飞猴技能使用）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 用量统计 + 心跳上报（供中心控制台远程监控）────────────────────────
APP_VERSION = "feihou-1.4"   # 每次发版递增：中心台靠它区分各盒子程序新旧
USAGE_PATH = os.path.join(DATA_DIR, "usage.json")
_usage_lock = threading.Lock()
_START_TS = [0.0]

# ── 盒子授权（激活码，由中心控制台管控；售卖版核心）────────────────────
LICENSE_PATH = os.path.join(DATA_DIR, "license.json")
# 售卖盒子设 ZHI_REQUIRE_LICENSE=1：未激活/到期/被停用 → 只能进激活页
REQUIRE_LICENSE = os.environ.get("ZHI_REQUIRE_LICENSE", "0") == "1"
_license_lock = threading.Lock()


def _license_get():
    try:
        with open(LICENSE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _license_save(d):
    with _license_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LICENSE_PATH, "w", encoding="utf-8") as f:
                json.dump(d or {}, f, ensure_ascii=False)
        except Exception:
            pass


def _license_ok():
    """门控：是否放行业务功能。中心短暂连不上沿用最近一次结果（卖家服务器抖动不影响客户），
    但 ① 本地按到期时间硬卡（离线也会到期）② 超过 7 天没和中心核验过 → 锁定（防改地址绕过监控）。"""
    if not REQUIRE_LICENSE:
        return True
    lic = _license_get()
    if (lic.get("status") or "") != "active":
        return False
    exp = lic.get("expires_ts") or 0
    if exp and time.time() >= exp:
        return False
    ck = lic.get("checked_ts") or 0
    return bool(ck) and (time.time() - ck) < 7 * 86400


def _now_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _usage_get():
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _img_count(r):
    return len(r.get("images", [])) if isinstance(r, dict) and r.get("ok") else 0


def _usage_bump(**kw):
    if not kw:
        return
    with _usage_lock:
        u = _usage_get()
        for k, v in kw.items():
            try:
                u[k] = int(u.get(k, 0)) + int(v)
            except Exception:
                pass
        u["updated"] = _now_str()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = USAGE_PATH + ".tmp"   # 原子写：避免心跳线程读到截断的半截文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(u, f, ensure_ascii=False)
            os.replace(tmp, USAGE_PATH)
        except Exception:
            pass


_BOX_ID_CACHE = [None]


def _box_id():
    import uuid
    if _BOX_ID_CACHE[0]:          # 进程级缓存：一旦确定本盒子ID，运行期内绝不改变（杜绝身份漂移）
        return _BOX_ID_CACHE[0]
    c = _creds_raw()
    bid = c.get("BOX_ID")
    if not bid and os.path.exists(CREDS_PATH):
        # 凭证文件存在却读不到ID（可能并发读到半截）→ 不要贸然换新ID，重读一次确认
        try:
            with open(CREDS_PATH, encoding="utf-8") as f:
                bid = (json.load(f) or {}).get("BOX_ID")
        except Exception:
            bid = None
    if bid:
        _BOX_ID_CACHE[0] = bid
        return bid
    bid = "box-" + uuid.uuid4().hex[:10]   # 真·首次（凭证里确无ID）才生成
    try:
        save_creds({"BOX_ID": bid})
    except Exception:
        pass
    _BOX_ID_CACHE[0] = bid
    return bid


def _box_cmd(cmd):
    a = (cmd or {}).get("action")
    if a == "restart":
        subprocess.run(_SCTL + ["restart", "zhuiwen-web", "zhuiwen-admin", "openclaw-gateway"], capture_output=True)


def _heartbeat_once():
    import urllib.request as _u
    c = _creds_raw()
    center = (c.get("CENTER_URL") or "").strip().rstrip("/")
    if not center:
        return
    st = status()
    with _usage_lock:       # 持锁读：避免与 _usage_bump 写入并发拿到空/半截数据
        usage_snapshot = _usage_get()
    payload = {"box_id": _box_id(), "name": c.get("BOX_NAME") or "", "version": APP_VERSION,
               "uptime": int(time.time() - _START_TS[0]) if _START_TS[0] else 0,
               "model": st.get("model"), "gateway": st.get("gateway"),
               "usage": usage_snapshot, "net": True, "ts": _now_str(),
               "code": (c.get("LICENSE_CODE") or "").strip().upper(),    # 激活码：预烧码首次心跳自动激活
               "box_key": c.get("CENTER_KEY") or ""}
    try:
        req = _u.Request(center + "/api/box/heartbeat", data=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
        lic = resp.get("license")
        if isinstance(lic, dict) and lic.get("status"):
            lic["checked"] = _now_str()
            lic["checked_ts"] = time.time()
            _license_save(lic)        # 中心是许可的唯一权威：续期/停用 1 分钟内生效
        for cmd in (resp.get("commands") or []):
            try:
                _box_cmd(cmd)
            except Exception:
                pass
    except Exception:
        pass


def _reporter_loop():
    _heartbeat_once()
    while True:
        time.sleep(60)
        _heartbeat_once()


def box_activate(b):
    """用激活码向中心服务器激活本盒子（设置页/激活页调用）。"""
    code = (b.get("code") or "").strip().upper()
    if not code:
        return {"ok": False, "error": "请输入激活码"}
    c = _creds_raw()
    center = (c.get("CENTER_URL") or "").strip().rstrip("/")   # 出厂已绑定，不接受用户自填
    if not center:
        return {"ok": False, "error": "本盒子未配置授权服务器，请联系卖家"}
    import urllib.request as _u
    payload = {"box_id": _box_id(), "code": code, "name": c.get("BOX_NAME") or "",
               "box_key": c.get("CENTER_KEY") or ""}
    try:
        req = _u.Request(center + "/api/box/activate", data=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "连接中心服务器失败：%s" % e}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "激活失败"}
    save_creds({"LICENSE_CODE": code, "CENTER_URL": center})
    res["checked"] = _now_str()
    res["checked_ts"] = time.time()
    _license_save(res)
    threading.Thread(target=_heartbeat_once, daemon=True).start()   # 立即上报一次
    return {"ok": True, "status": res.get("status"), "expires_at": res.get("expires_at") or "",
            "days_left": res.get("days_left"), "msg": "激活成功，有效期至 %s" % (res.get("expires_at") or "-")}


def license_view():
    lic = _license_get()
    c = _creds_raw()
    st = lic.get("status") or ""
    if not st:
        st = "none" if (REQUIRE_LICENSE or c.get("LICENSE_CODE")) else "standalone"
    return {"ok": True, "require": REQUIRE_LICENSE, "box_id": _box_id(),
            "center": c.get("CENTER_URL") or "", "code": c.get("LICENSE_CODE") or "",
            "status": st, "expires_at": lic.get("expires_at") or "",
            "days_left": lic.get("days_left"), "checked": lic.get("checked") or "",
            "active": _license_ok()}


# ── 反向隧道 agent：长轮询中心台 → 转发到本地后台(8088) → 回传 ──────────
_ADMIN_PORT = int(os.environ.get("ZHIADM_PORT", "8088"))


def _admin_cookie():
    p = os.environ.get("ZHI_ADMIN_PASS", "")
    if not p:
        return ""
    return "za=" + hashlib.sha256(("zhadm|" + p).encode()).hexdigest()[:24]


def _tunnel_forward(req):
    import urllib.request as _u, urllib.error as _ue, base64
    method = req.get("method", "GET")
    path = req.get("path", "/")
    body = base64.b64decode(req.get("body_b64") or "")
    url = "http://127.0.0.1:%d%s" % (_ADMIN_PORT, path)
    headers = {}
    ct = (req.get("headers") or {}).get("Content-Type")
    if ct:
        headers["Content-Type"] = ct
    ck = _admin_cookie()
    if ck:
        headers["Cookie"] = ck
    try:
        r = _u.Request(url, data=(body if method == "POST" else None), headers=headers, method=method)
        with _u.urlopen(r, timeout=40) as resp:
            data = resp.read()
            status = resp.status
            rct = resp.headers.get("Content-Type", "application/octet-stream")
    except _ue.HTTPError as he:
        data = he.read()
        status = he.code
        rct = he.headers.get("Content-Type", "text/plain; charset=utf-8")
    except Exception as e:
        data = ("代理到本地后台失败: " + str(e)).encode("utf-8")
        status = 502
        rct = "text/plain; charset=utf-8"
    return {"id": req.get("id"), "status": status, "ctype": rct,
            "body_b64": base64.b64encode(data).decode()}


def _tunnel_loop():
    import urllib.request as _u
    while True:
        c = _creds_raw()
        center = (c.get("CENTER_URL") or "").strip().rstrip("/")
        if not center:
            time.sleep(20)
            continue
        tkey = c.get("CENTER_KEY") or ""
        try:
            rq = _u.Request(center + "/api/tunnel/poll",
                            data=json.dumps({"box_id": _box_id(), "tkey": tkey}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}, method="POST")
            with _u.urlopen(rq, timeout=35) as r:
                j = json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(3)
            continue
        req = (j or {}).get("req")
        if not req:
            continue
        resp = _tunnel_forward(req)
        resp["tkey"] = tkey
        try:
            rr = _u.Request(center + "/api/tunnel/resp", data=json.dumps(resp).encode("utf-8"),
                            headers={"Content-Type": "application/json"}, method="POST")
            _u.urlopen(rr, timeout=20).read()
        except Exception:
            pass


# ── 候选商品 → 智能体打分（选品菜单 / 扩展采集 共用）─────────────────
def _loose_json(text):
    s = (text or "").strip()
    for pat in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        m = re.search(pat, s)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    try:
        return json.loads(s)
    except Exception:
        return None


def _fmt_price(v):
    try:
        return ("%.2f" % float(v)).rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _save_passing_to_box(passing):
    urls = [r["c"].get("source_url", "") for r in passing
            if "1688.com/offer/" in (r["c"].get("source_url", "") or "")]
    if not urls:
        return 0
    code, out, _ = _run(SELECT_CMD + ["--mode", "save", "--urls"] + urls, timeout=90)
    if code != 0:
        return 0
    try:
        return int((json.loads(out.strip() or "{}")).get("saved", 0))
    except Exception:
        return 0


def _ali_chat(messages, max_tokens=1500, temperature=0.4, model=None, timeout=120):
    """统一的阿里通义千问对话调用（DashScope 兼容模式）。全平台 LLM 都走这里，不再用 DeepSeek。"""
    import urllib.request as _u
    creds = _creds_raw()
    key = (creds.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        return ""
    model = model or (creds.get("ALI_CHAT_MODEL") or "qwen-plus").strip()
    body = json.dumps({"model": model, "messages": messages, "temperature": temperature,
                       "max_tokens": max_tokens}).encode("utf-8")
    try:
        req = _u.Request("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", data=body,
                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    except Exception:
        return ""


def _score_candidates(cands, threshold, save_passing=False):
    th = 70 if threshold in (None, "") else int(threshold)
    payload = [{"i": i, "title": c.get("title", ""), "price_cny": c.get("price_cny", 0),
                "source_url": c.get("source_url", "")} for i, c in enumerate(cands)]
    prompt = (
        "你是 TikTok Shop 跨境选品专家。对下面 %d 个候选商品逐个按维度打分（总分100）："
        "TikTok趋势热度25 / 利润空间25 / 视觉内容20 / 物流友好15 / 市场竞争度15。"
        "只返回一个 JSON 数组，不要任何解释、不要 markdown。每个元素："
        '{"i":序号, "score":0-100整数, "reason":"20字内中文理由", "title_en":"60字内英文标题", "category":"TikTok一级类目(英文)"}。'
        "只评估给出的商品，不要编造。\n\n候选（含序号 i）：\n%s"
        % (len(cands), json.dumps(payload, ensure_ascii=False))
    )
    # 优先直连 DeepSeek（比走网关 agent 稳：网关短回复时 payloads 偶发为空导致评分失败）
    scored = _agent_llm_json("你是 TikTok Shop 跨境选品专家。严格只返回一个 JSON 数组，不要解释、不要 markdown。",
                             prompt, max_tokens=min(8000, 500 + len(cands) * 130), msg_cap=24000, timeout=120)
    if not isinstance(scored, list):   # 兜底：走 OpenClaw 网关 agent（旧路）
        _score_candidates.seq = getattr(_score_candidates, "seq", 0) + 1
        sess = "agent:%s:sel%d" % (AGENT, _score_candidates.seq)
        code, out, err = _run([OPENCLAW, "agent", "--message", prompt, "--session-key", sess,
                               "--json", "--timeout", "150"], timeout=175)
        if code == 0:
            scored = _loose_json(_extract_reply(out))
    if not isinstance(scored, list):
        return {"ok": False, "error": "采到 %d 个候选，但评分失败：模型未返回有效结果，请重试" % len(cands)}
    by_i = {}
    if isinstance(scored, list):
        for it in scored:
            if isinstance(it, dict) and "i" in it:
                try:
                    by_i[int(it["i"])] = it
                except Exception:
                    pass
    if not by_i:
        return {"ok": False, "count": len(cands), "passed": 0, "saved": 0,
                "error": "评分结果解析失败，请重试", "reply": "评分结果为空，请重试"}

    rows = []
    for i, c in enumerate(cands):
        s = by_i.get(i, {})
        try:
            sc = float(s.get("score") or 0)
        except Exception:
            sc = 0.0
        rows.append({"c": c, "score": sc, "reason": str(s.get("reason", "")),
                     "title_en": str(s.get("title_en", "")), "category": str(s.get("category", ""))})
    rows.sort(key=lambda r: r["score"], reverse=True)
    passing = [r for r in rows if r["score"] >= th]

    lines = ["| 排名 | 中文标题 | 采购价¥ | 分数 | 理由 | 建议英文标题 | TikTok类目 |",
             "|:---:|:---|:---:|:---:|:---|:---|:---|"]
    for rank, r in enumerate(rows, 1):
        c = r["c"]
        mark = "✅" if r["score"] >= th else ""
        lines.append("| %s%d | %s | %s | %d | %s | %s | %s |" % (
            mark, rank, (c.get("title", "") or "")[:36], _fmt_price(c.get("price_cny", 0)),
            int(r["score"]), (r["reason"] or "")[:20], (r["title_en"] or "")[:50], r["category"]))
    md = ("## 选品评分（共 %d 个 · ≥%d 分推荐 %d 个）\n\n" % (len(rows), th, len(passing))) + "\n".join(lines)

    saved = 0
    if save_passing and passing:
        saved = _save_passing_to_box(passing)
        md += "\n\n✅ 已将 %d 个评分≥%d 的商品存入妙手采集箱（共 %d 个达标）。" % (saved, th, len(passing))
    scores = [{"id": r["c"].get("id"), "score": r["score"], "pass": r["score"] >= th,
               "source_url": r["c"].get("source_url", "")} for r in rows]
    return {"ok": True, "count": len(cands), "passed": len(passing), "saved": saved,
            "reply": md, "scores": scores}


def ingest_1688(products, threshold, save_passing=True):
    """接收浏览器扩展从已登录 1688 采集的商品并打分；评分通过的可存入妙手采集箱。"""
    cands = []
    for p in (products or []):
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        url = (p.get("source_url") or p.get("url") or "").strip()
        if not title and not url:
            continue
        cands.append({
            "id": p.get("id") or p.get("offerId") or "",
            "title": title,
            "price_cny": p.get("price_cny") or p.get("price") or 0,
            "source_url": url,
            "image": p.get("image") or "",
        })
    if not cands:
        return {"ok": False, "error": "扩展未送来有效商品（标题/链接为空）"}
    try:
        dd = os.path.expanduser("~/zhuiwen-data")
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "last_1688_ingest.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(cands, ensure_ascii=False, indent=2))
    except Exception:
        pass
    return _score_candidates(cands, threshold, save_passing=save_passing)


# 违禁词（妙手词库 + 常见货源营销词）：标题里出现会导致平台发布失败或不专业，自动优化时清掉
_BANWORDS = [
    "淘宝", "天猫", "Taobao", "京东", "JD.com", "拼多多", "Pinduoduo", "Temu", "唯品会", "Vipshop",
    "苏宁易购", "苏宁", "Suning", "亚马逊", "Amazon", "eBay", "易贝", "沃尔玛", "Walmart", "Wayfair",
    "Etsy", "Shopee", "TikTok", "抖音", "Zalando", "Lazada", "来赞达", "Wish", "Shopify", "Ozon",
    "AliExpress", "速卖通", "Coupang", "Alibaba", "阿里巴巴", "Daraz", "JOOM", "Allegro", "Qoo10",
    "SHOPLINE", "SHEIN", "Miravia", "JUMIA", "eMAG", "1688", "义乌", "PingPong", "LianLian",
    "跨境寻源通", "货源网", "供应链", "一件代发", "代发", "厂家直销", "厂家", "工厂", "源头工厂",
    "源头", "批发", "批发价", "清仓", "清仓价", "跨境专供", "外贸", "特价", "促销", "秒杀", "包邮",
    "正品", "专柜", "旗舰店", "官方", "直销", "礼品伞广告伞", "可印logo", "印logo", "定制",
]
_BAN_RE = re.compile("|".join(re.escape(w) for w in sorted(set(_BANWORDS), key=len, reverse=True)), re.IGNORECASE)


def _clean_title(t):
    """删标题里的平台名/批发/工厂等违禁与营销词，整理空白与多余标点。"""
    s = _BAN_RE.sub("", t or "")
    s = re.sub(r"[【】\[\]（）()]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ，,、。.-—|/·　")
    return s


def optimize_box_items(ids, do_title=True, do_images=True):
    """自动优化采集箱：清标题违禁词 + Qwen-VL 质检图片(剔除工厂宣传图/水印/低质)，写回。"""
    creds = _creds_raw()
    key = (creds.get("DASHSCOPE_API_KEY") or "").strip()
    vlm = (creds.get("QWEN_VL_MODEL") or "qwen3-vl-plus").strip()
    n_t, n_i = 0, 0
    for did in (ids or [])[:200]:
        if not did:
            continue
        det = box_detail(did)
        if not det.get("ok"):
            continue
        ch = {}
        if do_title and det.get("title"):
            ct = _clean_title(det["title"])
            if ct and ct != det["title"]:
                ch["title"] = ct
        if do_images and key:
            imgs = det.get("imgUrls") or []
            if len(imgs) > 1:
                good = studio.pick_good_images(key, imgs, model=vlm)
                if good and len(good) < len(imgs):
                    ch["imgUrls"] = good[:15]
        if ch and box_edit(did, ch).get("ok"):
            if "title" in ch:
                n_t += 1
            if "imgUrls" in ch:
                n_i += 1
    return {"titles": n_t, "images": n_i}


def ingest_1688_urls(urls, threshold, save_passing=True, score=True, translate=False, lang="",
                     trans_images=False, list_tiktok=False, tk_auto=False, top_n=0, optimize=False,
                     platform="tiktok"):
    """扩展送 1688 链接：妙手 fetch_item 采集 →（可选 AI 评分 / 翻译 / 一条龙上架 TikTok）→ 入采集箱。"""
    urls = [u.strip() for u in (urls or []) if u and "1688.com/offer/" in u]
    urls = list(dict.fromkeys(urls))[:200]   # 上限提到 200（原 50 导致采多少都只处理50个）
    if not urls:
        return {"ok": False, "error": "扩展未采到有效的 1688 货源链接"}
    code, out, err = _run(SELECT_CMD + ["--mode", "url", "--limit",
                           str(len(urls)), "--urls"] + urls, timeout=520)
    if code != 0:
        return {"ok": False, "error": "妙手采集失败：" + ((err or out or "").strip().splitlines()[-1] if (err or out) else "未知")}
    try:
        cands = json.loads(out.strip() or "[]")
    except Exception:
        cands = []
    if not cands:
        return {"ok": False, "error": "妙手未采到详情（链接可能无效或采集超时）。已尝试 %d 条链接。" % len(urls)}

    if score:
        res = _score_candidates(cands, threshold, save_passing=False)
        if not res.get("ok"):
            return res
        scores = res.get("scores") or []
        if top_n and scores:   # 只保留评分最高 N 个（覆盖阈值通过标记）
            keep = {str(s.get("id")) for s in sorted(scores, key=lambda x: x.get("score", 0), reverse=True)[:int(top_n)] if s.get("id")}
            for s in scores:
                s["pass"] = str(s.get("id")) in keep
        if save_passing:
            fail_ids = [str(s["id"]) for s in scores if s.get("id") and not s.get("pass")]
            deleted = 0
            if fail_ids:
                c2, o2, _ = _run(SELECT_CMD + ["--mode", "delete", "--ids"] + fail_ids, timeout=60)
                if c2 == 0:
                    try:
                        deleted = int((json.loads(o2.strip() or "{}")).get("deleted", 0))
                    except Exception:
                        deleted = 0
            keptn = sum(1 for s in scores if s.get("pass"))
            res["saved"] = keptn
            res["reply"] += "\n\n✅ 采集箱已保留 %d 个评分≥%d 的品，移除 %d 个不达标。" % (keptn, int(threshold or 70), deleted)
            pass_ids = {str(s.get("id")) for s in scores if s.get("pass")}
            kept = [c for c in cands if str(c.get("id")) in pass_ids]
        else:
            kept = list(cands)
    else:
        lines = ["| # | 商品 | 货源价¥ |", "|---|---|---|"]
        for i, c in enumerate(cands, 1):
            lines.append("| %d | %s | %s |" % (i, _esc_md(c.get("title")), c.get("price_cny") or c.get("price_min") or 0))
        res = {"ok": True, "count": len(cands), "saved": len(cands),
               "reply": "🛒 已直接采集 **%d** 个商品到采集箱（未评分）。\n\n" % len(cands) + "\n".join(lines),
               "scores": [{"id": c.get("id"), "title": c.get("title"), "pass": True} for c in cands]}
        kept = list(cands)

    if optimize:
        opt = optimize_box_items([c.get("id") for c in kept if c.get("id")])
        for c in kept:   # 让后续翻译用清理后的标题
            if c.get("title"):
                c["title"] = _clean_title(c["title"])
        res["reply"] += "\n\n🧹 自动优化：清理 **%d** 个标题违禁词、精选 **%d** 个商品的图片（剔除工厂宣传图/水印/低质）。" % (opt.get("titles", 0), opt.get("images", 0))

    if translate and lang:
        creds = _creds_raw()
        pub = (MEDIA_PUBLIC_BASE or "").rstrip("/")
        done, img_items, img_done, fail = 0, 0, 0, 0
        for c in kept:
            did = c.get("id")
            if not did:
                continue
            changes = {}
            t = c.get("title")
            if t:
                tr = studio.translate_title({"title": t, "lang": lang}, creds)
                if tr.get("ok") and tr.get("title"):
                    changes["title"] = tr["title"]
            if trans_images:
                det = box_detail(did)
                imgs = (det.get("imgUrls") if det.get("ok") else None) or ([c.get("image")] if c.get("image") else [])
                if imgs:
                    ti = studio.translate_images({"images": imgs[:9], "lang": lang, "pub_base": pub}, creds, MEDIA_DIR)
                    # 用妙手可直接抓取的公网直链（DashScope OSS）写回；盒子 8080 被防火墙挡的本地链会被妙手判不可达而整条编辑失败
                    wb = [u for u in (ti.get("urls") or []) if u] if ti.get("ok") else []
                    if wb:
                        changes["imgUrls"] = wb
                # 详情页(notes HTML)里的图片也翻译并替换回
                nt, nn = _translate_notes_images(det.get("notes") or "", lang, creds, pub)
                if nn:
                    changes["notes"] = nt
                    img_done += nn
            if changes:
                if box_edit(did, changes).get("ok"):
                    if "title" in changes:
                        done += 1
                    if "imgUrls" in changes:
                        img_items += 1
                        img_done += len(changes["imgUrls"])
                else:
                    fail += 1
        res["reply"] += "\n\n🌐 已把 **%d** 个标题翻译为「%s」并写回采集箱。" % (done, lang)
        if trans_images:
            res["reply"] += "\n🖼 已翻译并写回 **%d** 个商品共 **%d** 张图片。" % (img_items, img_done)
        if fail:
            res["reply"] += "\n（⚠ %d 个商品写回失败，可在采集箱手动重试）" % fail
    if list_tiktok:
        kept_ids = [c.get("id") for c in kept if c.get("id")]
        if (platform or "tiktok").lower() == "ozon":
            if kept_ids:
                rr = ozon_list_items(kept_ids, auto=bool(tk_auto))
                if rr.get("ok"):
                    s = rr.get("summary", {})
                    res["reply"] += "\n\n📤 已提交 Ozon：提交 %s · 失败 %s（共 %s）%s" % (
                        s.get("submitted", 0), s.get("failed", 0), s.get("total", 0),
                        ("　task_id=" + str(rr.get("task_id"))) if rr.get("task_id") else "")
                else:
                    res["reply"] += "\n\n📤 Ozon 上架失败：" + (rr.get("error") or "")
        else:
            shop = _default_tk_shop()
            if not shop:
                res["reply"] += "\n\n📤 上架跳过：未配置 TikTok 店铺（采集箱→⚙模板配置 选店铺）"
            elif kept_ids:
                rr = tk_list_items(kept_ids, shop, templates_get().get("claim", {}).get("site", "MY"), bool(tk_auto))
                if rr.get("ok"):
                    s = rr.get("summary", {})
                    res["reply"] += "\n\n📤 已上架 TikTok：预填 %s · 发布 %s · 失败 %s（共 %s）" % (
                        s.get("prepared", 0), s.get("published", 0), s.get("failed", 0), s.get("total", 0))
                else:
                    res["reply"] += "\n\n📤 上架失败：" + (rr.get("error") or "")
    return res


def _esc_md(s):
    return (str(s or "")).replace("|", "/").replace("\n", " ")[:40]


# ── 演示版：单条 1688 链接 → 妙手直接采集到采集箱（不评分，快速）────────
def demo_save(url):
    url = (url or "").strip()
    if "/offer/" not in url:
        return {"ok": False, "error": "无效的 1688 货源链接"}
    code, out, err = _run(SELECT_CMD + ["--mode", "save", "--urls", url], timeout=45)
    if code != 0:
        return {"ok": False, "error": (err or out or "采集失败").strip().splitlines()[-1]}
    try:
        d = json.loads(out.strip() or "{}")
    except Exception:
        d = {}
    return {"ok": True, "saved": int(d.get("saved", 1) or 1)}


# ── 选品：纯妙手API采集 → 智能体打分 ─────────────────────────────────
def select(mode, urls, limit, threshold, only_success):
    cmd = SELECT_CMD + ["--mode", mode, "--limit", str(int(limit or 10)), "--format", "json"]
    if mode == "url":
        clean = [u.strip() for u in (urls or []) if u.strip()]
        if not clean:
            return {"ok": False, "error": "请粘贴至少一条 1688 货源链接"}
        cmd += ["--urls"] + clean
    elif only_success:
        cmd += ["--only-success"]
    code, out, err = _run(cmd, timeout=120)
    if code != 0:
        return {"ok": False, "error": (err or out or "采集失败").strip().splitlines()[-1]}
    try:
        cands = json.loads(out.strip() or "[]")
    except Exception:
        cands = []
    if not cands:
        tip = "采集箱为空或妙手凭证未配置（设置→平台凭证）" if mode == "box" else "链接无效或妙手采集超时，请重试"
        return {"ok": False, "error": "未采到候选商品：" + tip}
    return _score_candidates(cands, threshold)


# ── 热销榜（读 hot.py） ──────────────────────────────────────────────
def _hot_central_url():
    # 设了则从中心服务器拉热销，几十个盒子共享一份；优先读凭证（设置可改），回退环境变量
    u = (_creds_raw().get("HOT_CENTRAL_URL") or os.environ.get("HOT_CENTRAL_URL") or "").strip().rstrip("/")
    return u


def _hot_local(b):
    cmd = HOT_CMD + ["--format", "json", "--limit", str(int(b.get("limit") or 20))]
    for k in ("site", "period", "category", "keyword", "sort"):
        v = (b.get(k) or "").strip()
        if v:
            cmd += ["--" + k, v]
    code, out, err = _run(cmd, timeout=40)
    if code != 0:
        return {"ok": False, "error": (err or out or "读取热销数据失败").strip().splitlines()[-1]}
    try:
        return {"ok": True, "rows": json.loads(out.strip() or "[]")}
    except Exception:
        return {"ok": False, "error": "热销数据解析失败"}


def hot_upload(b, headers=None):
    # 采集机每日把热销 JSON 推到中心（密钥校验）；写入 hot_latest.json + 当日存档，供所有盒子共享
    key_cfg = (_creds_raw().get("HOT_UPLOAD_KEY") or os.environ.get("HOT_UPLOAD_KEY") or "").strip()
    if not key_cfg:
        return {"ok": False, "error": "本服务器未配置上传密钥(HOT_UPLOAD_KEY)，已禁止上传"}
    key_in = (b.get("key") or (headers.get("X-Hot-Key") if headers else "") or "").strip()
    if key_in != key_cfg:
        return {"ok": False, "error": "上传密钥不正确"}
    items = b.get("items")
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "items 为空或格式不正确（应为商品列表）"}
    if len(items) > 200000:
        return {"ok": False, "error": "数据量过大，已拒绝"}
    import datetime
    day = datetime.date.today().isoformat()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = json.dumps(items, ensure_ascii=False)
        for p in (os.path.join(DATA_DIR, "hot_latest.json"),
                  os.path.join(DATA_DIR, "hot_%s.json" % day)):
            with open(p, "w", encoding="utf-8") as f:
                f.write(payload)
        return {"ok": True, "count": len(items), "date": day, "msg": "已更新中心热销数据，%d 条" % len(items)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def hot_query(b):
    # 配了中心地址 → 从中心服务器统一拉取（盒子不必各自采集）；失败回退本地
    central = _hot_central_url()
    if central:
        import urllib.request as _u
        try:
            body = json.dumps({k: b.get(k) for k in ("site", "period", "category", "keyword", "sort", "limit")}).encode("utf-8")
            req = _u.Request(central + "/api/hot-data", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
            with _u.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("ok") and d.get("rows") is not None:
                d["source"] = "central"
                return d
        except Exception:
            pass
    return _hot_local(b)


# ── 采集箱（妙手 公共采集箱） ────────────────────────────────────────
def box_list(page, limit):
    code, out, err = _run(SELECT_CMD + ["--mode", "box", "--limit", str(int(limit or 20)),
                           "--page", str(int(page or 1)), "--format", "json"], timeout=60)
    if code != 0:
        return {"ok": False, "error": (err or out or "读取采集箱失败").strip().splitlines()[-1]}
    try:
        return {"ok": True, "rows": json.loads(out.strip() or "[]")}
    except Exception:
        return {"ok": False, "error": "采集箱数据解析失败"}


def box_delete(ids):
    ids = [str(i) for i in (ids or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "error": "未选择"}
    code, out, _ = _run(SELECT_CMD + ["--mode", "delete", "--ids"] + ids, timeout=60)
    if code != 0:
        return {"ok": False, "error": "删除失败"}
    try:
        return {"ok": True, "deleted": int((json.loads(out.strip() or "{}")).get("deleted", 0))}
    except Exception:
        return {"ok": True, "deleted": len(ids)}


def box_detail(did):
    if not str(did).strip().isdigit():
        return {"ok": False, "error": "无效ID"}
    code, out, err = _run(SELECT_CMD + ["--mode", "detail", "--id", str(did)], timeout=40)
    if code != 0:
        return {"ok": False, "error": (err or out or "读取失败").strip().splitlines()[-1]}
    try:
        d = json.loads(out.strip() or "{}")
        if "ok" not in d:
            d["ok"] = True
        return d
    except Exception:
        return {"ok": False, "error": "解析详情失败"}


def box_edit(did, changes):
    if not str(did).strip().isdigit():
        return {"ok": False, "error": "无效ID"}
    code, out, err = _run(SELECT_CMD + ["--mode", "edit", "--id", str(did),
                           "--changes", json.dumps(changes or {}, ensure_ascii=False)], timeout=50)
    if code != 0:
        return {"ok": False, "error": (err or out or "保存失败").strip().splitlines()[-1]}
    try:
        return json.loads(out.strip() or '{"ok":true}')
    except Exception:
        return {"ok": True}


def box_upload_img(b64):
    if not b64:
        return {"ok": False, "error": "无图片"}
    name = _save_media({"b64_json": b64} if not str(b64).startswith("http") else {"url": b64}, "boxup", "img")
    if not name:
        return {"ok": False, "error": "图片保存失败"}
    pub = (MEDIA_PUBLIC_BASE or "").rstrip("/")
    rel = "/asset/file?type=media&name=" + name
    return {"ok": True, "name": name, "display": rel, "url": (pub + rel) if pub else rel}


# ── 妙手模板/规则（定价/采集/认领/物流）：盒子本地配置，采集与上架时自动套用 ──
TEMPLATES_PATH = os.path.join(DATA_DIR, "templates.json")
_DEFAULT_TEMPLATES = {
    "pricing": {"enabled": True, "exchange": 0.62, "markup_pct": 60, "add_fixed": 0,
                "round99": True, "currency": "MYR"},
    "collect": {"lang": "英语", "threshold": 70, "score": True,
                "auto_translate": True, "trans_images": False},
    "claim": {"shopId": "", "site": "MY", "warehouse": ""},
    "logistics": {"weight_default": 0.1, "package_l": 10, "package_w": 10, "package_h": 10},
}


def templates_get():
    out = json.loads(json.dumps(_DEFAULT_TEMPLATES))
    try:
        d = json.load(open(TEMPLATES_PATH, encoding="utf-8"))
        for k, v in (d or {}).items():
            if isinstance(v, dict) and k in out:
                out[k].update(v)
    except Exception:
        pass
    return out


def templates_save(d):
    cur = templates_get()
    for k, v in (d or {}).items():
        if isinstance(v, dict) and k in cur:
            cur[k].update(v)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return {"ok": True, "templates": cur}


def compute_sale_price(cny, pricing=None):
    """按定价模板把货源价(CNY)换算成目标币种售价：货源价×汇率×(1+加价率%)+固定加价，可尾数.99。"""
    pricing = pricing if pricing is not None else templates_get().get("pricing", {})
    if not pricing.get("enabled"):
        return None
    try:
        p = float(cny or 0) * float(pricing.get("exchange") or 1) \
            * (1 + float(pricing.get("markup_pct") or 0) / 100.0) + float(pricing.get("add_fixed") or 0)
    except Exception:
        return None
    if p <= 0:
        return 0.0
    if pricing.get("round99") and p >= 1:
        import math
        p = math.floor(p) + 0.99
    return round(p, 2)


def tk_shops():
    """列出妙手里已绑定的 TikTok 店铺（供认领/上架选择）。"""
    code, out, err = _run(SELECT_CMD + ["--mode", "shops"], timeout=40)
    if code != 0:
        return {"ok": False, "error": (err or out or "读取店铺失败").strip().splitlines()[-1], "shops": []}
    try:
        return {"ok": True, "shops": json.loads(out.strip() or "[]")}
    except Exception:
        return {"ok": False, "error": "店铺数据解析失败", "shops": []}


def chat_vision(b):
    """对话上传图片 → Qwen-VL 视觉理解（跨境电商选品/优化视角）。"""
    creds = _creds_raw()
    key = (creds.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "error": "未配置阿里云百炼 Key（图片理解需 DashScope，请到设置/后台配置）"}
    imgs = [x for x in (b.get("images") or []) if x][:3]
    if not imgs:
        return {"ok": False, "error": "无图片"}
    prompt = (b.get("message") or "").strip() or "请描述这张商品图片，并从跨境电商（TikTok/Ozon）选品与listing优化角度给出建议。"
    if len(imgs) > 1:
        prompt += "（用户共上传 %d 张，先分析第 1 张）" % len(imgs)
    vlm = (creds.get("QWEN_VL_MODEL") or "qwen3-vl-plus").strip()
    out = studio.qwen_vl(key, prompt, imgs[0], model=vlm, timeout=90)
    if not out:
        return {"ok": False, "error": "图片理解无返回，请重试或换图"}
    return {"ok": True, "reply": out}


def _tkcall(endpoint, body):
    """经 select.py 调用妙手 TikTok 采集箱接口（统一签名）。"""
    code, out, err = _run(SELECT_CMD + ["--mode", "tkcall", "--tk", endpoint,
                           "--body", json.dumps(body, ensure_ascii=False)], timeout=70)
    if code != 0:
        return {"ok": False, "error": (err or out or "TK接口调用失败").strip().splitlines()[-1]}
    try:
        return json.loads(out.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "TK返回解析失败"}


def _flatten_leaves(tree, path="", out=None):
    """类目树 → 全部叶子类目 [{cid, path}]（isLastLevel=1；不再过滤 disabled，
    否则正确类目如 插座/猫粮 会被滤掉导致匹配错成手机壳）。"""
    if out is None:
        out = []
    if not isinstance(tree, dict):
        return out
    for cid, node in tree.items():
        if not isinstance(node, dict):
            continue
        cn = node.get("nameChinese") or node.get("name") or ""
        full = (path + "/" + cn) if path else cn
        if str(node.get("isLastLevel")) == "1" and not node.get("disabled"):
            out.append({"cid": node.get("cid") or cid, "path": full})   # 只要店铺已开通(未禁用)的类目
        if node.get("children"):
            _flatten_leaves(node["children"], full, out)
    return out


def _cat_children(tree):
    """顶层类目 [{cid, cn, node}]。"""
    out = []
    for cid, n in (tree or {}).items():
        if isinstance(n, dict):
            out.append({"cid": n.get("cid") or cid, "cn": n.get("nameChinese") or n.get("name") or "", "node": n})
    return out


def _qwen_pick(key, title, items, what):
    """让 Qwen 从 items[{cid,label}] 里挑一个最匹配的 cid（数字）。挑不中返回 None。"""
    if not items:
        return None
    lines = "\n".join("%s = %s" % (c["cid"], c["label"]) for c in items[:1400])
    out = studio.qwen_text(
        key, "你是 TikTok Shop 类目匹配助手。从候选里挑一个最贴合该商品的%s，即使不完全匹配也必须挑最接近的一个。"
             "只输出该项的 cid 数字，不要任何解释或多余字符。" % what,
        "商品标题：%s\n\n候选（格式 cid = 名称/路径）：\n%s\n\n输出最匹配的 cid 数字：" % (title, lines),
        model="qwen-plus", max_tokens=20)
    m = re.search(r"\d{4,}", out or "")
    cid = m.group(0) if m else None
    valid = {str(c["cid"]) for c in items}
    return cid if (cid and cid in valid) else None


def _ai_pick_category(title, cate_tree):
    """两段式选类目，且只在店铺已开通(未禁用)的叶子类目里选，避免"非主营类目/仅限邀请"。"""
    if not title or not isinstance(cate_tree, dict) or not cate_tree:
        return None
    key = (_creds_raw().get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        return None
    # 每个一级类目下的"可用叶子"，只保留有可用叶子的一级类目
    top_leaf = {}
    for t in _cat_children(cate_tree):
        sub = _flatten_leaves(t["node"].get("children") or {str(t["cid"]): t["node"]})
        if sub:
            top_leaf[str(t["cid"])] = (t["cn"], sub)
    if not top_leaf:
        return None
    top_cid = _qwen_pick(key, title, [{"cid": c, "label": v[0]} for c, v in top_leaf.items()], "一级类目")
    leaves = top_leaf.get(str(top_cid), (None, None))[1]
    if not leaves:  # 一级没选中，退化到全部可用叶子
        leaves = []
        for _, sub in top_leaf.values():
            leaves.extend(sub)
    if not leaves:
        return None
    picked = _qwen_pick(key, title, [{"cid": c["cid"], "label": c["path"]} for c in leaves], "叶子类目")
    return picked or str(leaves[0]["cid"])   # 兜底：选不中也给一个可用类目，保证 cid 必有


_CLAIMED_PATH = "/open/v1/product/common_collect_box/common_collect_box/claimed"


def _tk_fill_category_attrs(sci, site, cid, shop_id):
    """按类目元数据补"必填产品属性"，避免上架报"xxx 属性必填"。"""
    if not cid:
        return
    meta = _tkcall("get_category_metadata", {"site": site, "cid": int(cid), "shopIds": [int(shop_id)]})
    cm = ((meta.get("data") or {}).get("categoryMetadata")) or {} if meta.get("ok") else {}
    have = {str(a.get("attributeId")) for a in (sci.get("productAttributes") or []) if isinstance(a, dict)}
    out = list(sci.get("productAttributes") or [])
    for a in (cm.get("categoryProductAttrList") or []):
        if not a.get("isMandatory"):
            continue
        aid = str(a.get("attrId") or "")
        if not aid or aid in have:
            continue
        vals = a.get("values") or []
        if vals:
            v0 = vals[0]
            av = {"valueId": str(v0.get("id") or v0.get("valueId") or ""),
                  "valueName": v0.get("name") or v0.get("valueName") or ""}
        else:
            av = {"valueName": "Standard"}
        out.append({"attributeId": aid, "attributeName": a.get("name") or a.get("attributeNameAlias") or "",
                    "attributeValues": [av]})
    if out:
        sci["productAttributes"] = out


def _default_tk_shop(explicit=""):
    """目标 TikTok 店铺：显式 > 妙手第一个绑定的 TK 店铺（已不再依赖采集箱模板）。"""
    if str(explicit).strip().isdigit():
        return int(explicit)
    try:
        sh = tk_shops().get("shops") or []
        return int(sh[0]["shopId"]) if sh else None
    except Exception:
        return None


def _tk_fill_required(sci, logi):
    """上架保存前把必填字段补齐（缺失/为0一律补默认），避免 packageLength/重量/配送等"必填"报错。"""
    def pos(*vals):
        for v in vals:
            try:
                f = float(v)
                if f > 0:
                    return round(f, 2)
            except Exception:
                continue
        return None
    if isinstance(sci.get("imgUrls"), list) and len(sci["imgUrls"]) > 15:
        sci["imgUrls"] = sci["imgUrls"][:15]   # TK 主图上限 15
    notes = sci.get("notes")                   # 详情描述图上限 30，超出删多余
    if isinstance(notes, str):
        imgs = list(re.finditer(r"<img\b[^>]*>", notes, re.I))
        if len(imgs) > 30:
            for m in reversed(imgs[30:]):
                notes = notes[:m.start()] + notes[m.end():]
            sci["notes"] = notes
    sci["weight"] = pos(sci.get("weight"), logi.get("weight_default")) or 0.1
    sci["packageLength"] = pos(sci.get("packageLength"), logi.get("package_l")) or 10.0
    sci["packageWidth"] = pos(sci.get("packageWidth"), logi.get("package_w")) or 10.0
    sci["packageHeight"] = pos(sci.get("packageHeight"), logi.get("package_h")) or 10.0
    if not sci.get("deliveryOptionSetType"):
        sci["deliveryOptionSetType"] = "default"
    if not sci.get("isCodOpen"):
        sci["isCodOpen"] = "0"
    # 尺码表：仅保留合法 jpg/jpeg/png 图片URL，否则清空（避免"仅支持JPG/PNG"校验）
    sc = sci.get("sizeChart")
    if isinstance(sc, str) and re.search(r"\.(jpg|jpeg|png)(\?|$)", sc, re.I):
        sci["sizeChartType"] = "image"
    else:
        sci["sizeChart"] = ""
        sci["sizeChartType"] = ""
    return sci


def tk_list_items(detail_ids, shop_id, site, auto=False):
    """公共采集箱 → ① claimed 认领到 TikTok 平台采集箱(得 公共ID→TK_ID 映射) →
    ② claim_to_shop 认领到店铺 → ③ AI 选类目+套物流模板预填 → ④(auto)发布。"""
    detail_ids = [int(d) for d in (detail_ids or []) if str(d).strip().isdigit()]
    if not detail_ids:
        return {"ok": False, "error": "未选择要上架的商品"}
    shop_id = _default_tk_shop(shop_id)
    if not shop_id:
        return {"ok": False, "error": "妙手未绑定 TikTok 店铺，请先在妙手绑定店铺"}
    shop_id = int(shop_id)
    site = site or templates_get().get("claim", {}).get("site") or "MY"
    logi = templates_get().get("logistics", {})
    # 只处理"采集成功"的商品（claimed 要求 success，含未成功项会整批失败）
    status_map = {}
    for it in (box_list(1, 500).get("rows") or []):
        if it.get("id") is not None:
            status_map[int(it["id"])] = (it.get("status") or "success")
    skipped = [d for d in detail_ids if status_map.get(d, "success") not in ("success", "", None)]
    detail_ids = [d for d in detail_ids if d not in skipped]
    if not detail_ids:
        return {"ok": False, "error": "选中的商品都还未采集成功（妙手仍在补全详情，稍等几十秒再试）"}
    # ① 认领到平台(TK)采集箱：公共采集箱ID → TK采集箱ID
    cd = _tkcall(_CLAIMED_PATH, {"detailSerialNumberPlatformList":
                                 [{"detailId": d, "platform": "tiktok", "serialNumber": 1} for d in detail_ids]})
    if not cd.get("ok"):
        return {"ok": False, "error": "认领到平台采集箱失败：" + (cd.get("error") or "")}
    raw = ((cd.get("data") or {}).get("platformCollectBoxDetailIdMap")) or {}
    # 结构为 {"tiktok": {公共ID: TK_ID}}；兜底取第一个平台或扁平结构
    idmap = raw.get("tiktok") if isinstance(raw.get("tiktok"), dict) else None
    if idmap is None:
        idmap = next((v for v in raw.values() if isinstance(v, dict)), raw)
    pairs = []  # (公共ID, TK_ID)
    for d in detail_ids:
        tkid = idmap.get(str(d)) or idmap.get(d)
        if tkid:
            pairs.append((d, int(tkid)))
    if not pairs:
        return {"ok": False, "error": "认领到平台采集箱未返回 TK 商品ID（可能该商品已认领或不支持该平台）"}
    tk_ids = [tk for _, tk in pairs]
    # ② 认领到预发布店铺
    cl = _tkcall("claim_to_shop", {"shopIds": [shop_id], "detailIds": tk_ids})
    if not cl.get("ok"):
        return {"ok": False, "error": "认领到店铺失败：" + (cl.get("error") or "")}
    ct = _tkcall("get_category_tree_by_site", {"site": site})
    cate_tree = ((ct.get("data") or {}).get("cateTree")) or {} if ct.get("ok") else {}
    results = []
    for common_id, tkid in pairs:
        r = {"id": common_id, "tkId": tkid, "status": "fail"}
        info = _tkcall("get_shop_collect_item_info", {"detailId": tkid, "shopId": shop_id})
        if not info.get("ok"):
            r["error"] = "读取上架信息失败：" + (info.get("error") or "")
            results.append(r); continue
        data = info.get("data") or {}
        oss = data.get("ossMd5")
        sci = data.get("shopCollectItemInfo") or {}
        sci["detailId"] = tkid            # save 必填：店铺模式详情ID
        sci["shopId"] = shop_id
        if not sci.get("editModel"):
            sci["editModel"] = data.get("editModel") or "shop"
        r["title"] = (sci.get("title") or sci.get("oriTitle") or "")[:38]
        if not sci.get("cid") and cate_tree:
            cid = _ai_pick_category(sci.get("title") or sci.get("oriTitle") or "", cate_tree)
            if cid:
                sci["cid"] = str(cid)
        r["cid"] = sci.get("cid") or ""
        if sci.get("cid"):
            _tk_fill_category_attrs(sci, site, sci["cid"], shop_id)   # 补类目必填属性
        _tk_fill_required(sci, logi)   # 必填字段(尺寸/重量/配送等)缺失自动补全
        sv = _tkcall("save_shop_collect_item_info",
                     {"ossMd5": oss, "detailId": tkid, "shopId": shop_id, "shopCollectItemInfo": sci})
        if not sv.get("ok"):
            r["status"] = "prefill_fail"; r["error"] = "预填保存失败：" + (sv.get("error") or "")
            results.append(r); continue
        r["status"] = "prepared"
        results.append(r)
    prepared = [r["tkId"] for r in results if r["status"] == "prepared"]
    if auto and prepared:
        pub = _tkcall("save_move_collect_task", {"shopIds": [shop_id], "detailIds": prepared})
        for r in results:
            if r.get("tkId") in prepared:
                r["status"] = "published" if pub.get("ok") else "publish_fail"
                if not pub.get("ok"):
                    r["error"] = "发布失败：" + (pub.get("error") or "")
    n_ok = sum(1 for r in results if r["status"] in ("prepared", "published"))
    if n_ok:
        _usage_bump(listings=n_ok)   # 用量统计：上架成功数（预填或发布）
    return {"ok": True, "auto": auto, "results": results,
            "summary": {"total": len(detail_ids), "skipped": len(skipped),
                        "prepared": sum(1 for r in results if r["status"] == "prepared"),
                        "published": sum(1 for r in results if r["status"] == "published"),
                        "failed": sum(1 for r in results if "fail" in r["status"])}}


# ── Ozon 直连上架（Seller API；首版，需真实店铺联调）──────────────────────
OZON_BASE = "https://api-seller.ozon.ru"
_OZON_TREE = [None]


def _ozon_creds():
    c = _creds_raw()
    return ((c.get("OZON_CLIENT_ID") or os.environ.get("OZON_CLIENT_ID") or "").strip(),
            (c.get("OZON_API_KEY") or os.environ.get("OZON_API_KEY") or "").strip())


def _ozon_call(path, body, timeout=30):
    cid, key = _ozon_creds()
    if not cid or not key:
        return {"ok": False, "error": "未配置 Ozon 凭证（设置 → 平台凭证 → Ozon Client-Id / Api-Key）"}
    import urllib.request as _u
    import urllib.error as _e
    req = _u.Request(OZON_BASE + path, data=json.dumps(body or {}).encode("utf-8"), method="POST",
                     headers={"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"})
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8") or "{}")}
    except _e.HTTPError as ex:
        try:
            ed = json.loads(ex.read().decode("utf-8"))
            msg = ed.get("message") or ed.get("error") or json.dumps(ed, ensure_ascii=False)[:200]
        except Exception:
            msg = "HTTP %s" % ex.code
        return {"ok": False, "error": "Ozon接口(%s): %s" % (ex.code, msg)}
    except Exception as ex:
        return {"ok": False, "error": "Ozon请求失败: %s" % ex}


def _ozon_category_tree():
    """拉取并扁平化 Ozon 类目树为叶子 [{dcid,tid,name}]；缓存。"""
    if _OZON_TREE[0] is not None:
        return _OZON_TREE[0]
    r = _ozon_call("/v1/description-category/tree", {"language": "EN"})
    leaves = []
    if r.get("ok"):
        def walk(nodes, dcid, path):
            for n in (nodes or []):
                nd = n.get("description_category_id") or dcid
                nm = n.get("category_name") or n.get("type_name") or ""
                p = (path + " > " + nm).strip(" >")
                if n.get("type_id"):
                    leaves.append({"dcid": nd, "tid": n.get("type_id"), "name": p})
                walk(n.get("children") or [], nd, p)
        walk((r.get("data") or {}).get("result") or [], None, "")
    if leaves:
        _OZON_TREE[0] = leaves
    return leaves


def _ozon_pick_category(title):
    """AI 从 Ozon 类目树挑一个最贴合的叶子，返回 (description_category_id, type_id)。"""
    leaves = _ozon_category_tree()
    if not leaves:
        return None, None
    key = (_creds_raw().get("DASHSCOPE_API_KEY") or "").strip()
    tid = None
    if key:
        tid = _qwen_pick(key, title, [{"cid": l["tid"], "label": l["name"]} for l in leaves], "Ozon 商品类目")
    leaf = next((l for l in leaves if str(l["tid"]) == str(tid)), None) or leaves[0]
    return leaf["dcid"], leaf["tid"]


def ozon_list_items(detail_ids, auto=False):
    """采集箱选品 → 直连 Ozon Seller API 上架（/v3/product/import）。
    首版：标题/图片来自采集箱详情，类目 AI 自动匹配，尺寸/重量/品牌用默认值；
    Ozon 异步审核，提交后返回 task_id。需真实 Ozon 店铺按报错逐项校准（同当初 TK）。"""
    cid, key = _ozon_creds()
    if not cid or not key:
        return {"ok": False, "error": "请先在「设置 → 平台凭证」填写 Ozon Client-Id 与 Api-Key"}
    detail_ids = [str(d) for d in (detail_ids or []) if str(d).strip()]
    if not detail_ids:
        return {"ok": False, "error": "未选择要上架的商品"}
    items, results = [], []
    for did in detail_ids:
        det = box_detail(did)
        if not det.get("ok"):
            results.append({"id": did, "status": "fail", "error": det.get("error") or "读取详情失败"})
            continue
        title = ((det.get("title_en") or det.get("title") or "").strip())[:500] or ("Product %s" % did)
        imgs = [u for u in (det.get("imgUrls") or []) if str(u).startswith("http")][:15]
        try:
            price_rub = max(1, round(float(det.get("price_cny") or det.get("price") or 0) * 13))
        except Exception:
            price_rub = 100
        dcid, tid = _ozon_pick_category(title)
        if not dcid or not tid:
            results.append({"id": did, "title": title[:38], "status": "fail",
                            "error": "类目匹配失败（确认 Ozon 凭证可用、能拉取类目树）"})
            continue
        items.append({
            "offer_id": "FH-%s" % did, "name": title,
            "description_category_id": int(dcid), "type_id": int(tid),
            "price": str(price_rub), "currency_code": "RUB", "vat": "0",
            "images": imgs,
            "depth": 200, "width": 150, "height": 80, "dimension_unit": "mm",
            "weight": 500, "weight_unit": "g",
            "attributes": [{"complex_id": 0, "id": 85, "values": [{"value": "NoName"}]}],
        })
        results.append({"id": did, "offer_id": "FH-%s" % did, "title": title[:38],
                        "cid": "%s/%s" % (dcid, tid), "status": "prepared"})
    if not items:
        return {"ok": False, "error": "没有可上架的商品", "results": results}
    imp = _ozon_call("/v3/product/import", {"items": items})
    if not imp.get("ok"):
        for r in results:
            if r.get("status") == "prepared":
                r["status"] = "import_fail"
                r["error"] = imp.get("error")
        return {"ok": False, "error": imp.get("error"), "results": results,
                "summary": {"total": len(detail_ids), "submitted": 0,
                            "failed": sum(1 for r in results if "fail" in r["status"])}}
    task_id = ((imp.get("data") or {}).get("result") or {}).get("task_id")
    n_sub = 0
    for r in results:
        if r.get("status") == "prepared":
            r["status"] = "submitted"
            n_sub += 1
    if n_sub:
        _usage_bump(listings=n_sub)   # 用量统计：上架成功数(Ozon 提交)
    return {"ok": True, "platform": "ozon", "task_id": task_id, "results": results,
            "note": "已提交 Ozon 创建任务（task_id=%s）。Ozon 异步审核，可在 Ozon 卖家后台查看进度；"
                    "首版价格(汇率粗算)/尺寸/重量/品牌为默认值，按需在后台微调。" % task_id,
            "summary": {"total": len(detail_ids),
                        "submitted": sum(1 for r in results if r["status"] == "submitted"),
                        "failed": sum(1 for r in results if "fail" in r["status"])}}


# ── 对话操控层：把自然语言指令路由到本系统的实际功能并执行 ──────────────
def _agent_llm_json(system, message, history=None, max_tokens=400, msg_cap=1500, timeout=120):
    """统一走阿里通义千问（DashScope）。返回解析后的 JSON 或 None。"""
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-4:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) and h["content"].strip():
            msgs.append({"role": h["role"], "content": h["content"][:800]})
    msgs.append({"role": "user", "content": (message or "")[:msg_cap]})
    content = _ali_chat(msgs, max_tokens=max_tokens, temperature=0.1, timeout=timeout)
    return _loose_json(content) if content else None


def _box_all_ids(chinese_only=False, cap=500):
    d = box_list(1, cap)
    rows = d.get("rows") or []
    out = []
    for it in rows:
        if chinese_only and not re.search(r"[一-鿿]", it.get("title") or ""):
            continue
        if it.get("id"):
            out.append(it["id"])
    return out


def _translate_notes_images(notes, lang, creds, pub):
    """翻译详情页(notes HTML)里的 <img> 图片，并把原链接替换成译图链接。返回 (新notes, 翻译数)。"""
    if not notes or "<img" not in notes:
        return notes, 0
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', notes)
    seen, uniq = set(), []
    for u in imgs:
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u); uniq.append(u)
    uniq = uniq[:8]
    if not uniq:
        return notes, 0
    ti = studio.translate_images({"images": uniq, "lang": lang, "pub_base": pub}, creds, MEDIA_DIR)
    if not ti.get("ok"):
        return notes, 0
    new = ti.get("urls") or []
    n = 0
    for orig, rep in zip(uniq, new):
        if rep:
            notes = notes.replace(orig, rep); n += 1
    return notes, n


def translate_box_items(ids, lang, images=False):
    creds = _creds_raw()
    pub = (MEDIA_PUBLIC_BASE or "").rstrip("/")
    done, img_items = 0, 0
    for did in (ids or [])[:200]:
        det = box_detail(did)
        if not det.get("ok"):
            continue
        changes = {}
        t = det.get("title")
        if t:
            tr = studio.translate_title({"title": t, "lang": lang}, creds)
            if tr.get("ok") and tr.get("title"):
                changes["title"] = tr["title"]
        if images:
            imgs = det.get("imgUrls") or []
            if imgs:
                ti = studio.translate_images({"images": imgs[:9], "lang": lang, "pub_base": pub}, creds, MEDIA_DIR)
                wb = [u for u in (ti.get("urls") or []) if u] if ti.get("ok") else []
                if wb:
                    changes["imgUrls"] = wb
            nt, nn = _translate_notes_images(det.get("notes") or "", lang, creds, pub)
            if nn:
                changes["notes"] = nt
        if changes and box_edit(did, changes).get("ok"):
            if "title" in changes:
                done += 1
            if "imgUrls" in changes:
                img_items += 1
    return "已翻译写回 **%d** 个标题为「%s」%s。" % (done, lang, ("、%d 个商品的图片" % img_items) if images else "")


_ACT_SYSTEM = (
    "你是「飞猴」跨境电商智能体的指令路由器。判断用户这句话要在系统里执行什么，输出严格 JSON："
    '{"action":"动作","params":{...},"say":"给用户的简短中文说明"}。可用动作：\n'
    '- "box.list" 查看采集箱(params:{limit:数字,可选 keyword})\n'
    '- "box.count" 采集箱有多少商品\n'
    '- "box.delete_chinese" 删除采集箱里标题仍是中文的商品\n'
    '- "box.delete_all" 清空采集箱\n'
    '- "box.translate" 翻译采集箱并写回(params:{scope:"all"或"chinese",lang:"英语/马来语/俄语…",images:true/false})\n'
    '- "box.list_tiktok" 把采集箱商品上架到 TikTok(params:{scope:"all"或"chinese",auto:true/false})\n'
    '- "pipeline" 一条龙：把采集箱商品翻译并上架(params:{scope:"all"或"chinese",lang:"马来语/英语…",images:true/false,auto:true/false})。用户说"一条龙/翻译并上架/翻译完自动上架"用这个\n'
    '- "auto_collect" 全自动采集(从0开始)：用户要"自动采集N个品/按关键词采集/根据某方向采集并上架"时用。'
    'params:{keywords:["中文搜索词"],perKw:每词采集数,topN:只保留评分最高N个,score:true,translate:true/false,'
    'lang:"马来语",listTiktok:true/false,tkAuto:true/false}。你要根据用户方向(蓝海/热销/某品类/某市场)生成 3-8 个精准的中文 1688 搜索关键词\n'
    '- "analyze" 选品/竞品分析(params:{keyword:"词",type:"feasibility/blue_ocean/voc/compare/listing/pricing"})\n'
    '- "chat" 普通电商问答/咨询(不操作系统)。无法对应上面动作就用 chat。\n\n'
    "重要：凡是用户要『从 1688 采集/选品/找货』(不是操作已有采集箱)，一律用 auto_collect，"
    "由你直接生成中文搜索关键词，绝不要用 chat 去反问数据源或要 API Key。"
    "用户提到蓝海/热销/某市场就据此自拟关键词。只输出 JSON。\n"
    '示例：用户"根据TK马来西亚蓝海，自动采集每词20个，选评分最高10个，翻译成马来语并自动上架tk" → '
    '{"action":"auto_collect","params":{"keywords":["便携榨汁杯","硅胶厨房收纳","车载手机支架","宠物自动喂食器","桌面理线器"],'
    '"perKw":20,"topN":10,"score":true,"translate":true,"lang":"马来语","listTiktok":true,"tkAuto":true}}')


_AUTOCOLLECT_SYS = (
    "你是跨境电商选品助手，用户想从 1688 自动采集商品。只输出严格 JSON 对象："
    '{"keywords":["中文产品搜索词"],"perKw":每词采集数(默认10),"topN":只保留评分最高N个(没说填0),'
    '"score":true,"translate":是否翻译,"lang":"目标语言如马来语","listTiktok":是否自动上架,"tkAuto":是否直接发布}。\n'
    "关键词必须是【能在 1688 直接搜到具体商品的产品词】，例如：便携榨汁杯、硅胶厨房铲、车载手机支架、"
    "宠物自动喂食器、桌面理线器、折叠收纳箱、不锈钢保温杯。\n"
    "★绝对不要用抽象词，如：热销商品、蓝海产品、爆款、潜力产品、高利润商品、跨境热销品、市场新品 —— 这些搜不到货！\n"
    "根据用户给的方向(蓝海/某市场/某品类)推断出 3-8 个具体的、当下适合该市场的真实产品词。只输出 JSON，不要解释。")


_SITE_HINTS = [("马来", "马来西亚"), ("malaysia", "马来西亚"), ("印尼", "印度尼西亚"), ("印度尼西亚", "印度尼西亚"),
               ("泰国", "泰国"), ("越南", "越南"), ("菲律宾", "菲律宾"), ("新加坡", "新加坡"), ("美国", "美国"),
               ("英国", "英国"), ("日本", "日本"), ("沙特", "沙特"), ("巴西", "巴西"), ("墨西哥", "墨西哥"),
               ("德国", "德国"), ("法国", "法国"), ("意大利", "意大利"), ("西班牙", "西班牙")]


def _site_from_msg(msg):
    m = (msg or "").lower()
    for k, v in _SITE_HINTS:
        if k.lower() in m:
            return v
    return ""


def _hot_to_keywords(site, focus="", limit=20):
    """拉真实 TK 热销榜 + 用户品类方向 → Qwen 提炼可在 1688 搜的具体产品词。返回 (keywords, rows)。
    若用户指定了具体品类(如收纳盒)，围绕它出细分词；否则从热销趋势挑蓝海词。"""
    hot = hot_query({"site": site, "limit": limit, "sort": "daily_sales"})
    rows = hot.get("rows") or []
    titles = "\n".join("%d. %s" % (i + 1, (r.get("title") or r.get("name") or "")[:60]) for i, r in enumerate(rows[:20])) if rows else ""
    user = ("用户的需求原话：%s\n\n参考 TikTok·%s 实时热销趋势标题：\n%s\n\n"
            "规则：① 若用户明确提到了某个具体品类(如 收纳盒/瑜伽裤)，就【围绕该品类】输出其细分产品词"
            "(如 收纳盒→桌面收纳盒、抽屉收纳盒、化妆品收纳盒、衣柜收纳盒)；② 若用户只说市场/蓝海/热销没指定品类，"
            "就从上面热销趋势里挑有蓝海潜力的具体产品词。输出 6-10 个能在 1688 直接搜到的具体中文产品词，"
            "严格只输出 JSON 数组，如 [\"桌面收纳盒\",\"抽屉收纳盒\"]。" % (focus or "(未给方向)", site, titles or "(暂无)"))
    txt = _ali_chat(
        [{"role": "system", "content": "你是跨境选品专家，只输出能在 1688 搜到的具体中文产品词，绝不用'热销商品/蓝海产品'等抽象词。"},
         {"role": "user", "content": user}], max_tokens=300, temperature=0.3)
    kws = _loose_json(txt)
    return (kws if isinstance(kws, list) and kws else None), rows


def _do_auto_collect(params, pre="", message=""):
    kws = [k for k in (params.get("keywords") or []) if k][:10]
    hot_md = ""
    if message and re.search(r"热销|热卖|爆款|蓝海|榜|趋势|热门|大盘|实时|数据", message):
        site = _site_from_msg(message) or "美国"
        real_kws, rows = _hot_to_keywords(site, focus=message)
        if real_kws:
            kws = real_kws[:10]
            top = "\n".join("%d. %s  ¥%s" % (i + 1, (r.get("title") or r.get("name") or "")[:44],
                                             r.get("price_cny") or r.get("price") or "-") for i, r in enumerate(rows[:10]))
            hot_md = "📊 基于 **TK·%s** 实时热销 Top10（真实数据）：\n%s\n\n🔑 据此提炼关键词：%s\n\n" % (site, top, "、".join(kws))
    if not kws:
        return {"ok": False, "action": "auto_collect", "reply": "没识别到要采集的关键词/方向，请说明要采什么品类或市场方向。"}
    # 以用户原话解析的开关为准（图片翻译/一键采集 等），覆盖路由模型可能漏掉的项
    if message:
        pp = _parse_collect_params(message, kws)
        for k in ("transImages", "oneClick", "optimize", "translate", "tkAuto", "listTiktok"):
            params[k] = pp.get(k)
        for k in ("perKw", "topN", "lang"):
            if not params.get(k):
                params[k] = pp.get(k)
    tpl_lang = templates_get().get("collect", {}).get("lang") or "英语"
    job = {"keywords": kws, "perKw": int(params.get("perKw") or 10), "topN": int(params.get("topN") or 0),
           "score": params.get("score", True) is not False, "threshold": int(params.get("threshold") or 0),
           "translate": bool(params.get("translate")), "lang": params.get("lang") or tpl_lang,
           "transImages": bool(params.get("transImages")), "listTiktok": bool(params.get("listTiktok")),
           "tkAuto": bool(params.get("tkAuto")), "optimize": params.get("optimize", True) is not False,
           "oneClick": bool(params.get("oneClick")), "fast": True}
    # 逐关键词蓝海速评（一次 Qwen，呈现在对话里，不进智能分析板块）
    ana_md = ""
    if message and re.search(r"分析|蓝海|机会|可行|竞争", message):
        ana = _ali_chat(
            [{"role": "system", "content": "你是 TikTok 跨境选品专家。对每个关键词用一行点评蓝海机会(机会点/竞争度/建议)，简体中文 markdown 无序列表，每行 40 字内。"},
             {"role": "user", "content": "关键词：%s\n逐个一行点评。" % "、".join(kws)}],
            max_tokens=700, temperature=0.5)
        if ana:
            ana_md = "\n🔍 **关键词蓝海速评**\n" + ana.strip() + "\n"
    collect_job_create(job)
    return {"ok": True, "action": "auto_collect",
            "reply": pre + hot_md + ana_md + "\n✅ 已下发采集任务，采集插件会按【你保存的默认功能配置】自动执行 采集→评分→优化→翻译→上架。请保持插件侧边栏打开，进度在插件里实时显示。"}


def _parse_collect_params(msg, keywords):
    def num(pat):
        m = re.search(pat, msg)
        try:
            return int(m.group(1)) if m else 0
        except Exception:
            return 0
    per = num(r"(?:各|每词|每个|每关键词|采集?)\s*(\d+)\s*个") or 10
    topn = num(r"(?:最高的?|前|top\s*)(\d+)")
    lang = "马来语" if ("马来" in msg or "大马" in msg) else ("俄语" if "俄" in msg else ("英语" if "英" in msg else ""))
    return {"keywords": keywords, "perKw": per, "topN": topn, "score": True,
            "translate": ("翻译" in msg), "lang": lang,
            "transImages": bool(re.search(r"图片.{0,4}翻译|翻译.{0,4}图片|标题和图片|连图片|图片也翻", msg)),
            "optimize": not bool(re.search(r"不\s*优化|不要优化", msg)),
            "oneClick": bool(re.search(r"一键采集", msg)) and not bool(re.search(r"不\s*(使用|用|开启|要)?\s*一键采集|关闭\s*一键采集", msg)),
            "listTiktok": bool(re.search(r"上架|发布|上传到?\s*tk|tiktok", msg, re.I)),
            "tkAuto": bool(re.search(r"直接发布|自动发布|立即发布", msg))}


def agent_act(message, history=None):
    msg = message or ""
    # 启发式：明显的"从0采集/选品"直接走 auto_collect（路由模型常误判为 chat 反问）
    if "采集箱" not in msg and (re.search(r"(自动|全自动|帮我|根据|按|去).{0,14}(采集|选品|找货|采品)", msg)
                              or re.search(r"采集?\s*\d+\s*个", msg)
                              or ("采集" in msg and any(w in msg for w in ("关键词", "蓝海", "热销", "上架")))):
        ext, kws = None, None
        for _try in range(2):
            ext = _agent_llm_json(_AUTOCOLLECT_SYS, msg, max_tokens=700, timeout=60)
            if isinstance(ext, dict) and ext.get("keywords"):
                kws = ext.get("keywords"); break
            if isinstance(ext, list) and ext:
                if isinstance(ext[0], str):
                    kws = ext; break                       # 模型直接返回了关键词数组
                if isinstance(ext[0], dict) and ext[0].get("keywords"):
                    ext = ext[0]; kws = ext.get("keywords"); break
        if kws:
            params = _parse_collect_params(msg, [k for k in kws if k][:10])
            if isinstance(ext, dict):   # 若模型也给了参数则覆盖
                for k in ("perKw", "topN", "translate", "lang", "listTiktok", "tkAuto"):
                    if ext.get(k) not in (None, ""):
                        params[k] = ext[k]
            return _do_auto_collect(params, "", msg)
    obj = _agent_llm_json(_ACT_SYSTEM, message, history)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if not isinstance(obj, dict):
        obj = {}
    action = (obj.get("action") or "chat").strip()
    params = obj.get("params") or {}
    say = (obj.get("say") or "").strip()
    pre = (say + "\n\n") if say else ""
    try:
        if action == "box.count":
            return {"ok": True, "action": action, "reply": "采集箱当前有 **%d** 个商品。" % len(_box_all_ids())}
        if action == "box.list":
            rows = (box_list(1, int(params.get("limit") or 15)).get("rows")) or []
            kw = (params.get("keyword") or "").strip()
            if kw:
                rows = [it for it in rows if kw in (it.get("title") or "")]
            lines = "\n".join("- #%s %s ¥%s" % (it.get("id"), (it.get("title") or "")[:42], it.get("price_cny")) for it in rows[:30])
            return {"ok": True, "action": action, "reply": pre + "采集箱（%d 条）：\n%s" % (len(rows), lines or "（空）")}
        if action == "box.delete_chinese":
            ids = _box_all_ids(chinese_only=True)
            if not ids:
                return {"ok": True, "action": action, "reply": "没有标题为中文的商品（可能都已翻译）。"}
            r = box_delete(ids)
            return {"ok": r.get("ok", False), "action": action, "reply": "已删除 **%d** 个标题为中文的商品。" % (r.get("deleted") or 0)}
        if action == "box.delete_all":
            ids = _box_all_ids()
            if not ids:
                return {"ok": True, "action": action, "reply": "采集箱已经是空的。"}
            r = box_delete(ids)
            return {"ok": r.get("ok", False), "action": action, "reply": "已清空采集箱，删除 **%d** 个商品。" % (r.get("deleted") or 0)}
        if action == "box.translate":
            lang = params.get("lang") or templates_get().get("collect", {}).get("lang") or "英语"
            ids = _box_all_ids(chinese_only=(params.get("scope") != "all"))
            if not ids:
                return {"ok": True, "action": action, "reply": "没有需要翻译的商品。"}
            return {"ok": True, "action": action, "reply": pre + translate_box_items(ids, lang, bool(params.get("images")))}
        if action == "box.list_tiktok":
            shop = _default_tk_shop()
            if not shop:
                return {"ok": False, "action": action, "reply": "未配置 TikTok 店铺，请先在 采集箱→⚙ 模板配置 里选店铺。"}
            ids = _box_all_ids(chinese_only=False)
            if not ids:
                return {"ok": True, "action": action, "reply": "采集箱为空，没有可上架的商品。"}
            r = tk_list_items(ids, shop, templates_get().get("claim", {}).get("site", "MY"), bool(params.get("auto")))
            if not r.get("ok"):
                return {"ok": False, "action": action, "reply": "上架失败：" + (r.get("error") or "")}
            s = r.get("summary", {})
            return {"ok": True, "action": action,
                    "reply": pre + "上架处理完成：预填 %s · 发布 %s · 失败 %s（共 %s）。%s" % (
                        s.get("prepared", 0), s.get("published", 0), s.get("failed", 0), s.get("total", 0),
                        "" if params.get("auto") else "（未开自动发布，去 TikTok 后台确认类目后发布）")}
        if action == "pipeline":
            lang = params.get("lang") or templates_get().get("collect", {}).get("lang") or "英语"
            ids = _box_all_ids(chinese_only=(params.get("scope") != "all"))
            if not ids:
                return {"ok": True, "action": action, "reply": "采集箱为空或无需处理的商品。"}
            tr = translate_box_items(ids, lang, bool(params.get("images")))
            out = pre + "🔄 一条龙处理 %d 个商品：\n① 翻译：%s" % (len(ids), tr)
            shop = _default_tk_shop()
            if not shop:
                return {"ok": True, "action": action, "reply": out + "\n② 上架：跳过（未配置 TikTok 店铺，去 采集箱→⚙模板配置 选店铺）"}
            r = tk_list_items(ids, shop, templates_get().get("claim", {}).get("site", "MY"), bool(params.get("auto")))
            if r.get("ok"):
                s = r.get("summary", {})
                out += "\n② 上架：预填 %s · 发布 %s · 失败 %s（共 %s）%s" % (
                    s.get("prepared", 0), s.get("published", 0), s.get("failed", 0), s.get("total", 0),
                    "" if params.get("auto") else "（未开自动发布，去 TikTok 后台确认类目后发布）")
            else:
                out += "\n② 上架失败：" + (r.get("error") or "")
            return {"ok": True, "action": action, "reply": out}
        if action == "auto_collect":
            return _do_auto_collect(params, pre, message)
        if action == "analyze":
            r = analyze(params.get("keyword") or message, params.get("type") or "feasibility")
            return {"ok": r.get("ok", False), "action": action, "reply": r.get("reply") or r.get("error") or ""}
    except Exception as e:
        return {"ok": False, "action": action, "reply": "执行「%s」时出错：%s" % (action, str(e)[:160])}
    try:
        r = chat(message, "web", history)
        return {"ok": r.get("ok", True), "action": "chat", "reply": r.get("reply") or r.get("error") or ""}
    except Exception as e:
        return {"ok": False, "action": "chat", "reply": "对话出错：" + str(e)[:160]}


# ── 飞书(Lark)对接：网页配置 → 飞书webhook收消息 → agent_act执行 → 回飞书 + 同步网页 ──
_FEISHU_LOG = []           # [{id,role,text,ts}] 飞书会话，供网页实时显示
_FEISHU_SEEN = set()       # 去重 event_id
_FEISHU_TOKEN = {"tok": "", "exp": 0}


def _feishu_log_add(role, text):
    import time as _t
    nid = (_FEISHU_LOG[-1]["id"] + 1) if _FEISHU_LOG else 1
    _FEISHU_LOG.append({"id": nid, "role": role, "text": text, "ts": int(_t.time())})
    if len(_FEISHU_LOG) > 200:
        del _FEISHU_LOG[:len(_FEISHU_LOG) - 200]
    return nid


def _feishu_token():
    import time as _t, urllib.request as _u
    if _FEISHU_TOKEN["tok"] and _FEISHU_TOKEN["exp"] > _t.time():
        return _FEISHU_TOKEN["tok"]
    creds = _creds_raw()
    aid = (creds.get("FEISHU_APP_ID") or "").strip()
    sec = (creds.get("FEISHU_APP_SECRET") or "").strip()
    if not (aid and sec):
        return ""
    try:
        body = json.dumps({"app_id": aid, "app_secret": sec}).encode("utf-8")
        req = _u.Request("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                         data=body, headers={"Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        _FEISHU_TOKEN["tok"] = d.get("tenant_access_token") or ""
        _FEISHU_TOKEN["exp"] = _t.time() + min(int(d.get("expire") or 7000), 7000) - 120
        return _FEISHU_TOKEN["tok"]
    except Exception:
        return ""


def _feishu_send(chat_id, text):
    import urllib.request as _u
    tok = _feishu_token()
    if not (tok and chat_id):
        return False
    try:
        body = json.dumps({"receive_id": chat_id, "msg_type": "text",
                           "content": json.dumps({"text": text}, ensure_ascii=False)}).encode("utf-8")
        req = _u.Request("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                         data=body, headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8")).get("code") == 0
    except Exception:
        return False


def feishu_event(b):
    """飞书事件回调：URL验证(challenge) + 收消息 → agent_act 执行 → 回飞书 + 记录供网页同步。"""
    vt = (_creds_raw().get("FEISHU_VERIFY_TOKEN") or "").strip()
    if b.get("type") == "url_verification" or b.get("challenge"):
        if vt and b.get("token") and b.get("token") != vt:
            return {"code": -1}
        return {"challenge": b.get("challenge", "")}
    header = b.get("header") or {}
    if vt and header.get("token") and header.get("token") != vt:
        return {"code": -1}
    evt_id = header.get("event_id") or ""
    if evt_id:
        if evt_id in _FEISHU_SEEN:
            return {"code": 0}
        _FEISHU_SEEN.add(evt_id)
        if len(_FEISHU_SEEN) > 500:
            _FEISHU_SEEN.clear()
    if header.get("event_type") == "im.message.receive_v1":
        ev = b.get("event") or {}
        msg = ev.get("message") or {}
        chat_id = msg.get("chat_id")
        try:
            text = (json.loads(msg.get("content") or "{}").get("text") or "").strip()
        except Exception:
            text = ""
        text = re.sub(r"@_user_\d+\s*", "", text).strip()  # 去掉 @机器人 占位
        if text and chat_id:
            _feishu_log_add("user", text)
            try:
                reply = (agent_act(text) or {}).get("reply") or "（无回复）"
            except Exception as e:
                reply = "执行出错：" + str(e)[:120]
            _feishu_log_add("assistant", reply)
            _feishu_send(chat_id, reply)
    return {"code": 0}


def feishu_messages(since):
    try:
        since = int(since)
    except Exception:
        since = 0
    creds = _creds_raw()
    return {"ok": True, "enabled": bool(creds.get("FEISHU_APP_ID")),
            "messages": [m for m in _FEISHU_LOG if m["id"] > since],
            "last": (_FEISHU_LOG[-1]["id"] if _FEISHU_LOG else 0)}


# ── 全自动采集任务队列：对话下发 → 插件轮询执行(#4/#8) ──────────────────
_COLLECT_JOBS = []


def collect_job_create(opts):
    import time as _t
    jid = (_COLLECT_JOBS[-1]["id"] + 1) if _COLLECT_JOBS else 1
    _COLLECT_JOBS.append({"id": jid, "status": "pending", "opts": opts or {}, "result": None, "ts": int(_t.time())})
    if len(_COLLECT_JOBS) > 50:
        del _COLLECT_JOBS[:len(_COLLECT_JOBS) - 50]
    return {"ok": True, "id": jid}


def collect_job_poll():
    for j in _COLLECT_JOBS:
        if j["status"] == "pending":
            j["status"] = "running"
            return {"ok": True, "job": {"id": j["id"], "opts": j["opts"]}}
    return {"ok": True, "job": None}


def collect_job_done(jid, result):
    for j in _COLLECT_JOBS:
        if j["id"] == int(jid or 0):
            j["status"] = "done"
            j["result"] = result
    return {"ok": True}


def translate_read(url):
    url = (url or "").strip()
    if "/offer/" not in url:
        return {"ok": False, "error": "请粘贴有效的 1688 商品链接"}
    code, out, err = _run(SELECT_CMD + ["--mode", "images", "--urls", url], timeout=90)
    if code != 0:
        return {"ok": False, "error": (err or out or "读取失败").strip().splitlines()[-1]}
    try:
        d = json.loads(out.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "解析失败"}
    if not d.get("imgUrls"):
        return {"ok": False, "error": "未读取到商品图片（链接无效或采集超时，请重试）"}
    d["ok"] = True
    return d


# ── 智能分析 / 生图 / 生视频（经 agent） ─────────────────────────────
_AGENT_SEQ = 0

def _agent(prompt, tag="ana", timeout=200):
    """分析/报告类调用，统一走阿里通义千问（DashScope）。"""
    txt = _ali_chat([{"role": "system", "content": _CHAT_SYS}, {"role": "user", "content": prompt}],
                    max_tokens=2600, temperature=0.6, timeout=min(timeout, 150))
    if not txt:
        return {"ok": False, "error": "阿里模型无返回（请到后台确认 DashScope Key 与额度）"}
    return {"ok": True, "reply": txt}


ANALYSIS_PROMPTS = {
    "blue_ocean": "蓝海机会挖掘：针对「{kw}」，优先找高销量、评论少、竞争小、利润空间大的潜力蓝海方向，给结论、切入空间与风险。",
    "voc": "竞品差评 VOC 量化分析：针对「{kw}」，结构化提取人群画像、使用场景、核心痛点与未满足需求，输出可执行的产品改良方向。",
    "feasibility": "选品可行性分析：针对「{kw}」，从市场趋势、价格带分布、供给竞争三个维度评估是否值得切入，给出结论与风险。",
    "compare": "竞品对比分析：针对「{kw}」，对比主要竞品的卖点、定价、评分与差评，找出差异化切入点与改进机会。",
    "listing": "Listing 与卖点生成：针对「{kw}」，生成符合 TikTok/Ozon 平台规则的高转化标题与五点卖点（中英文）。",
    "pricing": "定价与利润测算：针对「{kw}」，结合采购成本、平台佣金、物流与退货率，测算毛利并给出定价区间建议。",
}


def analyze(kw, atype):
    tmpl = ANALYSIS_PROMPTS.get(atype) or ANALYSIS_PROMPTS["feasibility"]
    prompt = tmpl.format(kw=kw or "该品类/产品") + " 用简体中文 Markdown 输出（标题 + 表格 + 要点），务实、结论先行、可执行。"
    r = _agent(prompt, "ana")
    if r.get("ok") and r.get("reply"):
        try:
            os.makedirs(REPORTS_DIR, exist_ok=True)
            import datetime as _dt
            fn = "分析_%s_%s_%s.md" % (atype, re.sub(r"[^\w一-鿿]+", "", (kw or "")[:16]) or "report",
                                       _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
            open(os.path.join(REPORTS_DIR, fn), "w", encoding="utf-8").write(r["reply"])
        except Exception:
            pass
    return r


_IMG_SIZE = {"1:1": "1024x1024", "3:4": "768x1024", "4:3": "1024x768",
             "16:9": "1280x720", "9:16": "720x1280"}
# ── 阿里云百炼 Wan 生图（移植自「千相工坊」，国内可用、同步返回）──────────
_WAN_IMG_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_WAN_SIZE = {"1:1": "1024*1024", "3:4": "768*1024", "4:3": "1024*768",
             "16:9": "1280*720", "9:16": "720*1280"}
# 生图风格（文生图 / 图生图通用）
_STYLE_PROMPTS = {
    "白底主图": "Clean white or very light gray marketplace main image, product centered, premium soft shadow, subtle floor reflection, crisp edges, high-end product photography, no clutter.",
    "场景图": "Realistic lifestyle scene matching the product category, product placed in a natural use environment with suitable props, depth of field, warm realistic lighting, designed background.",
    "特写图": "Professional studio macro close-up, dramatic softbox lighting, shallow depth of field, emphasize texture and material detail, premium commercial look.",
    "模特图": "A realistic human model naturally using or wearing the product, lifestyle commercial photography, soft natural light, keep product details accurate.",
    "信息图": "Designed feature-focused image, clean layout with 2-3 minimal callout lines and small icons, light-colored designed background, premium layout, only short 1-2 word labels.",
    "卖点图": "Selling-point poster, bold short text overlays (max 3 short phrases), dynamic composition, designed gradient background, eye-catching commercial layout.",
    "A+图": "A+ content style premium catalog image, elegant gradient background, layered geometric blocks, soft spotlight, refined high-end magazine composition.",
}
_CREATIVE_PROMPTS = {
    "均衡": "Keep the product accurate. Improve lighting, background, shadow and composition moderately.",
    "创意": "Keep the product accurate. Create a designed e-commerce image with richer background, props, depth, premium lighting and strong visual hierarchy.",
    "大胆": "Keep the product accurate. Create a highly attractive commercial advertising image with dramatic lighting, premium scene, props and eye-catching composition. Do not change the product itself.",
}
_SCENE_HINTS = [
    (("耳机", "音箱", "蓝牙", "数码", "电子", "3c", "充电", "手机", "键盘", "鼠标", "headphone", "earbud"),
     "clean desk setup, modern tech workspace, minimal surface, geometric backdrop."),
    (("连衣裙", "女装", "男装", "卫衣", "t恤", "衬衫", "裤", "裙", "上衣", "服", "穿", "dress", "apparel"),
     "fashion catalog background, soft fabric drape, lifestyle wardrobe scene."),
    (("鞋", "运动鞋", "靴", "shoe", "sneaker"), "street ground scene, clean studio platform, minimal concrete surface."),
    (("包", "背包", "手提", "bag"), "commuter desk scene, travel setting, premium catalog platform."),
    (("家居", "家具", "收纳", "厨房", "沙发", "台灯", "杯", "壶", "锅", "home", "kitchen"),
     "modern home interior, kitchen counter or living room, cozy natural light."),
    (("美妆", "化妆", "护肤", "面膜", "口红", "beauty", "cosmetic"),
     "vanity table scene, marble surface, elegant minimal beauty environment."),
    (("宠物", "猫", "狗", "pet"), "warm home pet scene, cozy rug, natural light home environment."),
    (("玩具", "儿童", "母婴", "toy", "kid"), "bright playful scene, soft colorful background, clean safe environment."),
    (("珠宝", "首饰", "项链", "戒指", "手表", "jewel", "watch"),
     "jewelry display platform, velvet surface, premium showcase, macro detail."),
]
_NEG_PROMPT = ("Avoid: different product, changed color, removed print, fake logo, watermark, price tag, "
               "SKU code, random unreadable text, website UI, deformed product, busy cluttered background.")


def _scene_hint(text):
    t = (text or "").lower()
    for keys, hint in _SCENE_HINTS:
        if any(k in t for k in keys):
            return hint
    return "clean product platform, premium catalog background, minimal modern environment."


def _build_img_prompt(gtype, desc, info, brand, creative, has_ref):
    style = _STYLE_PROMPTS.get(gtype, _STYLE_PROMPTS["白底主图"])
    crea = _CREATIVE_PROMPTS.get(creative, _CREATIVE_PROMPTS["创意"])
    scene = _scene_hint((desc or "") + " " + (info or ""))
    if has_ref:
        head = ("Create a professional e-commerce product image for a cross-border listing (TikTok Shop / Ozon). "
                "Use the uploaded image as the EXACT product reference: preserve its color, shape, material, logo, "
                "pattern and texture. Redesign only the scene, lighting, background, props and composition."
                + ((" Extra info: %s." % info) if info else ""))
    else:
        head = ("Create a professional e-commerce product image for a cross-border listing (TikTok Shop / Ozon). "
                "Product: %s.%s%s High quality, sharp focus, commercial product photography."
                % (desc or "the product",
                   (" Details: %s." % info) if info else "",
                   (" Brand: %s." % brand) if brand else ""))
    return "\n".join([head, "Style: " + style, "Creative: " + crea, "Scene: " + scene, _NEG_PROMPT])


def _save_media(it, tag, kind="img"):
    import base64, datetime, urllib.request as _u
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    ext = ".mp4" if kind == "vid" else ".png"
    name = "%s_%s_%s%s" % (kind, ts, re.sub(r"[^\w]+", "", tag)[:8], ext)
    p = os.path.join(MEDIA_DIR, name)
    try:
        if it.get("url"):
            data = _u.urlopen(it["url"], timeout=150).read()
        elif it.get("b64_json"):
            raw = it["b64_json"]
            if isinstance(raw, str) and raw.startswith("data:") and "," in raw:
                raw = raw.split(",", 1)[1]
            data = base64.b64decode(raw)
        else:
            return None
        with open(p, "wb") as f:
            f.write(data)
        return name
    except Exception:
        return None


def gen_image(b):
    creds = _creds_raw()
    desc = (b.get("desc") or "").strip()
    info = (b.get("info") or "").strip()
    brand = (b.get("brand") or "").strip()
    gtypes = [g for g in (b.get("gtypes") or ["白底主图"]) if g] or ["白底主图"]
    creative = b.get("creative") or "创意"
    ratio = b.get("ratio") or "1:1"
    qty = max(1, min(int(b.get("qty") or 1), 4))
    ref_b64 = (b.get("ref_b64") or "").strip()        # 图生图参考图(data_url)
    ds_key = (creds.get("DASHSCOPE_API_KEY") or "").strip()
    if ds_key:                                         # 首选：阿里云百炼 Wan(国内可用)
        if not (desc or ref_b64):
            return {"ok": False, "error": "请先填写商品描述，或上传参考图。"}
        return _gen_image_wan(ds_key, creds, gtypes, desc, info, brand, creative, ratio, qty, ref_b64)
    return _gen_image_openai(creds, gtypes, desc, info, brand, ratio, qty)  # 回退：OpenAI 兼容


def _gen_image_wan(key, creds, gtypes, desc, info, brand, creative, ratio, qty, ref_b64):
    import urllib.request as _u, urllib.error as _ue
    model = (creds.get("WAN_IMAGE_MODEL") or "wan2.7-image-pro").strip()
    size = _WAN_SIZE.get(ratio, "1024*1024")
    saved, errs = [], []
    for gt in gtypes:
        prompt = _build_img_prompt(gt, desc, info, brand, creative, bool(ref_b64))
        content = ([{"image": ref_b64}] if ref_b64 else []) + [{"text": prompt}]
        body = json.dumps({"model": model,
                           "input": {"messages": [{"role": "user", "content": content}]},
                           "parameters": {"size": size, "n": max(1, min(qty, 4)),
                                          "prompt_extend": True, "watermark": False}}).encode("utf-8")
        try:
            req = _u.Request(_WAN_IMG_ENDPOINT, data=body,
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                             method="POST")
            with _u.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("code"):
                errs.append("%s：%s %s" % (gt, data.get("code"), str(data.get("message"))[:120]))
                continue
            got = 0
            for ch in ((data.get("output") or {}).get("choices") or []):
                for it in ((ch.get("message") or {}).get("content") or []):
                    img = it.get("image")
                    if not img:
                        continue
                    n = _save_media({"url": img} if str(img).startswith("http") else {"b64_json": img}, gt, "img")
                    if n:
                        saved.append(n); got += 1
            if not got:
                errs.append("%s：未返回图片" % gt)
        except _ue.HTTPError as he:
            try:
                bt = he.read().decode("utf-8", "ignore")[:240]
            except Exception:
                bt = ""
            errs.append("%s：HTTP %s %s" % (gt, he.code, bt or he.reason))
        except Exception as e:
            errs.append("%s：%s" % (gt, str(e)[:160]))
    if not saved:
        return {"ok": False, "error": "生成失败(百炼Wan)：" + ("；".join(errs) or "无返回，检查 DashScope Key/模型")}
    return {"ok": True, "images": saved, "engine": "百炼 " + model,
            "note": ("部分失败：" + "；".join(errs)) if errs else ""}


def _gen_image_openai(creds, gtypes, desc, info, brand, ratio, qty):
    import urllib.request as _u, urllib.error as _ue
    base = (creds.get("IMG_BASE_URL") or "").strip().rstrip("/")
    key = creds.get("IMG_API_KEY") or ""
    model = creds.get("IMG_MODEL") or "doubao-seedream-3-0-t2i"
    size = _IMG_SIZE.get(ratio, "1024x1024")
    if not (base and key):
        return {"ok": False, "error": "未配置生图 API。请在『管理后台 → 生图模型』填写 阿里云百炼 DashScope Key（推荐），或 OpenAI 兼容 Base URL / API Key。"}
    saved, errs = [], []
    for gt in gtypes:
        prompt = _build_img_prompt(gt, desc, info, brand, "创意", False)
        try:
            payload = json.dumps({"model": model, "prompt": prompt, "n": max(1, min(qty, 6)), "size": size}).encode("utf-8")
            req = _u.Request(base + "/images/generations", data=payload,
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                             method="POST")
            with _u.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
            got = 0
            for it in (data.get("data") or []):
                n = _save_media(it, gt, "img")
                if n:
                    saved.append(n); got += 1
            if not got:
                errs.append("%s：接口返回但无图片数据" % gt)
        except _ue.HTTPError as he:
            try:
                bt = he.read().decode("utf-8", "ignore")[:240]
            except Exception:
                bt = ""
            errs.append("%s：HTTP %s %s" % (gt, he.code, bt or he.reason))
        except Exception as e:
            errs.append("%s：%s" % (gt, str(e)[:160]))
    if not saved:
        return {"ok": False, "error": "生成失败：" + ("；".join(errs) or "接口无返回，检查 Base URL/Key/模型")}
    return {"ok": True, "images": saved, "note": ("部分失败：" + "；".join(errs)) if errs else ""}


# ── 阿里云百炼 Wan 视频（文生视频 / 图生视频，异步任务）─────────────────
_WAN_VID_SUBMIT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
_WAN_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/"
_VID_SIZE = {"16:9": "1920*1080", "9:16": "1080*1920", "1:1": "1440*1440",
             "4:3": "1632*1248", "3:4": "1248*1632"}
_VID_TYPE_HINT = {
    "商品展示": "clean product showcase with slow rotation, studio lighting, e-commerce hero shot",
    "开箱": "unboxing style, hands opening the package and revealing the product, cozy lighting",
    "使用场景": "product used naturally in a real lifestyle scene, soft natural light",
    "卖点讲解": "dynamic feature highlight, smooth camera moves emphasizing key selling points",
}


def _build_video_prompt(desc, gtype):
    hint = _VID_TYPE_HINT.get(gtype, _VID_TYPE_HINT["商品展示"])
    return ("Cross-border e-commerce product video for TikTok / Ozon. Product: %s. Style: %s. "
            "Smooth professional camera motion, commercial quality, no watermark, no text overlay."
            % (desc or "the product", hint))


def gen_video(b):
    creds = _creds_raw()
    desc = (b.get("desc") or "").strip()
    gtype = b.get("gtype") or "商品展示"
    ratio = b.get("ratio") or "16:9"
    ref_b64 = (b.get("ref_b64") or "").strip()
    try:
        duration = max(5, min(15, int(b.get("duration") or 5)))
    except Exception:
        duration = 5
    ds_key = (creds.get("DASHSCOPE_API_KEY") or "").strip()
    if ds_key:
        if not (desc or ref_b64):
            return {"ok": False, "error": "请填写商品描述/脚本，或上传参考图（图生视频）。"}
        return _gen_video_wan(ds_key, creds, desc, gtype, ratio, ref_b64, duration)
    return _gen_video_agent(desc, gtype)


def _gen_video_wan(key, creds, desc, gtype, ratio, ref_b64, duration=5):
    import urllib.request as _u, urllib.error as _ue, time as _t
    is_i2v = bool(ref_b64)
    model = (creds.get("VID_MODEL") or "").strip() or ("wan2.2-i2v-plus" if is_i2v else "wan2.2-t2v-plus")
    inp = {"prompt": _build_video_prompt(desc, gtype)}
    params = {"duration": int(duration)}   # 百炼 Wan 视频时长(秒)
    if is_i2v:
        inp["img_url"] = ref_b64
        params["resolution"] = "1080P"
    else:
        params["size"] = _VID_SIZE.get(ratio, "1920*1080")
    body = json.dumps({"model": model, "input": inp, "parameters": params}).encode("utf-8")
    try:
        req = _u.Request(_WAN_VID_SUBMIT, data=body,
                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                  "X-DashScope-Async": "enable"}, method="POST")
        with _u.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except _ue.HTTPError as he:
        try:
            bt = he.read().decode("utf-8", "ignore")[:240]
        except Exception:
            bt = ""
        return {"ok": False, "error": "视频提交失败 HTTP %s %s" % (he.code, bt or he.reason)}
    except Exception as e:
        return {"ok": False, "error": "视频提交失败：" + str(e)[:160]}
    if data.get("code"):
        return {"ok": False, "error": "视频提交失败：%s %s" % (data.get("code"), str(data.get("message"))[:160])}
    task_id = (data.get("output") or {}).get("task_id")
    if not task_id:
        return {"ok": False, "error": "未拿到任务ID：" + json.dumps(data)[:160]}
    last = ""
    for _ in range(75):                      # 75 × 6s ≈ 7.5 分钟
        _t.sleep(6)
        try:
            pd = json.loads(_u.urlopen(_u.Request(_WAN_TASK_URL + task_id,
                            headers={"Authorization": "Bearer " + key}), timeout=30).read().decode("utf-8"))
        except Exception:
            continue
        out = pd.get("output") or {}
        st = out.get("task_status") or last
        last = st
        if st == "SUCCEEDED":
            vu = out.get("video_url")
            if not vu and isinstance(out.get("results"), list) and out["results"]:
                vu = out["results"][0].get("video_url")
            if not vu:
                return {"ok": False, "error": "完成但未返回视频地址"}
            name = _save_media({"url": vu}, gtype, "vid")
            if not name:
                return {"ok": False, "error": "视频下载保存失败", "video_url": vu}
            return {"ok": True, "videos": [name], "engine": "百炼 " + model}
        if st in ("FAILED", "CANCELED", "UNKNOWN"):
            return {"ok": False, "error": "生成失败：" + str(out.get("message") or st)}
    return {"ok": False, "error": "生成超时（约 7 分钟未完成，状态：%s），可稍后重试。" % last}


def _gen_video_agent(desc, gtype):
    prompt = ("请用 byted-seedance-video-generate 技能，为跨境电商商品生成带货短视频。"
              "商品/描述：%s。视频类型：%s。完成后返回视频链接或保存路径；若需要密钥未配置，请明确告知。"
              % (desc or "（未填）", gtype or "商品展示"))
    return _agent(prompt, "vid", timeout=300)


# ── 智能体规划层（意图识别 + 任务拆解 + 飞猴，移植自「千相工坊」）──────────
_PLAN_MODULES = {
    "hot-selection": {"label": "热销榜选品", "tab": "hot", "emoji": "🔥"},
    "selection":     {"label": "1688找货源/采集箱", "tab": "box", "emoji": "🛒"},
    "analysis":      {"label": "智能分析", "tab": "analysis", "emoji": "🧭"},
    "listing":       {"label": "Listing与卖点", "tab": "analysis", "emoji": "📝"},
    "main-image":    {"label": "AI商品主图", "tab": "image", "emoji": "🎨"},
    "video":         {"label": "AI带货视频", "tab": "video", "emoji": "🎬"},
}
_PLAN_SYSTEM = """你是「飞猴」——专注 Ozon 与 TikTok Shop 跨境电商的选品与素材 AI 助手。
根据用户需求做：意图识别 + 任务拆解 + 必要飞猴。只输出 JSON，禁止 Markdown、禁止 JSON 以外的任何文字。

可用功能模块（module 只能取以下值）：
- hot-selection：热销榜选品（找爆款/热卖趋势）
- selection：1688找货源 / 采集箱（采集候选并AI评分）
- analysis：智能分析（可行性 / 竞品VOC差评 / 竞品对比 / 定价利润）
- listing：生成 Listing 标题与五点卖点（中英）
- main-image：AI 生成商品主图 / 场景图 / 卖点图
- video：AI 生成带货短视频（文生/图生）

返回 JSON：
{"intent":"plan","needsClarification":false,"clarifyingQuestions":[],
 "reply":"中文简短回复","analysis":{"productType":"","platform":"","goal":""},
 "tasks":[{"title":"","description":"","module":"main-image","priority":"P0","reason":""}],
 "suggestions":[]}

规则：
1. 闲聊/问身份/问能力：tasks 为空 []，reply 自然简短。
2. 只有当“连商品品类都没提到”时才飞猴（needsClarification=true，最多 4 条 clarifyingQuestions，tasks 为空）。
3. 只要用户给了品类（如蓝牙耳机、连衣裙、保温杯）并表达了做素材/上架/选品/分析等目标，就视为信息充分，needsClarification=false，直接拆 2-6 个 tasks，不要再飞猴。哪怕只说“做一套上架素材”，只要有品类就直接规划完整流程。
4. 每个 task 含 priority(P0/P1/P2) 与 reason，按执行顺序排列。完整上架流程顺序：hot-selection 或 selection → analysis → listing → main-image → video（服装类务必含 main-image，并可加 video）。
5. module 只能用上面列出的值，禁止编造。
6. reply 用中文、简洁专业。只输出 JSON。"""


def _plan_normalize(obj):
    tasks = []
    for t in (obj.get("tasks") or [])[:6]:
        if not isinstance(t, dict):
            continue
        mod = str(t.get("module") or "").strip()
        if mod not in _PLAN_MODULES:
            continue
        meta = _PLAN_MODULES[mod]
        pr = t.get("priority") if t.get("priority") in ("P0", "P1", "P2") else "P1"
        tasks.append({"title": str(t.get("title") or meta["label"])[:40],
                      "description": str(t.get("description") or "")[:160],
                      "module": mod, "tab": meta["tab"], "emoji": meta["emoji"],
                      "priority": pr, "reason": str(t.get("reason") or "")[:140]})
    cq = [str(q)[:80] for q in (obj.get("clarifyingQuestions") or []) if isinstance(q, str) and q.strip()][:4]
    sg = [str(s)[:90] for s in (obj.get("suggestions") or []) if isinstance(s, str) and s.strip()][:5]
    an = obj.get("analysis") if isinstance(obj.get("analysis"), dict) else {}
    return {"ok": True, "intent": str(obj.get("intent") or "")[:40],
            "needsClarification": bool(obj.get("needsClarification")),
            "clarifyingQuestions": cq, "reply": str(obj.get("reply") or "")[:600],
            "analysis": an, "tasks": tasks, "suggestions": sg, "engine": "deepseek"}


def _plan_call_model(message, history):
    """任务规划也走阿里通义千问（DashScope）。"""
    msgs = [{"role": "system", "content": _PLAN_SYSTEM}]
    for h in (history or [])[-6:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) and h["content"].strip():
            msgs.append({"role": h["role"], "content": h["content"][:2000]})
    msgs.append({"role": "user", "content": message[:2000]})
    content = _ali_chat(msgs, max_tokens=1500, temperature=0.4, timeout=60)
    obj = _loose_json(content) if content else None
    return _plan_normalize(obj) if isinstance(obj, dict) else None


def _plan_fallback(message, history):
    txt = ((message or "") + " " + " ".join(h.get("content", "") for h in (history or []) if isinstance(h, dict))).lower()

    def has(*ks):
        return any(k in txt for k in ks)
    tasks = []

    def add(mod, title, desc, pr, reason):
        m = _PLAN_MODULES[mod]
        tasks.append({"title": title, "description": desc, "module": mod, "tab": m["tab"],
                      "emoji": m["emoji"], "priority": pr, "reason": reason})
    full = has("一套", "整套", "全流程", "完整", "方案", "上架", "素材", "全部")
    if has("热销", "爆款", "热卖", "榜") or full:
        add("hot-selection", "热销榜找爆款", "在热销榜按站点/类目找高潜爆款", "P0", "先看大盘热卖趋势定方向")
    if has("货源", "1688", "采集", "找同款", "选品") or full:
        add("selection", "1688找货源并评分", "采集候选并 AI 选品评分入采集箱", "P1", "锁定可落地的供货款")
    if has("可行", "竞品", "差评", "voc", "痛点", "定价", "利润", "分析") or full:
        add("analysis", "智能分析", "可行性 / 竞品VOC / 定价利润评估", "P1", "用数据判断是否值得做、怎么做")
    if has("标题", "卖点", "listing", "文案") or full:
        add("listing", "生成Listing与卖点", "出 TikTok/Ozon 高转化标题与五点卖点", "P1", "好标题决定搜索与点击")
    if has("主图", "白底", "场景图", "作图", "生图", "图片") or full:
        add("main-image", "AI商品主图", "按描述/参考图生成主图与套图", "P1", "主图决定点击率")
    if has("视频", "短视频", "带货视频", "图生视频") or full:
        add("video", "AI带货视频", "生成商品展示/卖点短视频", "P2", "视频用于详情与广告投放")
    need = not tasks
    cq = (["做什么品类的商品？", "目标平台是 Ozon 还是 TikTok？", "需要哪些：选品/分析/主图/视频/Listing？"]
          if need else [])
    reply = "我帮你把这件事拆成可执行的步骤👇" if tasks else "请补充一下商品和目标，我来帮你拆解执行步骤。"
    return {"ok": True, "intent": "plan" if tasks else "clarify", "needsClarification": need,
            "clarifyingQuestions": cq, "reply": reply, "analysis": {}, "tasks": tasks,
            "suggestions": (["先用热销榜定方向，再用采集箱找货源", "服装类建议加模特图，3C类重视场景图"] if tasks else []),
            "engine": "local"}


def agent_plan(b):
    message = (b.get("message") or "").strip()
    history = b.get("history") or []
    if not message:
        return {"ok": False, "error": "消息为空"}
    r = _plan_call_model(message, history) or _plan_fallback(message, history)
    if r.get("ok") and r.get("tasks"):
        r["taskRecordIds"] = _tasks_persist(r, message)
    return r


# ── Agent 任务中心（持久化 + 状态追踪，移植自「千相工坊」）──────────────
TASKS_PATH = os.path.join(DATA_DIR, "agent_tasks.json")
_tasks_lock = threading.Lock()


def _tasks_load():
    try:
        with open(TASKS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _tasks_save(ts):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump(ts, f, ensure_ascii=False, indent=2)


def _tasks_persist(plan, src_msg):
    import time as _t
    tasks = plan.get("tasks") or []
    if not tasks:
        return []
    base = str(int(_t.time() * 1000))
    with _tasks_lock:
        store = _tasks_load()
        ids, prev = [], None
        n = len(tasks)
        for i, t in enumerate(tasks):
            tid = "t_%s_%d" % (base, i + 1)
            store.append({"id": tid, "title": t.get("title"), "module": t.get("module"),
                          "tab": t.get("tab"), "emoji": t.get("emoji"), "priority": t.get("priority"),
                          "reason": t.get("reason"), "description": t.get("description"),
                          "status": "pending", "stepIndex": i + 1, "totalSteps": n, "progress": 0,
                          "dependsOn": prev, "createdAt": base, "fromMsg": (src_msg or "")[:60]})
            ids.append(tid)
            prev = tid
        _tasks_save(store[-200:])           # 只保留最近 200 条
        return ids


def agent_tasks_list():
    store = sorted(_tasks_load(), key=lambda x: x.get("createdAt", ""), reverse=True)[:60]
    stats = {"total": len(store), "pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
    for t in store:
        st = t.get("status", "pending")
        stats[st] = stats.get(st, 0) + 1
    return {"ok": True, "tasks": store, "stats": stats}


def agent_task_update(b):
    tid = b.get("id")
    status = b.get("status")
    progress = b.get("progress")
    with _tasks_lock:
        store = _tasks_load()
        idx = {t.get("id"): t for t in store}
        t = idx.get(tid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        if status:
            if status not in ("pending", "in_progress", "completed", "cancelled"):
                return {"ok": False, "error": "状态非法"}
            if status == "in_progress" and t.get("dependsOn"):
                dep = idx.get(t["dependsOn"])
                if dep and dep.get("status") != "completed":
                    return {"ok": False, "error": "请先完成前置任务"}
            t["status"] = status
            if progress is None:
                t["progress"] = {"pending": 0, "completed": 100,
                                 "in_progress": max(t.get("progress", 0), 10)}.get(status, t.get("progress", 0))
        if progress is not None:
            try:
                t["progress"] = max(0, min(100, int(progress)))
            except Exception:
                pass
        _tasks_save(store)
        return {"ok": True, "task": t}


# ── 资产库（盒子上的数据 / 报告 / 媒体） ─────────────────────────────
def assets_list():
    import glob
    out = {"reports": [], "images": [], "videos": [], "ingest": None}
    try:
        if os.path.isdir(REPORTS_DIR):
            for p in sorted(glob.glob(os.path.join(REPORTS_DIR, "*.md")), reverse=True)[:80]:
                out["reports"].append({"name": os.path.basename(p), "size_kb": os.path.getsize(p) // 1024})
        if os.path.isdir(MEDIA_DIR):
            for p in sorted(glob.glob(os.path.join(MEDIA_DIR, "*")), reverse=True)[:120]:
                n = os.path.basename(p)
                ext = n.lower().rsplit(".", 1)[-1] if "." in n else ""
                if ext in ("png", "jpg", "jpeg", "webp", "gif"):
                    out["images"].append({"name": n})
                elif ext in ("mp4", "mov", "webm"):
                    out["videos"].append({"name": n})
        ing = os.path.join(DATA_DIR, "last_1688_ingest.json")
        if os.path.exists(ing):
            d = json.load(open(ing, encoding="utf-8"))
            out["ingest"] = {"count": len(d) if isinstance(d, list) else 0}
    except Exception:
        pass
    return out


def _asset_path(atype, name):
    name = os.path.basename(name or "")
    base = {"report": REPORTS_DIR, "media": MEDIA_DIR}.get(atype)
    if not base or not name:
        return None
    p = os.path.join(base, name)
    if os.path.abspath(p).startswith(os.path.abspath(base)) and os.path.exists(p):
        return p
    return None


def asset_delete(atype, name):
    p = _asset_path(atype, name)
    if not p:
        return {"ok": False, "error": "文件不存在"}
    try:
        os.remove(p)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def asset_read(atype, name):
    p = _asset_path(atype, name)
    if not p:
        return {"ok": False, "error": "文件不存在"}
    try:
        return {"ok": True, "content": open(p, encoding="utf-8").read()[:30000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def extract_keywords(titles):
    titles = [str(t) for t in (titles or []) if t][:20]
    if not titles:
        return {"ok": False, "error": "无商品"}
    lst = "\n".join("%d. %s" % (i + 1, t[:60]) for i, t in enumerate(titles))
    prompt = ("你是 1688 选品采购专家。为下面每个跨境电商热销商品标题，提炼 2-4 个精准的中文 1688 搜索关键词"
              "（用于在 1688 找到同款/相似货源）。只返回 JSON 数组："
              "[{\"i\":序号从1开始, \"keywords\":[\"词1\",\"词2\"]}]，不要解释、不要markdown。\n商品：\n" + lst)
    r = _agent(prompt, "kw", timeout=120)
    if not r.get("ok"):
        return r
    arr = _loose_json(r.get("reply", ""))
    by_i = {}
    if isinstance(arr, list):
        for it in arr:
            if isinstance(it, dict) and "i" in it:
                try:
                    by_i[int(it["i"])] = it.get("keywords") or []
                except Exception:
                    pass
    items = [{"title": t, "keywords": by_i.get(i + 1, [])} for i, t in enumerate(titles)]
    return {"ok": True, "items": items}


# ── HTTP 服务 ────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not ZHI_WEB_PASS:
            return True
        return ("zw=" + _WEB_COOKIE) in (self.headers.get("Cookie") or "")

    def _authed_or_pass(self, b):
        return self._authed() or ((b or {}).get("pass") or "") == ZHI_WEB_PASS

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        # 售卖版许可门控：未激活/到期/停用时，GET 一律拦截，只放行 首页(转激活页) 和 logo
        if not _license_ok() and self.path != "/logo" and not self.path.startswith("/logo?") \
                and self.path != "/" and not self.path.startswith("/?"):
            return self._send(403, json.dumps({"ok": False, "error": "license",
                                               "msg": "盒子未激活或授权已到期"}, ensure_ascii=False))
        if self.path.startswith("/asset/file"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            p = _asset_path((q.get("type") or ["media"])[0], (q.get("name") or [""])[0])
            if not p:
                return self._send(404, json.dumps({"error": "not found"}))
            ext = p.lower().rsplit(".", 1)[-1] if "." in p else ""
            ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                  "gif": "image/gif", "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}.get(ext, "application/octet-stream")
            try:
                return self._send(200, open(p, "rb").read(), ct)
            except Exception:
                return self._send(404, json.dumps({"error": "read error"}))
        if self.path == "/logo" or self.path.startswith("/logo?"):   # 品牌logo（公开；有上传用上传，否则默认SVG）
            try:
                if os.path.exists(LOGO_PATH):
                    return self._send(200, open(LOGO_PATH, "rb").read(), "image/png")
            except Exception:
                pass
            return self._send(200, _LOGO_SVG, "image/svg+xml")
        # 登录门控（设置了 ZHI_WEB_PASS 才生效）
        if ZHI_WEB_PASS and not self._authed():
            if self.path == "/" or self.path.startswith("/?"):
                return self._send(200, WEB_LOGIN_HTML, "text/html; charset=utf-8")
            return self._send(401, json.dumps({"error": "auth"}))
        if self.path == "/" or self.path.startswith("/?"):
            if not _license_ok():   # 售卖版：未激活/到期/停用 → 激活页
                return self._send(200, ACTIVATE_HTML, "text/html; charset=utf-8")
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, json.dumps(status(), ensure_ascii=False))
        if self.path == "/api/creds":
            return self._send(200, json.dumps(load_creds(), ensure_ascii=False))
        if self.path == "/api/assets":
            return self._send(200, json.dumps(assets_list(), ensure_ascii=False))
        if self.path == "/api/agent/tasks":
            return self._send(200, json.dumps(agent_tasks_list(), ensure_ascii=False))
        if self.path == "/api/templates":
            return self._send(200, json.dumps({"ok": True, "templates": templates_get()}, ensure_ascii=False))
        if self.path == "/api/tk/shops":
            return self._send(200, json.dumps(tk_shops(), ensure_ascii=False))
        if self.path == "/download/extension":
            import io, zipfile
            base = os.path.expanduser("~/ext/zhuiwen-1688")
            if not os.path.isdir(base):
                return self._send(404, json.dumps({"error": "扩展未部署到盒子"}))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(base):
                    for f in files:
                        fp = os.path.join(root, f)
                        z.write(fp, "zhuiwen-1688/" + os.path.relpath(fp, base))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=zhuiwen-caiji-extension.zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/asset/file"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            p = _asset_path((q.get("type") or ["media"])[0], (q.get("name") or [""])[0])
            if not p:
                return self._send(404, json.dumps({"error": "not found"}))
            ext = p.lower().rsplit(".", 1)[-1] if "." in p else ""
            ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                  "gif": "image/gif", "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}.get(ext, "application/octet-stream")
            try:
                return self._send(200, open(p, "rb").read(), ct)
            except Exception:
                return self._send(404, json.dumps({"error": "read error"}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        b = self._json_body()
        if self.path == "/api/login":
            if ZHI_WEB_PASS and (b.get("pass") or "") == ZHI_WEB_PASS:
                return self._send(200, json.dumps({"ok": True}),
                                  cookie="zw=" + _WEB_COOKIE + "; Path=/; Max-Age=2592000; HttpOnly")
            return self._send(200, json.dumps({"ok": False}))
        # 售卖版许可门控：未激活只放行 登录/激活/许可查询，其余一律拒绝
        if not _license_ok() and self.path not in ("/api/login", "/api/activate", "/api/license"):
            return self._send(200, json.dumps({"ok": False, "error": "license",
                                               "msg": "盒子未激活或授权已到期，请在首页输入激活码"}, ensure_ascii=False))
        if self.path == "/api/feishu/event":   # 飞书回调（公开，飞书服务器无法登录）
            return self._send(200, json.dumps(feishu_event(b), ensure_ascii=False))
        if self.path == "/api/hot-data":       # 中心热销数据（公开只读，供盒子统一拉取）
            return self._send(200, json.dumps(_hot_local(b), ensure_ascii=False))
        if self.path == "/api/hot-data/upload":  # 采集机每日推送热销数据到中心（密钥校验）
            return self._send(200, json.dumps(hot_upload(b, self.headers), ensure_ascii=False))
        if ZHI_WEB_PASS and not self._authed_or_pass(b):
            return self._send(401, json.dumps({"error": "auth"}))
        if self.path == "/api/activate":       # 用激活码激活本盒子
            return self._send(200, json.dumps(box_activate(b), ensure_ascii=False))
        if self.path == "/api/license":        # 许可状态（设置页/激活页显示）
            return self._send(200, json.dumps(license_view(), ensure_ascii=False))
        if self.path == "/api/feishu/messages":
            return self._send(200, json.dumps(feishu_messages(b.get("since", 0)), ensure_ascii=False))
        if self.path == "/api/collect-job/create":
            return self._send(200, json.dumps(collect_job_create(b.get("opts") or b), ensure_ascii=False))
        if self.path == "/api/collect-job/poll":
            return self._send(200, json.dumps(collect_job_poll(), ensure_ascii=False))
        if self.path == "/api/collect-job/done":
            return self._send(200, json.dumps(collect_job_done(b.get("id"), b.get("result")), ensure_ascii=False))
        if self.path == "/api/feishu/config":
            return self._send(200, json.dumps(save_creds({
                "FEISHU_APP_ID": b.get("app_id", ""), "FEISHU_APP_SECRET": b.get("app_secret", ""),
                "FEISHU_VERIFY_TOKEN": b.get("verify_token", "")}), ensure_ascii=False))
        if self.path == "/api/chat":
            r = chat(b.get("message", ""), b.get("session", "web"))
            _usage_bump(chats=(1 if r.get("ok") else 0), tokens=(len(r.get("reply", "")) // 3 if r.get("ok") else 0))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/chat/vision":
            r = chat_vision(b)
            _usage_bump(chats=(1 if r.get("ok") else 0))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/agent/act":
            r = agent_act(b.get("message", ""), b.get("history", []))
            _usage_bump(chats=(1 if r.get("ok") else 0))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/plan":
            return self._send(200, json.dumps(agent_plan(b), ensure_ascii=False))
        if self.path == "/api/agent/task":
            return self._send(200, json.dumps(agent_task_update(b), ensure_ascii=False))
        if self.path == "/api/img/main":
            r = studio.main_image(b, _creds_raw(), MEDIA_DIR); _usage_bump(images=_img_count(r))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/img/tryon":
            r = studio.tryon(b, _creds_raw(), MEDIA_DIR); _usage_bump(images=_img_count(r))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/img/tryon-batch":
            r = studio.tryon_batch(b, _creds_raw(), MEDIA_DIR)
            _usage_bump(images=sum(len(it.get("images", [])) for it in (r.get("items") or [])))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/img/tryon-export":
            return self._send(200, json.dumps(studio.export_zip(b, MEDIA_DIR), ensure_ascii=False))
        if self.path == "/api/img/editor":
            r = studio.editor(b, _creds_raw(), MEDIA_DIR); _usage_bump(images=_img_count(r))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/img/detail":
            r = studio.detail(b, _creds_raw(), MEDIA_DIR); _usage_bump(analyses=(1 if r.get("ok") else 0))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/model":
            r = set_relay(b.get("base_url", ""), b.get("api_key", ""),
                          b.get("model", ""), b.get("compat", "openai"))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/creds":
            return self._send(200, json.dumps(save_creds(b), ensure_ascii=False))
        if self.path == "/api/select":
            r = select(b.get("mode", "box"), b.get("urls", []),
                       b.get("limit", 10), b.get("threshold", 70),
                       bool(b.get("only_success", True)))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/demo-save":
            return self._send(200, json.dumps(demo_save(b.get("url", "")), ensure_ascii=False))
        if self.path == "/api/translate/read":
            return self._send(200, json.dumps(translate_read(b.get("url", "")), ensure_ascii=False))
        if self.path == "/api/translate/title":
            return self._send(200, json.dumps(studio.translate_title(b, _creds_raw()), ensure_ascii=False))
        if self.path == "/api/translate/images":
            r = studio.translate_images(b, _creds_raw(), MEDIA_DIR); _usage_bump(images=_img_count(r))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/ingest-1688":
            sc = bool(b.get("score", True))
            tr = bool(b.get("translate", False))
            lg = b.get("lang", "")
            if b.get("urls"):
                r = ingest_1688_urls(b.get("urls", []), b.get("threshold", 70),
                                     bool(b.get("save_passing", True)), score=sc, translate=tr, lang=lg,
                                     trans_images=bool(b.get("transImages", False)),
                                     list_tiktok=bool(b.get("listTiktok", False)), tk_auto=bool(b.get("tkAuto", False)),
                                     top_n=int(b.get("topN", 0) or 0), optimize=bool(b.get("optimize", False)),
                                     platform=(b.get("platform") or "tiktok"))
            else:
                r = ingest_1688(b.get("products", []), b.get("threshold", 70),
                                bool(b.get("save_passing", True)))
            _usage_bump(collects=int(r.get("saved") or 0))   # 用量统计：采集入箱数
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/hot":
            return self._send(200, json.dumps(hot_query(b), ensure_ascii=False))
        if self.path == "/api/box":
            return self._send(200, json.dumps(box_list(b.get("page", 1), b.get("limit", 20)), ensure_ascii=False))
        if self.path == "/api/box/delete":
            return self._send(200, json.dumps(box_delete(b.get("ids", [])), ensure_ascii=False))
        if self.path == "/api/box/detail":
            return self._send(200, json.dumps(box_detail(b.get("id", "")), ensure_ascii=False))
        if self.path == "/api/box/edit":
            return self._send(200, json.dumps(box_edit(b.get("id", ""), b.get("changes", {})), ensure_ascii=False))
        if self.path == "/api/box/upload-img":
            return self._send(200, json.dumps(box_upload_img(b.get("b64", "")), ensure_ascii=False))
        if self.path == "/api/templates":
            return self._send(200, json.dumps(templates_save(b.get("templates", b)), ensure_ascii=False))
        if self.path == "/api/tk/list":
            return self._send(200, json.dumps(tk_list_items(b.get("ids", []), b.get("shopId", ""),
                                                            b.get("site", ""), bool(b.get("auto", False))), ensure_ascii=False))
        if self.path == "/api/ozon/list":
            return self._send(200, json.dumps(ozon_list_items(b.get("ids", []), bool(b.get("auto", False))), ensure_ascii=False))
        if self.path == "/api/analyze":
            r = analyze(b.get("keyword", ""), b.get("type", "feasibility"))
            _usage_bump(analyses=(1 if r.get("ok") else 0))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/image":
            r = gen_image(b); _usage_bump(images=_img_count(r))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if self.path == "/api/extract-keywords":
            return self._send(200, json.dumps(extract_keywords(b.get("titles", [])), ensure_ascii=False))
        if self.path == "/api/asset/delete":
            return self._send(200, json.dumps(asset_delete(b.get("type", ""), b.get("name", "")), ensure_ascii=False))
        if self.path == "/api/asset/view":
            return self._send(200, json.dumps(asset_read(b.get("type", ""), b.get("name", "")), ensure_ascii=False))
        if self.path == "/api/video":
            r = gen_video(b); _usage_bump(videos=len(r.get("videos", [])) if r.get("ok") else 0)
            return self._send(200, json.dumps(r, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>飞猴 · 智能体工作台</title>
<style>
:root{--mint:#10b981;--minth:#059669;--soft:#ecfdf5;--bg:#ffffff;--panel:#f6f7f9;
--bd:#ececef;--tx:#16181d;--mut:#6b7280;--lite:#9096a1;}
*{box-sizing:border-box}
body{margin:0;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
color:var(--tx);background:var(--bg);height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--bd)}
.logo{width:34px;height:34px;border-radius:10px;background:var(--mint);color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px}
.brand b{font-size:17px}.brand div{font-size:12px;color:var(--mut)}
.tabs{margin-left:18px;display:flex;gap:6px}
.tab{padding:7px 16px;border-radius:9px;cursor:pointer;font-size:14px;color:var(--mut)}
.tab.on{background:var(--soft);color:var(--minth);font-weight:700}
.status{margin-left:auto;display:flex;gap:14px;font-size:12px;color:var(--mut);align-items:center}
.pill{padding:4px 10px;border-radius:20px;background:var(--panel);border:1px solid var(--bd)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;background:#9ca3af}
.dot.ok{background:var(--mint)}.dot.bad{background:#ef4444}
main{flex:1;overflow:hidden;display:flex}
#chat{flex:1;display:flex;min-width:0}
#chatmain{flex:1;display:flex;flex-direction:column;min-width:0}
#chatside{width:236px;flex:none;border-right:1px solid var(--bd);display:flex;flex-direction:column;background:#fafafa}
#chatside .new{margin:10px;padding:9px;border:1px solid var(--bd);border-radius:10px;background:#fff;cursor:pointer;font-weight:700;font-size:13px;text-align:center;color:var(--tx)}
#chatside .new:hover{border-color:var(--mint);color:var(--minth)}
#chatside .shead{font-size:11px;color:var(--mut);padding:2px 14px 4px;font-weight:700}
#chatlist{flex:1;overflow-y:auto;padding:0 8px 10px}
.sitem{display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--tx)}
.sitem:hover{background:#eef2f7}
.sitem.on{background:var(--soft);color:var(--minth);font-weight:700}
.sitem .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sitem .x{color:#c0c4cc;font-weight:700;opacity:0;flex:none;padding:0 2px}
.sitem:hover .x{opacity:1}.sitem .x:hover{color:#dc2626}
@media(max-width:760px){#chatside{width:148px}}
.msgs{flex:1;overflow-y:auto;padding:24px 0}
.wrap{max-width:820px;margin:0 auto;padding:0 20px}
.row{display:flex;margin:14px 0}.row.u{justify-content:flex-end}
.bub{max-width:72%;padding:11px 15px;border-radius:14px;font-size:14.5px;line-height:1.7;overflow-wrap:break-word}
.row.u .bub{background:var(--mint);color:#fff;border-bottom-right-radius:4px;width:fit-content;white-space:pre-wrap}
.row.a .bub{background:var(--panel);border:1px solid var(--bd);border-bottom-left-radius:4px;max-width:90%}
.row.e .bub{background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;white-space:pre-wrap}
.bub h3{font-size:15px;margin:11px 0 6px}.bub h4{font-size:13px;color:var(--mut);margin:9px 0 4px}
.bub p{margin:6px 0}.bub ul{margin:6px 0;padding-left:18px}.bub li{margin:2px 0}
.bub hr{border:none;border-top:1px dashed var(--bd);margin:9px 0}.bub b{font-weight:700}
.bub table{border-collapse:collapse;width:100%;margin:7px 0;font-size:12.5px}
.bub th,.bub td{border:1px solid var(--bd);padding:5px 8px;text-align:left;vertical-align:top}
.bub th{background:var(--soft);color:var(--minth);font-weight:700;white-space:nowrap}
.bub tr:nth-child(even) td{background:#fafbfc}
.who{font-size:11px;color:var(--lite);margin:0 6px 3px}
.chips{max-width:820px;margin:0 auto;padding:6px 20px 0;display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:7px 13px;border:1px solid var(--bd);border-radius:18px;font-size:13px;
cursor:pointer;color:var(--tx);background:#fff}
.chip:hover{border-color:var(--mint);color:var(--minth);background:var(--soft)}
.composer{border-top:1px solid var(--bd);padding:14px 0}
.cbar{max-width:820px;margin:0 auto;padding:0 20px;display:flex;gap:10px;align-items:flex-end}
.iconbtn{background:#fff;border:1px solid var(--bd);border-radius:12px;height:46px;width:46px;font-size:19px;cursor:pointer;flex:none}
.iconbtn:hover{border-color:var(--mint)}
.iconbtn.on{background:#fee2e2;border-color:#ef4444;animation:micp 1.1s infinite}
@keyframes micp{0%,100%{opacity:1}50%{opacity:.5}}
.attrow{max-width:820px;margin:0 auto 8px;padding:0 20px;display:flex;flex-wrap:wrap;gap:8px}
.attrow:empty{display:none}
.attchip{display:flex;align-items:center;gap:6px;background:var(--soft);border:1px solid var(--mint);border-radius:9px;padding:4px 9px;font-size:12px;max-width:200px}
.attchip img{width:30px;height:30px;object-fit:cover;border-radius:5px;flex:none}
.attchip span.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.attchip .rm{cursor:pointer;color:#dc2626;font-weight:700;flex:none}
.acttag{display:inline-block;background:var(--mint);color:#fff;border-radius:6px;padding:1px 7px;font-size:11px;font-weight:700;vertical-align:middle}
.acttag.bad{background:#dc2626}
textarea{flex:1;resize:none;border:1px solid var(--bd);border-radius:12px;padding:12px 14px;
font:inherit;font-size:14.5px;outline:none;max-height:140px}
textarea:focus{border-color:var(--mint)}
/* 生图/视频配置区的补充描述框：填满整列宽度，不再窄成一条 */
.cfg textarea{width:100%;box-sizing:border-box;flex:none;min-height:64px;max-height:180px;border-radius:10px;padding:10px 12px;font-size:14px}
.send{background:var(--mint);color:#fff;border:none;border-radius:12px;padding:0 22px;
font-weight:700;cursor:pointer;font-size:14px}
.send:hover{background:var(--minth)}.send:disabled{opacity:.5;cursor:default}
#settings{flex:1;overflow-y:auto;display:none}
.card{max-width:720px;margin:22px auto;background:#fff;border:1px solid var(--bd);
border-radius:16px;padding:22px}
.card h3{margin:0 0 4px;font-size:16px}.card p.h{margin:0 0 16px;color:var(--mut);font-size:13px}
label{display:block;font-size:13px;font-weight:700;margin:12px 0 5px}
input,select{width:100%;border:1px solid var(--bd);border-radius:10px;padding:10px 12px;
font:inherit;font-size:14px;outline:none}input:focus,select:focus{border-color:var(--mint)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.btn{margin-top:16px;background:var(--mint);color:#fff;border:none;border-radius:10px;
padding:10px 20px;font-weight:700;cursor:pointer;font-size:14px}.btn:hover{background:var(--minth)}
.note{margin-top:12px;font-size:12.5px;color:var(--mut);background:var(--soft);
border:1px solid #d1fae5;border-radius:10px;padding:10px 12px}
.ok{color:var(--minth)}.bad{color:#b91c1c}
.kv{font-size:13px;color:var(--mut);margin:6px 0}.kv b{color:var(--tx)}
.typing{display:inline-block;color:var(--lite);font-size:13px}
.page{flex:1;overflow-y:auto;display:none;padding:18px 22px}
.fbar{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;background:#fff;border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin-bottom:14px}
.fbar .fi{display:flex;flex-direction:column;gap:3px}.fbar .fi label{margin:0;font-size:11px;color:var(--mut)}
.fbar input,.fbar select{width:auto;min-width:92px;padding:7px 9px;font-size:13px}
.tablewrap{background:#fff;border:1px solid var(--bd);border-radius:12px;overflow:auto;max-height:calc(100vh - 230px)}
.dtable{width:100%;border-collapse:collapse;font-size:12.5px}
.dtable th,.dtable td{border-bottom:1px solid var(--bd);padding:7px 9px;text-align:left;vertical-align:middle}
.dtable th{color:var(--mut);font-size:11px;font-weight:700;position:sticky;top:0;background:#f7f8fa;white-space:nowrap}
.dtable img{width:42px;height:42px;object-fit:cover;border-radius:6px;border:1px solid var(--bd)}
.dtable a{color:var(--minth);text-decoration:none}
.pillx{font-size:11px;border-radius:10px;padding:2px 8px}.pillx.ok{background:var(--soft);color:var(--minth)}.pillx.no{background:#f1f5f9;color:#64748b}
.chip2{display:inline-block;padding:7px 14px;border:1px solid var(--bd);border-radius:18px;font-size:13px;cursor:pointer;background:#fff;margin:0 6px 8px 0}
.chip2:hover,.chip2.on{border-color:var(--mint);color:var(--minth);background:var(--soft)}
.rep{background:#fff;border:1px solid var(--bd);border-radius:14px;padding:18px 20px;font-size:13.5px;line-height:1.7;margin-top:12px}
.rep h3{font-size:15px;margin:11px 0 6px}.rep h4{font-size:13px;color:var(--mut);margin:9px 0 4px}.rep p{margin:6px 0}
.rep table{border-collapse:collapse;width:100%;margin:7px 0;font-size:12.5px}.rep th,.rep td{border:1px solid var(--bd);padding:5px 8px;text-align:left}
.rep th{background:var(--soft);color:var(--minth)}.rep ul{padding-left:18px}.rep hr{border:none;border-top:1px dashed var(--bd);margin:9px 0}.rep b{font-weight:700}
.hint{font-size:12px;color:var(--mut);margin:6px 0 10px}
.amedia{width:120px;height:120px;object-fit:cover;border-radius:8px;border:1px solid var(--bd);margin:6px;cursor:pointer}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99;align-items:center;justify-content:center}
#modal_in{background:#fff;border-radius:14px;max-width:92vw;max-height:92vh;overflow:auto;box-shadow:0 12px 44px rgba(0,0,0,.32)}
#modal_in img,#modal_in video{max-width:88vw;max-height:82vh;display:block}
#modal_in .mc{padding:18px 22px;max-width:760px;font-size:13.5px;line-height:1.7}
.kbtag{display:inline-block;background:var(--soft);color:var(--minth);border:1px solid #d1fae5;border-radius:8px;padding:3px 9px;margin:3px 5px 0 0;font-size:12px}
.actbtn{font-size:11px;border:1px solid var(--bd);background:#fff;border-radius:7px;padding:3px 8px;cursor:pointer;margin-right:4px;white-space:nowrap}
.actbtn:hover{border-color:var(--mint);color:var(--minth)}
.dtable td.thumb img{cursor:zoom-in}
.delx{color:#dc2626;cursor:pointer;font-size:12px;margin-left:8px}
#modal_in{position:relative}
#modal_x{position:absolute;top:6px;right:12px;font-size:26px;color:#9aa0ab;cursor:pointer;line-height:1;z-index:3}
#modal_x:hover{color:#16181d}
#toast{display:none;position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#16181d;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;z-index:200;box-shadow:0 6px 24px rgba(0,0,0,.3)}
.dtable input[type=checkbox]{width:16px;height:16px;accent-color:var(--mint);cursor:pointer;vertical-align:middle}
.dtable tbody tr:hover td{background:#f3faf7}
.dtable th:first-child,.dtable td:nth-child(1){text-align:center}
.itype-wrap input,.kwchk{accent-color:var(--mint)}
.atab{user-select:none}
.acard{background:#fff;border:1px solid var(--bd);border-radius:12px;overflow:hidden;width:180px;transition:box-shadow .15s}
.acard:hover{box-shadow:0 4px 16px rgba(0,0,0,.1)}
.acard .im{width:180px;height:180px;object-fit:cover;display:block;cursor:zoom-in;background:#f3f4f6}
.acard .ft{display:flex;align-items:center;padding:7px 9px;font-size:11px;color:var(--mut);gap:6px}
.acard .ft span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.istudio{display:flex;gap:16px;align-items:flex-start}
.istudio .cfg{width:430px;flex:none}
.istudio .work{flex:1;min-height:420px}
.istudio-grid{display:flex;flex-wrap:wrap;gap:12px}
.astab{display:inline-flex;align-items:center;gap:5px;padding:7px 15px;border:1px solid var(--bd);border-radius:18px;font-size:13px;cursor:pointer;background:#fff;margin:0 8px 8px 0;color:var(--mut)}
.astab:hover{border-color:var(--mint)}
.astab.on{background:var(--mint);border-color:var(--mint);color:#fff}
.astab b{font-weight:700}
.alist{display:flex;flex-direction:column;gap:8px}
.arow{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;background:#fff}
.arow .an{flex:1;cursor:pointer;font-size:13px}
.arow .an:hover{color:var(--minth)}
.iempty{color:var(--mut);font-size:13px;padding:46px 12px;text-align:center;width:100%;border:1.5px dashed var(--bd);border-radius:12px;background:#fafbfc;box-sizing:border-box}
.acard .ft a.actbtn{text-decoration:none;color:var(--mut)}
@media(max-width:900px){.istudio{flex-direction:column}.istudio .cfg{width:100%}}
.planwrap{margin:8px 0 2px}
.planc{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:10px 12px;margin-top:8px}
.planh{font-size:12.5px;font-weight:800;color:var(--minth);margin-bottom:8px}
.plant{display:flex;align-items:center;gap:10px;padding:8px;border-radius:9px}
.plant:hover{background:var(--soft)}
.plant .pnum{width:22px;height:22px;flex:none;border-radius:50%;background:var(--mint);color:#fff;font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center}
.plant .pmain{flex:1;min-width:0}
.plant .ptitle{font-size:13px;font-weight:700}
.plant .preason{font-size:11.5px;color:var(--mut);margin-top:2px}
.pprio{font-size:10px;padding:1px 6px;border-radius:6px;vertical-align:middle;font-weight:700}
.pprio.P0{background:#fee2e2;color:#dc2626}.pprio.P1{background:#fef3c7;color:#b45309}.pprio.P2{background:#e0e7ff;color:#4338ca}
.planq{font-size:12.5px;padding:7px 10px;border:1px dashed var(--bd);border-radius:8px;margin-bottom:6px;cursor:pointer;color:#374151}
.planq:hover{border-color:var(--mint);background:var(--soft);color:var(--minth)}
.plantip{font-size:11.5px;color:var(--mut);margin-top:8px;line-height:1.6}
.substab{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;border-bottom:1px solid var(--bd);padding-bottom:10px}
.subt{padding:8px 16px;border-radius:10px;font-size:13.5px;font-weight:600;cursor:pointer;color:var(--mut);background:#fff;border:1px solid var(--bd)}
.subt:hover{border-color:var(--mint)}
.subt.on{background:var(--mint);color:#fff;border-color:var(--mint)}
.upz{border:1.5px dashed var(--bd);border-radius:12px;padding:16px;text-align:center;cursor:pointer;background:#fafbfc;transition:.15s}
.upz:hover{border-color:var(--mint);background:var(--soft)}
.upz img,.upz video{max-height:150px;max-width:100%;border-radius:8px;display:block;margin:0 auto}
.upz .ph{color:var(--mut);font-size:12.5px}
.becard{border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin:12px 0;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.03)}
.becard textarea,.becard input{border-radius:8px}
.behd{font-weight:700;font-size:13.5px;color:var(--minth);margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--soft);display:flex;align-items:center;gap:8px}
#be_attrs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px 14px}
.attrcell{display:flex;align-items:center;background:#f9fafb;border:1px solid var(--bd);border-radius:9px;overflow:hidden}
.attrcell input{border:none!important;background:transparent;padding:8px 10px;font-size:12.5px;min-width:0;border-radius:0}
.attrcell input:focus{outline:none;background:#fff}
.attrcell .ak{flex:0 0 38%;font-weight:600;color:#374151;border-right:1px solid var(--bd)!important}
.attrcell .av{flex:1}
.attrcell .x2{border:none;color:#cbd5e1;width:28px;height:30px;line-height:30px;border-radius:0;flex:none}
.attrcell .x2:hover{color:#dc2626;background:#fef2f2}
.skutb{border-collapse:collapse;width:100%;font-size:12px}
.skutb th,.skutb td{border:1px solid var(--bd);padding:5px 7px;text-align:center;white-space:nowrap}
.skutb th{background:var(--soft);color:var(--minth);position:sticky;top:0;font-weight:700}
.skutb tr:nth-child(even) td{background:#fafafa}
.skutb td:first-child,.skutb td:nth-child(2){text-align:left;max-width:200px;overflow:hidden;text-overflow:ellipsis}
.skutb td input{padding:4px 6px;font-size:12px;border-radius:6px;border:1px solid var(--bd)}
.x2{width:24px;height:24px;line-height:22px;text-align:center;border:1px solid #fecaca;color:#dc2626;border-radius:6px;cursor:pointer;flex:none}
.x2:hover{background:#fef2f2}
.thumbrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.thumbrow .tb{position:relative;width:72px;height:72px;border-radius:8px;overflow:hidden;border:1px solid var(--bd)}
.thumbrow .tb img{width:100%;height:100%;object-fit:cover}
.thumbrow .tb .x{position:absolute;top:1px;right:1px;background:rgba(0,0,0,.55);color:#fff;width:17px;height:17px;border-radius:50%;font-size:12px;line-height:17px;text-align:center;cursor:pointer}
.thumbrow .add{display:flex;align-items:center;justify-content:center;width:72px;height:72px;border:1.5px dashed var(--bd);border-radius:8px;cursor:pointer;color:var(--mut);font-size:22px}
.thumbrow .add:hover{border-color:var(--mint);color:var(--minth)}
.chk2{display:inline-flex;align-items:center;gap:5px;padding:6px 11px;border:1px solid var(--bd);border-radius:8px;cursor:pointer;font-size:12px;margin:0 6px 6px 0}
.chk2.on{border-color:var(--mint);background:var(--soft);color:var(--minth)}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin-top:4px}
.seg span{padding:6px 13px;cursor:pointer;font-size:12.5px;background:#fff;border-right:1px solid var(--bd)}
.seg span:last-child{border-right:none}.seg span.on{background:var(--mint);color:#fff}
.stylebtn{display:inline-flex;align-items:center;gap:5px;padding:7px 12px;border:1px solid var(--bd);border-radius:9px;font-size:12.5px;cursor:pointer;background:#fff;margin:0 6px 6px 0}
.stylebtn.on{border-color:var(--mint);background:var(--mint);color:#fff}
</style></head><body>
<header>
  <div class="logo" style="background:transparent;padding:0;overflow:hidden"><img src="/logo" alt="飞猴" style="width:100%;height:100%;object-fit:cover;display:block"></div>
  <div class="brand"><b>飞猴</b><div>跨境电商智能体工作台</div></div>
  <div class="tabs">
    <div class="tab on" data-v="chat" onclick="tab('chat')">对话</div>
    <div class="tab" data-v="hot" onclick="tab('hot')">热销榜</div>
    <div class="tab" data-v="box" onclick="tab('box')">采集箱</div>
    <div class="tab" data-v="analysis" onclick="tab('analysis')">智能分析</div>
    <div class="tab" data-v="image" onclick="tab('image')">AI生图</div>
    <div class="tab" data-v="video" onclick="tab('video')">AI视频</div>
    <div class="tab" data-v="translate" onclick="tab('translate')">一键翻译</div>
    <div class="tab" data-v="assets" onclick="tab('assets')">资产库</div>
    <div class="tab" data-v="settings" onclick="tab('settings')">设置</div>
  </div>
  <div class="status">
    <span class="pill"><span id="gdot" class="dot"></span>网关 <b id="gw">…</b></span>
    <span class="pill">模型 <b id="md">…</b></span>
  </div>
</header>
<main>
  <section id="chat">
    <aside id="chatside">
      <div class="new" onclick="newChat()">➕ 新对话</div>
      <div class="shead">历史对话</div>
      <div id="chatlist"></div>
    </aside>
    <div id="chatmain">
    <div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--bd);flex:none">
      <span class="hint" id="chat_title" style="margin:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">当前对话</span>
    </div>
    <div class="msgs" id="msgs"><div class="wrap">
      <div class="row a"><div><div class="who">飞猴</div>
      <div class="bub">你好，我是「飞猴」跨境电商智能体 👋 可以让我做选品、竞品分析、Listing、定价、客服话术等。试试下面的快捷指令，或直接提问。</div></div></div>
    </div></div>
    <div class="chips" id="chips"></div>
    <div style="padding:0 4px 6px"><span class="actbtn" onclick="openTaskCenter()">📋 任务中心</span><span class="hint" style="margin:0 0 0 8px">规划出的任务会自动入库，可在这里追踪执行</span></div>
    <div class="composer">
      <div id="attach_row" class="attrow"></div>
      <div class="cbar">
        <button class="iconbtn" id="micbtn" title="语音输入（再点停止）" onclick="toggleMic()">🎤</button>
        <button class="iconbtn" title="上传图片/文件" onclick="document.getElementById('chatfile').click()">📎</button>
        <input type="file" id="chatfile" accept="image/*,.pdf,.txt,.csv,.md,.json,.docx,.xlsx" multiple style="display:none" onchange="chatAttach(event)">
        <textarea id="inp" rows="1" placeholder="输入需求；或点 🎤 说话、📎 传图/文件。例：分析这个品类差评痛点…"></textarea>
        <button class="send" id="snd" onclick="send()">发送</button>
      </div>
    </div>
    </div>
  </section>
  <section id="hot" class="page">
    <div class="fbar">
      <div class="fi"><label>站点</label><select id="h_site"><option>美国</option><option>日本</option><option>英国</option><option>越南</option><option>泰国</option><option>菲律宾</option><option>马来西亚</option><option>印度尼西亚</option><option>新加坡</option><option>德国</option><option>法国</option><option>意大利</option><option>西班牙</option><option>墨西哥</option><option>巴西</option><option>沙特</option></select></div>
      <div class="fi"><label>榜单</label><select id="h_period"><option>天榜</option><option>周榜</option><option>月榜</option></select></div>
      <div class="fi"><label>排序</label><select id="h_sort"><option value="rank">平台排名</option><option value="daily_sales">日销量</option><option value="sold_count">总销量</option><option value="est_gmv">预估GMV</option><option value="creator_count">达人数</option><option value="commission_rate">佣金率</option><option value="price_cny">价格</option></select></div>
      <div class="fi"><label>类目含</label><input id="h_cat" placeholder="可选"></div>
      <div class="fi"><label>关键词</label><input id="h_kw" placeholder="可选"></div>
      <div class="fi"><label>数量</label><input id="h_limit" type="number" value="30" style="min-width:68px"></div>
      <button class="btn" style="margin:0" onclick="hotLoad()">查询</button>
    </div>
    <div class="hint" id="h_hint">TikTok 各站点热销榜，每日更新。选条件后点「查询」。</div>
    <div style="display:flex;align-items:center;gap:10px;margin:0 0 10px;flex-wrap:wrap;padding:8px 12px;background:var(--soft);border:1px solid #d1fae5;border-radius:10px">
      <span style="font-size:12px;color:var(--minth);font-weight:700">批量操作</span>
      <button class="btn" style="margin:0;font-size:12px;padding:6px 14px" onclick="hotKwBatch()">🔑 提取选中关键词</button>
      <span class="hint" id="h_kwmsg" style="margin:0"></span>
    </div>
    <div class="tablewrap"><table class="dtable" id="h_tbl"></table></div>
  </section>
  <section id="box" class="page">
    <div class="fbar">
      <button class="btn" style="margin:0" onclick="boxLoad(1)">刷新</button>
      <button class="btn" style="margin:0" onclick="openTkList()">📤 上架 TikTok</button>
      <button class="btn" style="margin:0;background:#005bff" onclick="openOzonList()">📤 上架 Ozon</button>
      <button class="btn" style="margin:0;background:#fff;color:#dc2626;border:1px solid #fecaca" onclick="boxDel()">删除选中</button>
      <div class="fi"><label>每页</label><input id="b_limit" type="number" value="20" style="min-width:68px"></div>
      <span class="hint" id="b_hint" style="margin:0 0 0 8px"></span>
    </div>
    <div class="tablewrap"><table class="dtable" id="b_tbl"></table></div>
  </section>
  <section id="analysis" class="page">
    <div class="card" style="max-width:none;margin:0 0 14px">
      <h3>智能分析</h3><p class="h">选分析类型 + 输入产品/品类，由「飞猴」智能体出结构化分析报告，自动存入资产库。</p>
      <div id="a_chips"></div>
      <label>产品 / 关键词 / 品类</label>
      <input id="a_kw" placeholder="如 瑜伽裤 / 保温杯 / 宠物饮水机">
      <button class="btn" id="a_btn" onclick="doAnalyze()">开始分析</button>
      <div id="a_msg" class="hint"></div>
    </div>
    <div id="a_desc" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-bottom:14px"></div>
    <div class="card" style="max-width:none;margin:0 0 14px"><h3>最近分析报告</h3><div id="a_recent" class="hint">—</div></div>
    <div class="rep" id="a_rep" style="display:none"></div>
  </section>
  <section id="image" class="page">
    <div class="substab">
      <span class="subt on" id="igt_main" onclick="igTab('main')">🎨 全品类主图</span>
      <span class="subt" id="igt_tryon" onclick="igTab('tryon')">👗 AI模特换装</span>
      <span class="subt" id="igt_editor" onclick="igTab('editor')">✂️ 抠图改图</span>
      <span class="subt" id="igt_detail" onclick="igTab('detail')">📄 详情页生成</span>
    </div>
    <!-- 全品类主图 -->
    <div id="ig_main" class="igsub"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
      <h3>🎨 全品类主图</h3><p class="h">上传商品图，基于原商品生成跨境高转化主图（图生图，保留商品换场景）。</p>
      <label>上传商品图片（可多张）</label>
      <div class="thumbrow" id="mn_thumbs"></div>
      <input type="file" id="mn_file" accept="image/*" multiple style="display:none" onchange="stuUpMulti(event,'mn')">
      <label>主图风格</label><div id="mn_styles"></div>
      <label>创意强度</label><div id="mn_creatives"></div>
      <label>补充描述（可选）</label><textarea id="mn_desc" rows="2" placeholder="如：放在原木桌面、暖光、北欧风背景，突出质感细节"></textarea>
      <label>生成张数</label><select id="mn_num"><option>1</option><option>2</option><option>3</option><option selected>4</option></select>
      <button class="btn" id="mn_btn" onclick="doMain('batch')" style="width:100%;margin-top:10px">✨ 生成主图</button>
      <button class="btn" id="mn_confirm" onclick="mnConfirm()" style="width:100%;display:none;background:var(--mint)">✅ 确认使用这张主图</button>
      <div id="mn_batchbox" style="display:none"><select id="mn_count" style="display:none"><option selected>4</option></select><button class="btn" id="mn_batch" onclick="doMain('batch')" style="display:none">批量</button></div>
      <div id="mn_msg" class="hint"></div>
    </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🖼 工作台</h3><span class="hint" id="mn_cnt"></span><span class="actbtn" onclick="tab('assets')">资产库 →</span></div>
      <div id="mn_studio"><div class="iempty">上传商品图 → 选风格 → 生成首图，结果在这里展示。</div></div>
    </div></div></div></div>
    <!-- AI模特换装 -->
    <div id="ig_tryon" class="igsub" style="display:none">
      <div style="margin-bottom:10px"><span class="seg"><span id="tom_single" class="on" onclick="toMode('single')">单图生成</span><span id="tom_batch" onclick="toMode('batch')">批量生成</span></span></div>
      <div id="to_single"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
        <h3>👗 AI模特换装 · 单图</h3><p class="h">上传白底商品图与模特参考图，先生成 1 张确认图，确认后再生成 4 张展示图。保持模特一致、服装细节不丢。</p>
        <div class="grid2">
          <div><label>白底商品图</label><div class="upz" id="to_productz" onclick="document.getElementById('to_product').click()"><div class="ph">点击上传商品图</div></div><input type="file" id="to_product" accept="image/*" style="display:none" onchange="stuUp(event,'to_product','to_productz')"></div>
          <div><label>模特参考图</label><div class="upz" id="to_modelz" onclick="document.getElementById('to_model').click()"><div class="ph">点击上传模特图</div></div><input type="file" id="to_model" accept="image/*" style="display:none" onchange="stuUp(event,'to_model','to_modelz')"></div>
        </div>
        <label>配饰增强（可选，多选）</label><div id="to_acc"></div>
        <label>补充描述（可选）</label><textarea id="to_desc" rows="2" placeholder="如：户外阳光场景、街拍风格、暖色调背景"></textarea>
        <label>生成张数（确认后输出）</label><select id="to_num"><option>1</option><option>2</option><option>3</option><option selected>4</option></select>
        <button class="btn" id="to_btn" onclick="doTryon('preview')" style="width:100%;margin-top:8px">🧍 生成确认图</button>
        <button class="btn" id="to_confirm" onclick="doTryon('confirm')" style="width:100%;display:none;background:var(--mint)">✅ 确认并生成 4 张展示图</button>
        <div id="to_msg" class="hint"></div>
      </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🖼 换装工作台</h3><span class="hint" id="to_cnt"></span></div>
        <div id="to_studio"><div class="iempty">上传商品图 + 模特图 → 生成确认图 → 确认 → 4 张展示图。</div></div>
      </div></div></div></div>
      <div id="to_batch" style="display:none">
        <div class="card" style="max-width:none;margin:0 0 12px">
          <h3>👗 批量模特换装</h3><p class="h">多商品 × 多模特批量生成确认图 + 英文标题（单次≤12项），逐个确认生成 4 张展示图，可打包导出 ZIP。</p>
          <div class="grid2">
            <div><label>商品图（可多张）</label><div class="thumbrow" id="tob_p_thumbs"></div><input type="file" id="tob_p_file" accept="image/*" multiple style="display:none" onchange="stuUpMulti(event,'tob_p')"></div>
            <div><label>模特图（可多张）</label><div class="thumbrow" id="tob_m_thumbs"></div><input type="file" id="tob_m_file" accept="image/*" multiple style="display:none" onchange="stuUpMulti(event,'tob_m')"></div>
          </div>
          <div class="grid2">
            <div><label>模特性别</label><select id="tob_gender"><option value="female">女模</option><option value="male">男模</option></select></div>
            <div><label>匹配模式</label><select id="tob_match"><option value="random_model_per_product">每商品轮询模特</option><option value="product_model_cross">商品 × 模特 全组合</option></select></div>
          </div>
          <button class="btn" id="tob_btn" onclick="doTryonBatch()" style="width:100%">批量生成确认图 + 标题</button>
          <div id="tob_msg" class="hint"></div>
        </div>
        <div class="card" style="max-width:none;margin:0">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap"><h3 style="margin:0;flex:1">批量结果</h3><span class="actbtn" onclick="doTryonConfirmAll()">✅ 确认全部 → 4图</span><span class="actbtn" onclick="doTryonExport()">⬇ 导出 ZIP</span></div>
          <div id="tob_results"><div class="iempty">上传商品图 + 模特图后点击「批量生成确认图」。</div></div>
        </div>
      </div>
    </div>
    <!-- 抠图改图 -->
    <div id="ig_editor" class="igsub" style="display:none"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
      <h3>✂️ 抠图改图</h3><p class="h">第一步：上传参考图抠出透明图案；第二步：把图案贴到你的商品图，生成白底效果图。</p>
      <label>① 参考图（竞品图/带图案的图）</label><div class="upz" id="ed_refz" onclick="document.getElementById('ed_ref').click()"><div class="ph">点击上传参考图</div></div><input type="file" id="ed_ref" accept="image/*" style="display:none" onchange="stuUp(event,'ed_ref','ed_refz')">
      <button class="btn" id="ed_extract" onclick="doEditor('extract')" style="width:100%;margin-top:8px">✂️ 抠取图案</button>
      <label style="margin-top:14px">② 透明图案（抠图结果，或自己上传）</label><div class="upz" id="ed_patternz" onclick="document.getElementById('ed_pattern').click()"><div class="ph">抠图后自动填入，或点击上传</div></div><input type="file" id="ed_pattern" accept="image/*" style="display:none" onchange="stuUp(event,'ed_pattern','ed_patternz')">
      <label>目标商品图</label><div class="upz" id="ed_productz" onclick="document.getElementById('ed_product').click()"><div class="ph">点击上传目标商品图</div></div><input type="file" id="ed_product" accept="image/*" style="display:none" onchange="stuUp(event,'ed_product','ed_productz')">
      <label>补充描述（可选）</label><textarea id="ed_desc" rows="2" placeholder="如：白底、图案居中、增加自然阴影"></textarea>
      <label>生成张数</label><select id="ed_num"><option selected>1</option><option>2</option><option>3</option><option>4</option></select>
      <button class="btn" id="ed_apply" onclick="doEditor('apply')" style="width:100%;margin-top:8px;background:var(--mint)">🪄 生成贴图效果</button>
      <div id="ed_msg" class="hint"></div>
    </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🖼 抠改工作台</h3><span class="hint" id="ed_cnt"></span></div>
      <div id="ed_studio"><div class="iempty">先抠图，再贴到商品图。识别到的商品类型会显示在这里。</div></div>
    </div></div></div></div>
    <!-- 详情页生成 -->
    <div id="ig_detail" class="igsub" style="display:none"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
      <h3>📄 详情页生成</h3><p class="h">上传商品图，AI 自动识别品类并生成英文详情长图。</p>
      <label>商品图片</label><div class="upz" id="dt_imgz" onclick="document.getElementById('dt_img').click()"><div class="ph">点击上传商品图</div></div><input type="file" id="dt_img" accept="image/*" style="display:none" onchange="stuUp(event,'dt_img','dt_imgz')">
      <label>补充说明（可选）</label><textarea id="dt_notes" rows="3" placeholder="如：450ml 陶瓷马克杯，竹木盖，TikTok 美区，主打保温"></textarea>
      <button class="btn" id="dt_btn" onclick="doDetail()" style="width:100%;margin-top:8px">📄 生成详情页</button>
      <div id="dt_msg" class="hint"></div>
    </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">📄 详情预览</h3><span class="hint" id="dt_cat"></span></div>
      <div id="dt_studio"><div class="iempty">上传商品图后点击「生成详情页」，英文详情长图在这里展示。</div></div>
    </div></div></div></div>
  </section>
  <section id="video" class="page">
    <div class="substab">
      <span class="subt on" id="vdt_t2v" onclick="vdTab('t2v')">🎬 文生视频</span>
      <span class="subt" id="vdt_i2v" onclick="vdTab('i2v')">🖼 图生视频</span>
    </div>
    <!-- 文生视频 -->
    <div id="vd_t2v" class="vdsub"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
      <h3>🎬 文生视频</h3><p class="h">用文字描述生成商品展示短视频（约 1-3 分钟）。</p>
      <label>视频描述 / 脚本 *</label><textarea id="t2v_desc" rows="3" placeholder="如：白色陶瓷马克杯在原木桌上缓慢旋转，暖光，展示竹木盖细节"></textarea>
      <div class="grid2">
        <div><label>视频类型</label><select id="t2v_type"><option>商品展示</option><option>开箱</option><option>使用场景</option><option>卖点讲解</option></select></div>
        <div><label>画面比例</label><select id="t2v_ratio"><option value="16:9">16:9 横屏</option><option value="9:16">9:16 竖屏</option><option value="1:1">1:1 方形</option></select></div>
      </div>
      <label>生成时长（秒）</label><select id="t2v_dur"><option value="5">5 秒</option><option value="10">10 秒</option><option value="15">15 秒</option></select>
      <button class="btn" id="t2v_btn" onclick="doVid('t2v')" style="width:100%;margin-top:8px">🎬 生成视频</button>
      <div id="t2v_msg" class="hint"></div>
    </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🎞 工作台</h3><span class="hint" id="t2v_cnt"></span><span class="actbtn" onclick="tab('assets')">资产库 →</span></div>
      <div id="t2v_studio"><div class="iempty">填写脚本后点击「生成视频」，结果在这里播放。</div></div>
    </div></div></div></div>
    <!-- 图生视频 -->
    <div id="vd_i2v" class="vdsub" style="display:none"><div class="istudio"><div class="cfg"><div class="card" style="max-width:none;margin:0">
      <h3>🖼 图生视频</h3><p class="h">上传商品图，让它动起来（约 1-3 分钟）。</p>
      <label>商品图片 *</label><div class="upz" id="i2v_imgz" onclick="document.getElementById('i2v_img').click()"><div class="ph">点击上传商品图</div></div><input type="file" id="i2v_img" accept="image/*" style="display:none" onchange="stuUp(event,'i2v_img','i2v_imgz')">
      <label>动效 / 补充描述</label><textarea id="i2v_desc" rows="2" placeholder="如：商品在高级影棚背景中缓慢旋转，突出质感"></textarea>
      <div class="grid2">
        <div><label>视频类型</label><select id="i2v_type"><option>商品展示</option><option>使用场景</option><option>卖点讲解</option></select></div>
        <div><label>画面比例</label><select id="i2v_ratio"><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></div>
      </div>
      <label>生成时长（秒）</label><select id="i2v_dur"><option value="5">5 秒</option><option value="10">10 秒</option><option value="15">15 秒</option></select>
      <button class="btn" id="i2v_btn" onclick="doVid('i2v')" style="width:100%;margin-top:8px">🖼 生成图生视频</button>
      <div id="i2v_msg" class="hint"></div>
    </div></div><div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🎞 工作台</h3><span class="hint" id="i2v_cnt"></span><span class="actbtn" onclick="tab('assets')">资产库 →</span></div>
      <div id="i2v_studio"><div class="iempty">上传商品图后点击生成，结果在这里播放。</div></div>
    </div></div></div></div>
  </section>
  <section id="translate" class="page">
    <div class="istudio">
      <div class="cfg"><div class="card" style="max-width:none;margin:0">
        <h3>🌐 一键翻译</h3><p class="h">粘贴 1688 链接读取商品图，或直接上传图片；选目标语言，一键把<b>标题</b>和<b>图片</b>翻成该语言。图片翻译用 AI 重绘，质量视图而定。</p>
        <label>1688 商品链接</label>
        <div style="display:flex;gap:8px"><input id="tr_url" placeholder="https://detail.1688.com/offer/xxx.html" style="flex:1"><button class="btn" id="tr_read" onclick="trRead()" style="margin:0">读取</button></div>
        <label>或上传图片（可多张）</label>
        <div class="upz" onclick="document.getElementById('tr_up_file').click()"><div class="ph">点击上传图片</div></div>
        <input type="file" id="tr_up_file" accept="image/*" multiple style="display:none" onchange="trUpload(event)">
        <label>目标语言</label><select id="tr_lang"></select>
        <label>商品标题（可编辑）</label><textarea id="tr_title" rows="2" placeholder="读取链接后自动填入，或手动输入"></textarea>
        <button class="btn" id="tr_btn" onclick="doTranslate()" style="width:100%;margin-top:8px">🌐 一键翻译标题 + 选中图片</button>
        <div id="tr_msg" class="hint"></div>
      </div></div>
      <div class="work"><div class="card" style="max-width:none;margin:0;min-height:440px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><h3 style="margin:0;flex:1">🖼 图片（点选要翻译的）</h3><span class="hint" id="tr_cnt"></span></div>
        <div id="tr_grid"><div class="iempty">读取 1688 链接或上传图片后，在这里勾选要翻译的图片。</div></div>
        <div id="tr_result" style="margin-top:12px"></div>
      </div></div>
    </div>
  </section>
  <section id="assets" class="page">
    <div class="card" style="max-width:none;margin:0">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
        <h3 style="margin:0">📦 资产库</h3><span class="hint" id="as_meta" style="flex:1"></span>
        <span class="actbtn" onclick="assetsLoad()">↻ 刷新</span>
      </div>
      <div id="as_tabs" style="margin-bottom:12px"></div>
      <div id="as_body" class="hint">加载中…</div>
    </div>
  </section>
  <section id="settings">
    <div class="card">
      <h3>平台凭证</h3>
      <p class="h">填写你自己的平台账号密钥，填好后即可使用选品/采集/上架等功能；仅保存在本盒子，不会上传。</p>
      <div class="grid2">
        <div><label>妙手 App Key</label><input id="c_mkey" placeholder="你的妙手 App Key"></div>
        <div><label>妙手 App Secret</label><input id="c_msec" type="password" placeholder="你的妙手 App Secret"></div>
      </div>
      <div class="grid2" style="margin-top:8px">
        <div><label>Ozon Client-Id</label><input id="c_ozid" placeholder="Ozon 卖家 Client-Id"></div>
        <div><label>Ozon Api-Key</label><input id="c_ozkey" type="password" placeholder="Ozon 卖家 Api-Key"></div>
      </div>
      <button class="btn" onclick="saveCreds()">保存凭证</button>
      <div id="cmsg" class="note" style="display:none"></div>
      <div class="h" style="margin-top:6px">妙手凭证：登录 <b>妙手开放平台</b> → 创建你自己的应用 → 拿到 App Key / App Secret（你的采集箱数据与其他人完全独立）。</div>
      <div class="h" style="margin-top:4px">Ozon 凭证：到 <b>Ozon 卖家后台 → 设置 → API 密钥</b> 获取。</div>
    </div>
    <div class="card" id="lic_card">
      <h3 id="lic_title">盒子激活</h3>
      <p class="h">盒子ID：<code id="lic_bid">-</code>　状态：<b id="lic_st">-</b>　<span class="h" id="lic_exp" style="margin:0"></span></p>
      <div id="lic_form">
        <label id="lic_codelabel">激活码</label>
        <input id="lic_code" placeholder="FH-XXXX-XXXX-XXXX" style="text-transform:uppercase">
        <button class="btn" onclick="doActivate()" id="lic_btn">激活</button>
        <div id="lic_msg" class="note" style="display:none"></div>
      </div>
      <div class="h" id="lic_hint" style="margin-top:6px">激活码由卖家提供。激活后即可使用全部功能。</div>
    </div>
    <div class="card">
      <h3>飞猴 1688 采集插件</h3>
      <p class="h">Chrome / Edge 浏览器扩展，登录 1688 后一键采集 → 盒子 AI 选品评分。可在任意设备从本页下载，无需 U 盘拷贝。</p>
      <a class="btn" href="/download/extension" style="display:inline-block;text-decoration:none;text-align:center;width:auto;padding-left:18px;padding-right:18px">⬇ 下载采集插件 (.zip)</a>
      <div class="h" style="margin-top:10px;line-height:1.7">安装：解压后，打开浏览器 <b>扩展程序</b> 页 → 开启「<b>开发者模式</b>」→ 点「<b>加载已解压的扩展程序</b>」→ 选择解压出的 <code>zhuiwen-1688</code> 文件夹。固定到工具栏即可使用。</div>
    </div>
    <div class="card">
      <h3>🛫 飞书（Lark）对话接入</h3>
      <p class="h">配置后，可直接在飞书里跟「飞猴」对话，并<b>自动执行选品/采集/翻译/上架</b>等功能；飞书里的对话会<b>实时同步到本页「对话」</b>。</p>
      <div class="grid2">
        <div><label>App ID</label><input id="fs_id" placeholder="cli_xxxxxxxx"></div>
        <div><label>App Secret</label><input id="fs_sec" type="password" placeholder="飞书应用 App Secret"></div>
      </div>
      <label>Verification Token（事件订阅，可选但建议填）</label><input id="fs_vt" placeholder="飞书事件订阅 Verification Token">
      <button class="btn" onclick="saveFeishu()">保存飞书配置</button>
      <div id="fsmsg" class="note" style="display:none"></div>
      <div class="kv" style="margin-top:10px">事件请求地址（填到飞书开放平台「事件订阅」）：</div>
      <div style="display:flex;gap:8px;align-items:center"><code id="fs_hook" style="flex:1;word-break:break-all;background:var(--soft);padding:6px 9px;border-radius:8px;font-size:12px"></code><span class="actbtn" onclick="copyHook()">复制</span></div>
      <div class="h" style="margin-top:8px;line-height:1.7">步骤：飞书开放平台建<b>企业自建应用</b> → 开通<b>机器人</b>能力 → 权限加 <code>im:message</code>、<code>im:message:send_as_bot</code> → 「事件订阅」填上面地址并订阅<b>接收消息</b> → 把 App ID/Secret/Verification Token 填到这里保存 → 在飞书把机器人拉进群或私聊即可。<b>注：需公网环境。</b></div>
    </div>
    <div class="card">
      <h3>网关与模型状态</h3>
      <div class="kv">网关服务：<b id="s_gw">…</b></div>
      <div class="kv">当前默认模型：<b id="s_md">…</b></div>
      <div class="kv">智能体：<b id="s_ag">…</b></div>
      <button class="btn" onclick="refresh()">刷新状态</button>
    </div>
  </section>
</main>
<div id="modal" onclick="closeModal(event)"><div id="modal_in" onclick="event.stopPropagation()"><span id="modal_x" onclick="closeModal(null,true)">×</span><div id="modal_c"></div></div></div>
<div id="toast"></div>
<script>
var modalLock=false;
function _showModal(){document.getElementById('modal').style.display='flex';}
function openImg(u){modalLock=false;document.getElementById('modal_c').innerHTML='<img src="'+u+'" referrerpolicy="no-referrer">';_showModal();}
function openVideo(u){modalLock=false;document.getElementById('modal_c').innerHTML='<video src="'+u+'" controls autoplay></video>';_showModal();}
function openHtml(h,lock){modalLock=!!lock;document.getElementById('modal_c').innerHTML='<div class="mc rep">'+h+'</div>';_showModal();}
function closeModal(e,force){if(force||(e&&e.target.id==='modal'&&!modalLock))document.getElementById('modal').style.display='none';}
function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.style.display='block';clearTimeout(window._tt);window._tt=setTimeout(function(){t.style.display='none';},2200);}
const SID = Math.random().toString(36).slice(2,10);
const CHIPS = ["🔥 热销爆款选品","🌊 蓝海机会挖掘","💬 竞品差评VOC分析","🛒 1688找货源",
"🧭 选品可行性分析","📝 生成Listing与卖点","💰 定价与利润测算"];
var hot_loaded=0,bx_page=1,a_type='feasibility';
function tab(v){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.v===v));
['chat','hot','box','analysis','image','video','translate','assets','settings'].forEach(s=>{var e2=document.getElementById(s);if(e2)e2.style.display=(s===v)?(s==='chat'?'flex':'block'):'none';});
if(v==='settings')refresh();if(v==='assets')assetsLoad();if(v==='box')boxLoad(1);if(v==='analysis')aChips();
if(v==='image'&&!window._igInit){window._igInit=1;igTab('main');}
if(v==='video'&&!window._vdInit){window._vdInit=1;vdTab('t2v');}
if(v==='translate')trInit();
if(v==='hot'&&!hot_loaded){hot_loaded=1;hotLoad();}}
function apiP(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());}
function apiG(u){return fetch(u).then(r=>r.json());}
function fnum(n){n=parseFloat(n)||0;if(n<=0)return '-';if(n>=1e6)return (n/1e6).toFixed(1)+'M';if(n>=1e3)return (n/1e3).toFixed(1)+'K';return ''+Math.round(n);}
var hot_rows=[];
async function hotLoad(){const hint=document.getElementById('h_hint');hint.textContent='查询中…';
 try{const j=await apiP('/api/hot',{site:h_site.value,period:h_period.value,sort:h_sort.value,category:h_cat.value.trim(),keyword:h_kw.value.trim(),limit:+h_limit.value||30});
 if(!j.ok){hint.textContent='✗ '+j.error;return;}hot_rows=j.rows||[];
 hint.textContent='共 '+hot_rows.length+' 条 · 快照 '+((hot_rows[0]&&hot_rows[0].scraped_at)?hot_rows[0].scraped_at.slice(0,10):'');
 let h='<tr><th><input type="checkbox" onclick="hotAll(this)"></th><th>#</th><th>图</th><th>商品</th><th>类目</th><th>价¥</th><th>日销</th><th>总销</th><th>预估GMV</th><th>达人</th><th>佣金</th><th>操作</th></tr>';
 hot_rows.forEach((r,i)=>{h+='<tr><td><input type="checkbox" class="hchk" value="'+i+'"></td><td>'+(r.rank||'')+'</td>'
  +'<td class="thumb">'+(r.image_url?'<img src="'+r.image_url+'" referrerpolicy="no-referrer" loading="lazy" onclick="openImg(\''+r.image_url+'\')">':'')+'</td>'
  +'<td>'+_esc((r.title||'').slice(0,40))+'</td><td>'+_esc(r.category||'')+'</td><td>'+fnum(r.price_cny)+'</td><td>'+fnum(r.daily_sales)+'</td><td>'+fnum(r.sold_count)+'</td><td>¥'+fnum(r.est_gmv)+'</td><td>'+fnum(r.creator_count)+'</td><td>'+(r.commission_rate?Math.round(r.commission_rate)+'%':'-')+'</td>'
  +'<td><span class="actbtn" onclick="hotKw('+i+')">提取关键词</span><span class="actbtn" onclick="hotAnalyze('+i+')">分析</span></td></tr>';});
 document.getElementById('h_tbl').innerHTML=h;}catch(e){hint.textContent='✗ '+e;}}
function hotAll(c){document.querySelectorAll('.hchk').forEach(x=>x.checked=c.checked);}
function hotSel(){return [...document.querySelectorAll('.hchk:checked')].map(x=>hot_rows[+x.value]).filter(Boolean);}
async function hotKw(i){const t=hot_rows[i]&&hot_rows[i].title;if(!t)return;const m=document.getElementById('h_kwmsg');m.textContent='提取中…';
 const j=await apiP('/api/extract-keywords',{titles:[t]});if(!j.ok){m.textContent='✗ '+j.error;return;}m.textContent='';openKw(j.items||[]);}
async function hotKwBatch(){const sel=hotSel();const m=document.getElementById('h_kwmsg');if(!sel.length){m.textContent='请先勾选商品';return;}m.textContent='批量提取中…（'+sel.length+' 个）';
 const j=await apiP('/api/extract-keywords',{titles:sel.map(r=>r.title)});if(!j.ok){m.textContent='✗ '+j.error;return;}m.textContent='✓ 完成';openKw(j.items||[]);}
function openKw(items){let h='<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;padding-right:26px"><h3 style="margin:0;flex:1">🔑 1688 关键词</h3><span class="actbtn" onclick="kwAll()">全选/反选</span><span class="actbtn" onclick="copyKw()" style="background:var(--mint);color:#fff;border-color:var(--mint)">复制选中</span></div>';
 items.forEach(it=>{h+='<div style="margin:8px 0;border-top:1px dashed var(--bd);padding-top:8px"><div style="font-weight:700;font-size:12.5px;margin-bottom:6px">'+_esc((it.title||'').slice(0,42))+'</div>';
  (it.keywords||[]).forEach(k=>{h+='<label class="kbtag" style="cursor:pointer;display:inline-flex;align-items:center;gap:5px"><input type="checkbox" class="kwchk" value="'+_esc(k)+'" checked>'+_esc(k)+'</label>';});
  if(!(it.keywords||[]).length)h+='<span class="hint">未提取到</span>';h+='</div>';});
 h+='<div class="hint" style="margin-top:10px">勾选关键词 → 复制 → 粘贴到「飞猴采集插件」的关键词框采集。</div>';openHtml(h,true);}
function kwAll(){const all=[...document.querySelectorAll('.kwchk')];const on=all.some(x=>!x.checked);all.forEach(x=>x.checked=on);}
function copyKw(){const ks=[...document.querySelectorAll('.kwchk:checked')].map(x=>x.value);if(!ks.length){toast('请先勾选关键词');return;}const txt=ks.join('\n');
 (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(function(){toast('已复制 '+ks.length+' 个关键词，可粘贴到采集插件');}).catch(function(){const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');toast('已复制 '+ks.length+' 个');}catch(e){toast('复制失败');}ta.remove();});}
function hotAnalyze(i){const t=hot_rows[i]&&hot_rows[i].title;if(!t)return;tab('analysis');aChips();document.getElementById('a_kw').value=t.slice(0,40);}
function usd(c){c=parseFloat(c)||0;return (c/7.2).toFixed(2);}
async function boxLoad(pg){bx_page=pg||1;const hint=document.getElementById('b_hint');hint.textContent='读取中…';
 try{const j=await apiP('/api/box',{page:bx_page,limit:+document.getElementById('b_limit').value||20});
 if(!j.ok){hint.textContent='✗ '+j.error;return;}const rows=j.rows||[];hint.textContent='第 '+bx_page+' 页 · '+rows.length+' 件';
 let h='<tr><th><input type="checkbox" onclick="boxAll(this)"></th><th>产品信息</th><th>货源价格</th><th>库存</th><th>重量kg</th><th>创建时间</th><th>状态</th><th>操作</th></tr>';
 for(const r of rows){const rng=(r.price_min&&r.price_max&&r.price_min!==r.price_max);
  const pr='CNY '+(rng?(r.price_min+'~'+r.price_max):(r.price_cny||0));
  const ur='USD '+(rng?(usd(r.price_min)+'~'+usd(r.price_max)):usd(r.price_cny));
  h+='<tr><td><input type="checkbox" class="bchk" value="'+r.id+'"></td>'
   +'<td><div style="display:flex;gap:9px"><div class="thumb">'+(r.image?'<img src="'+r.image+'" referrerpolicy="no-referrer" loading="lazy" onclick="openImg(\''+r.image+'\')">':'')+'</div>'
   +'<div style="min-width:0"><div>'+_esc((r.title||'').slice(0,52))+'</div><div class="hint" style="margin:3px 0 0">'+(r.item_num?('货号 '+_esc(String(r.item_num))+' · '):'')+'采集箱ID '+r.id+'<br>货源 '+_esc(String(r.source_id||''))+' '+(r.source_url?'<a href="'+r.source_url+'" target="_blank">1688↗</a>':'')+'</div></div></div></td>'
   +'<td style="white-space:nowrap">'+pr+'<div class="hint" style="margin:2px 0 0">'+ur+'</div></td>'
   +'<td>'+fnum(r.stock)+'</td><td>'+(r.weight||0)+'</td><td style="white-space:nowrap;font-size:11px">'+_esc(r.created||'')+'</td>'
   +'<td><span class="pillx '+(r.status==='success'?'ok':'no')+'">'+_esc(r.status||'')+'</span></td>'
   +'<td style="white-space:nowrap"><span class="actbtn" onclick="boxEdit('+r.id+')">编辑</span><span class="actbtn" style="color:#dc2626;border-color:#fecaca" onclick="boxDelOne('+r.id+')">删除</span></td></tr>';}
 h+='<tr><td colspan="8" style="padding:10px"><button class="btn" style="margin:0;background:#eef0f3;color:#374151" onclick="boxLoad('+Math.max(1,bx_page-1)+')">‹ 上一页</button> <button class="btn" style="margin:0" onclick="boxLoad('+(bx_page+1)+')">下一页 ›</button></td></tr>';
 document.getElementById('b_tbl').innerHTML=h;}catch(e){hint.textContent='✗ '+e;}}
function boxDelOne(id){if(!confirm('确定删除该商品？'))return;apiP('/api/box/delete',{ids:[id]}).then(j=>{if(j.ok)boxLoad(bx_page);else alert(j.error);});}
var BOX_EDIT=null,BE_IMGS=[];
function openWide(h){modalLock=true;document.getElementById('modal_c').innerHTML='<div class="rep" style="max-width:980px;width:92vw;max-height:90vh;overflow:auto;padding:20px 24px;box-sizing:border-box">'+h+'</div>';_showModal();}
var BE_ATTRS=[],BE_SKUS=[],BE_SP={};
async function boxEdit(id){openWide('<div style="padding:8px"><span class="typing">读取商品详情…</span></div>');
 const j=await apiP('/api/box/detail',{id:id});
 if(!j.ok){openWide('<div class="bad" style="padding:14px">✗ '+_esc(j.error||'读取失败')+'</div>');return;}
 BOX_EDIT={id:id};BE_IMGS=(j.imgUrls||[]).map(function(u){return {url:u,display:u,sel:true};});
 BE_ATTRS=(j.sourceAttrs||[]).map(function(a){return {name:a.name||'',value:a.value||''};});
 BE_SP={c:j.colorPropName||'颜色',s:j.sizePropName||'尺寸',p3:j.saleProp3Name||''};
 BE_SKUS=Object.keys(j.skuMap||{}).map(function(k){var p=k.split(';');var v=(j.skuMap||{})[k]||{};
   return {key:k,color:p[1]||'',size:p[2]||'',p3:p[3]||'',price:v.price==null?'':v.price,stock:v.stock==null?'':v.stock,weight:v.weight==null?'':v.weight};});
 var fld=function(lb,id2,val,t){return '<div style="flex:1"><label style="margin:10px 0 4px">'+lb+'</label><input id="'+id2+'" '+(t||'')+' value="'+_esc(String(val==null?'':val))+'"></div>';};
 var cate=(j.cateList||[]).join(' / ');
 var h='<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;padding-right:26px"><h3 style="margin:0;flex:1;font-size:17px">✏️ 编辑采集箱商品 #'+id+'</h3>'
  +(cate?'<span class="hint" style="margin:0">类目：'+_esc(cate)+'</span>':'')+'<button class="btn" onclick="boxEditSave()" style="margin:0">保存修改</button></div>'
  // 基本信息
  +'<div class="becard"><div class="behd">基本信息</div>'
  +'<label style="margin:2px 0 4px">产品标题 *</label><textarea id="be_title" rows="2">'+_esc(j.title||'')+'</textarea>'
  +'<div style="display:flex;gap:14px">'+fld('货号','be_itemNum',j.itemNum||'')+fld('货源价(¥)','be_price',j.price||0,'type=number step=0.01')+'</div>'
  +'<div style="display:flex;gap:14px">'+fld('库存','be_stock',j.stock||0,'type=number')+fld('重量(kg)','be_weight',j.weight||0,'type=number step=0.01')+'</div>'
  +'<label style="margin:10px 0 4px">简易描述</label><textarea id="be_notes" rows="3">'+_esc(j.notesText||'')+'</textarea></div>'
  // 产品属性
  +'<div class="becard"><div class="behd">产品属性 <span class="hint" style="margin:0">（'+BE_ATTRS.length+' 项）</span><span class="actbtn" style="float:right" onclick="beAttrAdd()">＋ 新增属性</span></div><div id="be_attrs"></div></div>'
  // 销售属性 / SKU
  +'<div class="becard"><div class="behd">销售属性 / SKU <span class="hint" style="margin:0">（'+BE_SKUS.length+' 个规格，可改 货源价/库存/重量）</span></div><div id="be_skus" style="overflow:auto"></div></div>'
  // 产品图片
  +'<div class="becard"><div class="behd">产品图片 <span id="be_imgn" class="hint" style="margin:0"></span><span class="actbtn" style="float:right" onclick="document.getElementById(\'be_upfile\').click()">＋ 上传图片</span></div>'
  +'<div class="hint" style="margin:0 0 6px">点图放大 · × 删除 · 取消勾选则不写回</div>'
  +'<input type="file" id="be_upfile" accept="image/*" multiple style="display:none" onchange="boxEditUpload(event)">'
  +'<div class="thumbrow" id="be_imgs"></div></div>'
  +'<div id="be_msg" class="hint" style="margin-top:6px"></div>';
 openWide(h);beRenderImgs();beRenderAttrs();beRenderSkus();}
function beRenderAttrs(){var box=document.getElementById('be_attrs');if(!box)return;
 box.innerHTML=BE_ATTRS.map(function(a,i){return '<div class="attrcell"><input class="ak" value="'+_esc(a.name)+'" oninput="BE_ATTRS['+i+'].name=this.value" placeholder="属性名"><input class="av" value="'+_esc(a.value)+'" oninput="BE_ATTRS['+i+'].value=this.value" placeholder="属性值"><div class="x2" onclick="BE_ATTRS.splice('+i+',1);beRenderAttrs()">×</div></div>';}).join('')||'<div class="hint" style="margin:0">无属性，点「＋ 新增属性」添加</div>';}
function beAttrAdd(){BE_ATTRS.push({name:'',value:''});beRenderAttrs();}
function beRenderSkus(){var box=document.getElementById('be_skus');if(!box)return;
 if(!BE_SKUS.length){box.innerHTML='<div class="hint" style="margin:0">单规格商品（无多 SKU）</div>';return;}
 var p3=BE_SP.p3&&BE_SKUS.some(function(s){return s.p3;});
 var th='<tr><th>'+_esc(BE_SP.c)+'</th><th>'+_esc(BE_SP.s)+'</th>'+(p3?'<th>'+_esc(BE_SP.p3)+'</th>':'')+'<th>货源价¥</th><th>库存</th><th>重量kg</th></tr>';
 var rows=BE_SKUS.map(function(s,i){return '<tr><td>'+_esc(s.color)+'</td><td>'+_esc(s.size||'-')+'</td>'+(p3?'<td>'+_esc(s.p3||'-')+'</td>':'')
   +'<td><input type="number" step="0.01" style="width:78px" value="'+_esc(String(s.price))+'" oninput="BE_SKUS['+i+'].price=this.value"></td>'
   +'<td><input type="number" style="width:72px" value="'+_esc(String(s.stock))+'" oninput="BE_SKUS['+i+'].stock=this.value"></td>'
   +'<td><input type="number" step="0.01" style="width:64px" value="'+_esc(String(s.weight))+'" oninput="BE_SKUS['+i+'].weight=this.value"></td></tr>';}).join('');
 box.innerHTML='<table class="skutb">'+th+rows+'</table>';}
function beRenderImgs(){var row=document.getElementById('be_imgs');if(!row)return;
 row.innerHTML=BE_IMGS.map(function(im,i){return '<div class="tb" style="width:84px;height:84px"><img src="'+im.display+'" referrerpolicy="no-referrer" onclick="openImg(\''+im.display+'\')"><label style="position:absolute;top:1px;left:1px;background:rgba(255,255,255,.85);border-radius:4px;padding:0 2px"><input type="checkbox" class="be_imgchk" '+(im.sel?'checked':'')+' onchange="BE_IMGS['+i+'].sel=this.checked" style="accent-color:var(--mint)"></label><div class="x" onclick="BE_IMGS.splice('+i+',1);beRenderImgs()">×</div></div>';}).join('');
 var n=document.getElementById('be_imgn');if(n)n.textContent='（'+BE_IMGS.length+' 张）';}
async function boxEditUpload(ev){var fs=[].slice.call(ev.target.files);var m=document.getElementById('be_msg');for(var i=0;i<fs.length;i++){m.className='hint';m.textContent='上传第 '+(i+1)+' 张…';var d=await fileToScaledDataURL(fs[i],1536,0.85);try{var j=await apiP('/api/box/upload-img',{b64:d});if(j.ok)BE_IMGS.push({url:j.url,display:j.display,sel:true});}catch(e){}}ev.target.value='';m.textContent='';beRenderImgs();}
async function boxEditSave(){if(!BOX_EDIT)return;var m=document.getElementById('be_msg');m.className='hint';m.textContent='保存中…';
 var imgs=BE_IMGS.filter(function(x){return x.sel;}).map(function(x){return x.url;});
 var attrs=BE_ATTRS.filter(function(a){return (a.name||'').trim();}).map(function(a){return {name:a.name.trim(),value:a.value};});
 var skuMap={};BE_SKUS.forEach(function(s){skuMap[s.key]={price:s.price,stock:s.stock,weight:s.weight};});
 var changes={title:be_title.value.trim(),itemNum:be_itemNum.value.trim(),price:be_price.value,stock:be_stock.value,weight:be_weight.value,notesText:be_notes.value,imgUrls:imgs,sourceAttrs:attrs,skuMap:skuMap};
 if(!changes.title){m.className='hint bad';m.textContent='标题必填';return;}
 var j=await apiP('/api/box/edit',{id:BOX_EDIT.id,changes:changes});
 if(j.ok){m.className='hint ok';m.textContent='✓ 已保存';toast('采集箱商品已更新');setTimeout(function(){closeModal(null,true);boxLoad(bx_page);},700);}
 else{m.className='hint bad';m.textContent='✗ '+(j.error||'保存失败');}}
var TPL=null,TPL_SHOPS=[];
function tfld(lb,id,val,t,w){return '<div><label style="margin:0 0 4px">'+lb+'</label><input id="'+id+'" '+(t||'')+' value="'+_esc(String(val==null?'':val))+'" style="width:'+(w||100)+'px"></div>';}
function tck(id,on,lb){return '<label style="font-weight:400;display:inline-flex;align-items:center;gap:5px;margin-right:16px;font-size:13px"><input type="checkbox" id="'+id+'"'+(on?' checked':'')+'> '+lb+'</label>';}
async function openTpl(){openWide('<div style="padding:8px"><span class="typing">读取模板配置…</span></div>');
 var jt=await fetch('/api/templates').then(function(r){return r.json();}).catch(function(){return {};});
 TPL=(jt&&jt.templates)||{};
 var js=await fetch('/api/tk/shops').then(function(r){return r.json();}).catch(function(){return {shops:[]};});
 TPL_SHOPS=(js&&js.shops)||[];
 var P=TPL.pricing||{},C=TPL.collect||{},CL=TPL.claim||{},L=TPL.logistics||{};
 var LANGS=['英语','印尼语','马来语','泰语','越南语','菲律宾语','西班牙语','葡萄牙语','俄语','日语','韩语','德语','法语','意大利语','阿拉伯语'];
 var langopt=LANGS.map(function(l){return '<option'+(l===(C.lang||'英语')?' selected':'')+'>'+l+'</option>';}).join('');
 var shopopt='<option value="">— 选择店铺 —</option>'+TPL_SHOPS.map(function(s){return '<option value="'+s.shopId+'" data-site="'+_esc(s.site||'')+'"'+(String(s.shopId)===String(CL.shopId)?' selected':'')+'>'+_esc((s.platform||'tiktok')+' · '+(s.site||'')+' · '+(s.shopName||s.shopId))+'</option>';}).join('');
 var h='<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-right:26px"><h3 style="margin:0;flex:1;font-size:17px">⚙ 采集 / 上架模板</h3><button class="btn" onclick="tplSave()" style="margin:0">保存模板</button></div>'
  +'<p class="hint" style="margin:0 0 4px">配置后采集和上架自动应用：采集设置用于快速导入；定价、物流等用于上架到 TikTok。</p>'
  +'<div class="becard"><div class="behd">💰 定价模板 '+tck('tp_en',P.enabled,'启用')+'</div>'
  +'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">'+tfld('目标币种','tp_cur',P.currency||'MYR','',90)
  +tfld('汇率 CNY→币种','tp_ex',P.exchange==null?0.62:P.exchange,'type=number step=0.0001',130)
  +tfld('加价率 %','tp_mk',P.markup_pct==null?60:P.markup_pct,'type=number',90)
  +tfld('固定加价','tp_add',P.add_fixed||0,'type=number step=0.01',90)+'</div>'
  +'<div style="margin-top:8px">'+tck('tp_r99',P.round99,'售价取 .99 尾数')+'</div>'
  +'<div class="hint" id="tp_demo" style="margin-top:6px"></div></div>'
  +'<div class="becard"><div class="behd">🛒 采集默认</div>'
  +'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end"><div><label style="margin:0 0 4px">默认目标语言</label><select id="tc_lang" style="min-width:120px">'+langopt+'</select></div>'
  +tfld('评分阈值','tc_th',C.threshold==null?70:C.threshold,'type=number',90)+'</div>'
  +'<div style="margin-top:8px">'+tck('tc_score',C.score,'默认AI评分')+tck('tc_tr',C.auto_translate,'默认翻译标题')+tck('tc_ti',C.trans_images,'默认翻译图片')+'</div></div>'
  +'<div class="becard"><div class="behd">📦 认领 / 店铺</div>'
  +'<label style="margin:0 0 4px">目标 TikTok 店铺'+(TPL_SHOPS.length?'':'（未读到店铺，请先绑定 TikTok 店铺）')+'</label><select id="tcl_shop" style="width:100%" onchange="tplShopSite()">'+shopopt+'</select>'
  +'<div style="display:flex;gap:12px;margin-top:8px">'+tfld('站点','tcl_site',CL.site||'MY','',90)+tfld('仓库ID(可空)','tcl_wh',CL.warehouse||'','',150)+'</div></div>'
  +'<div class="becard"><div class="behd">🚚 物流模板</div><div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">'
  +tfld('默认重量kg','tl_w',L.weight_default==null?0.1:L.weight_default,'type=number step=0.01',110)
  +tfld('长cm','tl_l',L.package_l||0,'type=number',70)+tfld('宽cm','tl_wd',L.package_w||0,'type=number',70)+tfld('高cm','tl_h',L.package_h||0,'type=number',70)+'</div></div>'
  +'<div id="tpl_msg" class="hint"></div>';
 openWide(h);tplDemo();
 ['tp_ex','tp_mk','tp_add','tp_r99','tp_cur'].forEach(function(id){var e=document.getElementById(id);if(e)e.addEventListener('input',tplDemo);});}
function tplShopSite(){var s=document.getElementById('tcl_shop');var o=s.options[s.selectedIndex];var st=o&&o.getAttribute('data-site');if(st)document.getElementById('tcl_site').value=st;}
function tplDemo(){var d=document.getElementById('tp_demo');if(!d)return;
 var ex=parseFloat(document.getElementById('tp_ex').value)||0,mk=parseFloat(document.getElementById('tp_mk').value)||0,add=parseFloat(document.getElementById('tp_add').value)||0,r99=document.getElementById('tp_r99').checked,cur=document.getElementById('tp_cur').value||'';
 var p=100*ex*(1+mk/100)+add;if(r99&&p>=1)p=Math.floor(p)+0.99;
 d.innerHTML='示例：货源价 <b>¥100</b> → 预估售价 <b>'+p.toFixed(2)+' '+_esc(cur)+'</b>';}
async function tplSave(){var m=document.getElementById('tpl_msg');m.className='hint';m.textContent='保存中…';
 var sel=document.getElementById('tcl_shop');var opt=sel.options[sel.selectedIndex];
 var t={pricing:{enabled:document.getElementById('tp_en').checked,currency:document.getElementById('tp_cur').value.trim(),exchange:document.getElementById('tp_ex').value,markup_pct:document.getElementById('tp_mk').value,add_fixed:document.getElementById('tp_add').value,round99:document.getElementById('tp_r99').checked},
  collect:{lang:document.getElementById('tc_lang').value,threshold:document.getElementById('tc_th').value,score:document.getElementById('tc_score').checked,auto_translate:document.getElementById('tc_tr').checked,trans_images:document.getElementById('tc_ti').checked},
  claim:{shopId:sel.value,site:document.getElementById('tcl_site').value.trim()||(opt?opt.getAttribute('data-site'):'')||'',warehouse:document.getElementById('tcl_wh').value.trim()},
  logistics:{weight_default:document.getElementById('tl_w').value,package_l:document.getElementById('tl_l').value,package_w:document.getElementById('tl_wd').value,package_h:document.getElementById('tl_h').value}};
 var j=await apiP('/api/templates',{templates:t});
 if(j.ok){m.className='hint ok';m.textContent='✓ 模板已保存，采集/上架将自动套用';toast('模板已保存');}else{m.className='hint bad';m.textContent='✗ '+(j.error||'保存失败');}}
async function openTkList(){
 var ids=[...document.querySelectorAll('.bchk:checked')].map(function(x){return x.value;});
 openWide('<div style="padding:8px"><span class="typing">读取店铺与配置…</span></div>');
 var jt=await fetch('/api/templates').then(function(r){return r.json();}).catch(function(){return {};});
 var CL=((jt&&jt.templates)||{}).claim||{};
 var js=await fetch('/api/tk/shops').then(function(r){return r.json();}).catch(function(){return {shops:[]};});
 var shops=(js&&js.shops)||[];
 var defShop=CL.shopId||(shops[0]&&shops[0].shopId)||'';
 var shopopt=shops.map(function(s){return '<option value="'+s.shopId+'" data-site="'+_esc(s.site||'')+'"'+(String(s.shopId)===String(defShop)?' selected':'')+'>'+_esc((s.platform||'tiktok')+' · '+(s.site||'')+' · '+(s.shopName||s.shopId))+'</option>';}).join('');
 var h='<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-right:26px"><h3 style="margin:0;flex:1;font-size:17px">📤 上架到 TikTok 店铺</h3></div>'
  +'<div class="becard">'
  +'<label style="margin:0 0 4px">目标店铺</label><select id="tk_shop" style="width:100%">'+(shopopt||'<option value="">未读到店铺，请先绑定 TikTok 店铺</option>')+'</select>'
  +'<div style="margin:12px 0 4px"><b>'+(ids.length?('已选中 '+ids.length+' 个商品'):'未勾选商品')+'</b>'+(ids.length?'':' —— 请先在列表勾选要上架的商品')+'</div>'
  +'<label style="font-weight:400;display:flex;align-items:center;gap:7px;margin-top:8px;font-size:13.5px"><input type="checkbox" id="tk_auto"> <b>是否自动发布</b>（开：AI 选类目→预填→<b>直接发布</b>；关：预填后停，去 TikTok 卖家后台确认再发布）</label>'
  +'<div class="hint" style="margin-top:6px">类目由 AI 按标题匹配；重量/包裹尺寸套用「模板配置→物流」。建议先关「自动」跑一两个验证类目准确度。</div>'
  +'<button class="btn" id="tk_go" onclick="doTkList()" style="margin-top:12px"'+(ids.length?'':' disabled')+'>开始上架</button>'
  +'<div id="tk_msg" class="hint" style="margin-top:8px"></div></div>'
  +'<div id="tk_res"></div>';
 openWide(h);TK_IDS=ids;}
var TK_IDS=[];
async function doTkList(){var m=document.getElementById('tk_msg'),btn=document.getElementById('tk_go');
 var sel=document.getElementById('tk_shop');var shopId=sel.value;var site=(sel.options[sel.selectedIndex]||{}).getAttribute?sel.options[sel.selectedIndex].getAttribute('data-site'):'';
 if(!shopId){m.className='hint bad';m.textContent='请选择店铺';return;}
 if(!TK_IDS.length){m.className='hint bad';m.textContent='未选择商品';return;}
 var auto=document.getElementById('tk_auto').checked;
 btn.disabled=true;m.className='hint';m.textContent=(auto?'认领→选类目→预填→发布':'认领→选类目→预填')+'中…（'+TK_IDS.length+' 个，约每个 5-15 秒）';
 try{var j=await apiP('/api/tk/list',{ids:TK_IDS,shopId:shopId,site:site,auto:auto});
  if(!j.ok){m.className='hint bad';m.textContent='✗ '+(j.error||'失败');btn.disabled=false;return;}
  var s=j.summary||{};m.className='hint ok';m.textContent='✓ 完成：预填 '+(s.prepared||0)+(auto?(' · 已发布 '+(s.published||0)):'')+' · 失败 '+(s.failed||0)+' / 共 '+(s.total||0);
  var st={prepared:'🟡 已预填(待发布)',published:'🟢 已发布',publish_fail:'🔴 发布失败',prefill_fail:'🔴 预填失败',fail:'🔴 失败'};
  var rows=(j.results||[]).map(function(r){return '<tr><td>'+r.id+'</td><td style="text-align:left">'+_esc(r.title||'')+'</td><td>'+_esc(String(r.cid||'-'))+'</td><td>'+(st[r.status]||r.status)+'</td><td style="text-align:left;color:#dc2626">'+_esc(r.error||'')+'</td></tr>';}).join('');
  document.getElementById('tk_res').innerHTML='<div class="becard"><div class="behd">上架结果</div><div style="overflow:auto"><table class="skutb"><tr><th>商品ID</th><th>标题</th><th>类目</th><th>状态</th><th>说明</th></tr>'+rows+'</table></div>'+(auto?'':'<div class="hint" style="margin-top:8px">已预填到店铺，请在 TikTok 卖家后台「待发布」里确认类目/必填属性后发布；或勾选「自动发布」重试。</div>')+'</div>';
  btn.disabled=false;toast('上架处理完成');
 }catch(e){m.className='hint bad';m.textContent='✗ '+e;btn.disabled=false;}}
// ── 上架 Ozon ──
var OZ_IDS=[];
function openOzonList(){
 var ids=[...document.querySelectorAll('.bchk:checked')].map(function(x){return x.value;});
 var h='<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-right:26px"><h3 style="margin:0;flex:1;font-size:17px">📤 上架到 Ozon</h3></div>'
  +'<div class="becard">'
  +'<div style="margin:2px 0 4px"><b>'+(ids.length?('已选中 '+ids.length+' 个商品'):'未勾选商品')+'</b>'+(ids.length?'':' —— 请先在列表勾选要上架的商品')+'</div>'
  +'<div class="hint" style="margin-top:6px">上架到你的 Ozon 店铺（需先在「设置 → 平台凭证」填好 Ozon 凭证）。类目由 AI 自动匹配；上架后可在 Ozon 后台微调价格、尺寸、重量等信息。</div>'
  +'<div class="hint" style="margin-top:4px">建议先上架 1 个商品确认无误后再批量上架。</div>'
  +'<button class="btn" id="oz_go" onclick="doOzonList()" style="margin-top:12px;background:#005bff"'+(ids.length?'':' disabled')+'>开始上架 Ozon</button>'
  +'<div id="oz_msg" class="hint" style="margin-top:8px"></div></div>'
  +'<div id="oz_res"></div>';
 openWide(h);OZ_IDS=ids;}
async function doOzonList(){var m=document.getElementById('oz_msg'),btn=document.getElementById('oz_go');
 if(!OZ_IDS.length){m.className='hint bad';m.textContent='未选择商品';return;}
 btn.disabled=true;m.className='hint';m.textContent='匹配类目 → 构建 → 提交 Ozon 中…（'+OZ_IDS.length+' 个）';
 try{var j=await apiP('/api/ozon/list',{ids:OZ_IDS});
  var s=j.summary||{};
  if(j.ok){m.className='hint ok';m.textContent='✓ 已提交 '+(s.submitted||0)+' · 失败 '+(s.failed||0)+' / 共 '+(s.total||0)+(j.task_id?('　task_id='+j.task_id):'');}
  else{m.className='hint bad';m.textContent='✗ '+(j.error||'失败');}
  if(j.results&&j.results.length){
   var st={submitted:'🟢 已提交Ozon',prepared:'🟡 已构建',import_fail:'🔴 提交失败',fail:'🔴 失败'};
   var rows=j.results.map(function(r){return '<tr><td>'+r.id+'</td><td style="text-align:left">'+_esc(r.title||'')+'</td><td>'+_esc(String(r.cid||'-'))+'</td><td>'+(st[r.status]||r.status)+'</td><td style="text-align:left;color:#dc2626">'+_esc(r.error||'')+'</td></tr>';}).join('');
   document.getElementById('oz_res').innerHTML='<div class="becard"><div class="behd">上架结果</div><div style="overflow:auto"><table class="skutb"><tr><th>采集箱ID</th><th>标题</th><th>类目</th><th>状态</th><th>说明</th></tr>'+rows+'</table></div>'+(j.note?('<div class="hint" style="margin-top:8px">'+_esc(j.note)+'</div>'):'')+'</div>';}
  btn.disabled=false;
 }catch(e){m.className='hint bad';m.textContent='✗ '+e;btn.disabled=false;}}
function boxAll(c){document.querySelectorAll('.bchk').forEach(x=>x.checked=c.checked);}
function boxDel(){const ids=[...document.querySelectorAll('.bchk:checked')].map(x=>x.value);if(!ids.length){alert('请先勾选要删除的商品');return;}
 if(!confirm('确定删除选中的 '+ids.length+' 个商品？不可恢复。'))return;apiP('/api/box/delete',{ids}).then(j=>{if(j.ok){alert('已删除 '+j.deleted+' 个');boxLoad(bx_page);}else alert(j.error);});}
var A_TYPES=[['blue_ocean','🌊 蓝海机会','找高销量、评论少、竞争小、利润厚的潜力方向，给切入空间与风险'],
['voc','💬 竞品VOC','从竞品差评提取人群/场景/核心痛点与未满足需求 → 改良方向'],
['feasibility','🧭 可行性','市场趋势 + 价格带 + 供给竞争 三维评估是否值得做'],
['compare','⚔️ 竞品对比','对比主要竞品的卖点/定价/评分/差评，找差异化切入'],
['listing','📝 Listing卖点','生成符合 TikTok/Ozon 的高转化标题与五点卖点（中英文）'],
['pricing','💰 定价利润','结合成本/佣金/物流/退货 测算毛利并给定价区间']];
function aChips(){const c=document.getElementById('a_chips');
 if(c&&!c._b){c._b=1;A_TYPES.forEach(([k,l])=>{const e=el('span','chip2'+(k===a_type?' on':''),l);e.onclick=()=>aPick(k);c.appendChild(e);});}
 const d=document.getElementById('a_desc');if(d&&!d._b){d._b=1;d.innerHTML=A_TYPES.map(([k,l,desc])=>'<div class="card" style="max-width:none;margin:0;cursor:pointer" onclick="aPick(\''+k+'\')"><h3 style="font-size:14px">'+l+'</h3><div class="hint" style="margin:4px 0 0">'+desc+'</div></div>').join('');}
 aRecent();}
function aPick(k){a_type=k;document.querySelectorAll('#a_chips .chip2').forEach((x,i)=>x.classList.toggle('on',A_TYPES[i][0]===k));document.getElementById('a_kw').focus();}
var A_REP=[],aRepPage=1,A_REP_PP=5;
function aRecent(){apiG('/api/assets').then(a=>{A_REP=(a.reports||[]);aRepPage=1;aRepRender();});}
function aRepGo(p){aRepPage=p;aRepRender();}
function aRepRender(){var box=document.getElementById('a_recent');if(!box)return;
 var tot=A_REP.length,tp=Math.max(1,Math.ceil(tot/A_REP_PP));if(aRepPage>tp)aRepPage=tp;if(aRepPage<1)aRepPage=1;
 if(!tot){box.innerHTML='暂无（分析后自动保存到这里）';return;}
 var sl=A_REP.slice((aRepPage-1)*A_REP_PP,aRepPage*A_REP_PP);
 var h=sl.map(function(x){return '<div class="kv" style="cursor:pointer" onclick="viewReport(\''+x.name+'\')">📄 '+_esc(x.name)+' <span class="hint">('+x.size_kb+'KB)</span></div>';}).join('');
 if(tp>1)h+='<div style="text-align:center;margin:10px 0 0;font-size:13px;color:var(--mut)"><span class="actbtn" onclick="aRepGo('+Math.max(1,aRepPage-1)+')">‹ 上一页</span> 第 <b>'+aRepPage+'</b> / '+tp+' 页（共 '+tot+'）<span class="actbtn" onclick="aRepGo('+Math.min(tp,aRepPage+1)+')">下一页 ›</span></div>';
 box.innerHTML=h;}
function viewReport(n){apiP('/api/asset/view',{type:'report',name:n}).then(j=>{if(j.ok)openHtml(mdToHtml(j.content));else alert(j.error);});}
async function doAnalyze(){aChips();const kw=document.getElementById('a_kw').value.trim();const btn=document.getElementById('a_btn'),msg=document.getElementById('a_msg'),rep=document.getElementById('a_rep');
 btn.disabled=true;msg.textContent='分析中…（约 20-60 秒）';rep.style.display='none';
 try{const j=await apiP('/api/analyze',{keyword:kw,type:a_type});if(j.ok){msg.textContent='✓ 完成（报告已存入资产库）';rep.style.display='block';rep.innerHTML=mdToHtml(j.reply||'(空)');}else msg.textContent='✗ '+j.error;}catch(e){msg.textContent='✗ '+e;}btn.disabled=false;}
function renderMedia(t){t=t||'';const imgs=(t.match(/https?:\/\/[^\s)"']+\.(?:png|jpg|jpeg|webp|gif)/gi)||[]).map(u=>'<img class="amedia" src="'+u+'">').join('');const vids=(t.match(/https?:\/\/[^\s)"']+\.(?:mp4|mov|webm)/gi)||[]).map(u=>'<video class="amedia" controls src="'+u+'"></video>').join('');return imgs+vids+mdToHtml(t);}
// ── AI 创作工作室（主图/换装/抠图改图/详情 + 文生/图生视频）──────────
var STU={mn_imgs:[],tob_p_imgs:[],tob_m_imgs:[]};
function fileToScaledDataURL(file,maxDim,q){return new Promise(function(res,rej){if(!file){res('');return;}
 var fr=new FileReader();fr.onload=function(){var img=new Image();img.onload=function(){
  var w=img.width,h=img.height,m=maxDim||1536;if(w>m||h>m){if(w>=h){h=Math.round(h*m/w);w=m;}else{w=Math.round(w*m/h);h=m;}}
  var c=document.createElement('canvas');c.width=w;c.height=h;c.getContext('2d').drawImage(img,0,0,w,h);
  try{res(c.toDataURL('image/jpeg',q||0.82));}catch(e){res(fr.result);}};img.onerror=function(){res(fr.result);};img.src=fr.result;};
 fr.onerror=rej;fr.readAsDataURL(file);});}
function urlToDataURL(u){return fetch(u).then(function(r){return r.blob();}).then(function(b){return new Promise(function(res){var fr=new FileReader();fr.onload=function(){res(fr.result);};fr.readAsDataURL(b);});});}
async function stuUp(ev,key,zoneId){var f=ev.target.files[0];if(!f)return;var d=await fileToScaledDataURL(f,1536,0.85);STU[key]=d;var z=document.getElementById(zoneId);if(z)z.innerHTML='<img src="'+d+'">';}
async function stuUpMulti(ev,prefix){var fs=[].slice.call(ev.target.files);for(var i=0;i<fs.length;i++){var d=await fileToScaledDataURL(fs[i],1536,0.85);STU[prefix+'_imgs'].push(d);}ev.target.value='';stuThumbs(prefix);
 if(prefix==='mn'){var cf=document.getElementById('mn_confirm');if(cf)cf.style.display='none';var bb=document.getElementById('mn_batchbox');if(bb)bb.style.display='none';}}
function stuThumbs(prefix){var row=document.getElementById(prefix+'_thumbs');if(!row)return;var arr=STU[prefix+'_imgs']||[];
 if(!arr.length){row.innerHTML='<div class="upz" style="width:100%;box-sizing:border-box" onclick="document.getElementById(\''+prefix+'_file\').click()"><div class="ph">📷 点击上传图片（可多张）<br><span style="font-size:11px">支持 jpg/png/webp，单张 ≤10MB</span></div></div>';return;}
 row.innerHTML=arr.map(function(d,i){return '<div class="tb"><img src="'+d+'"><div class="x" onclick="stuDel(\''+prefix+'\','+i+')">×</div></div>';}).join('')+'<div class="add" onclick="document.getElementById(\''+prefix+'_file\').click()">＋</div>';}
function stuDel(prefix,i){STU[prefix+'_imgs'].splice(i,1);stuThumbs(prefix);}
function imgGrid(names){return '<div class="istudio-grid">'+names.map(function(n){var u='/asset/file?type=media&name='+encodeURIComponent(n);return '<div class="acard"><img class="im" src="'+u+'" onclick="openImg(\''+u+'\')"><div class="ft"><span title="'+_esc(n)+'">'+_esc(n)+'</span><a class="actbtn" href="'+u+'" download>下载</a></div></div>';}).join('')+'</div>';}
function vidGrid(names){return '<div class="istudio-grid">'+names.map(function(n){var u='/asset/file?type=media&name='+encodeURIComponent(n);return '<div class="acard" style="width:260px"><video class="im" style="width:260px;height:auto;max-height:360px" src="'+u+'" controls onclick="openVideo(\''+u+'\')"></video><div class="ft"><span title="'+_esc(n)+'">'+_esc(n)+'</span><a class="actbtn" href="'+u+'" download>下载</a></div></div>';}).join('')+'</div>';}
function igTab(v){['main','tryon','editor','detail'].forEach(function(k){var e=document.getElementById('ig_'+k);if(e)e.style.display=(k===v)?'block':'none';var t=document.getElementById('igt_'+k);if(t)t.classList.toggle('on',k===v);});if(v==='main')mnInit();if(v==='tryon')toInit();}
function vdTab(v){['t2v','i2v'].forEach(function(k){var e=document.getElementById('vd_'+k);if(e)e.style.display=(k===v)?'block':'none';var t=document.getElementById('vdt_'+k);if(t)t.classList.toggle('on',k===v);});}
// 全品类主图
var MN_STYLES=[['clean_marketplace','干净白底'],['premium_catalog','高级目录'],['lifestyle_scene','生活场景'],['studio_light','影棚光感'],['feature_callout','卖点标注']];
var MN_CREAT=[['balanced','平衡'],['creative','创意'],['bold','大胆']];
function mnInit(){var s=document.getElementById('mn_styles');if(s&&!s._b){s._b=1;s.innerHTML=MN_STYLES.map(function(x,i){return '<span class="stylebtn'+(i===0?' on':'')+'" data-v="'+x[0]+'" onclick="mnPick(this,\'mn_styles\')">'+x[1]+'</span>';}).join('');}
 var c=document.getElementById('mn_creatives');if(c&&!c._b){c._b=1;c.innerHTML=MN_CREAT.map(function(x,i){return '<span class="stylebtn'+(i===1?' on':'')+'" data-v="'+x[0]+'" onclick="mnPick(this,\'mn_creatives\')">'+x[1]+'</span>';}).join('');}
 stuThumbs('mn');}
function mnConfirm(){document.getElementById('mn_confirm').style.display='none';document.getElementById('mn_batchbox').style.display='block';document.getElementById('mn_msg').textContent='✓ 已确认首图，可继续批量生成。';}
function mnPick(el,grp){document.querySelectorAll('#'+grp+' .stylebtn').forEach(function(x){x.classList.remove('on');});el.classList.add('on');}
function mnVal(grp){var e=document.querySelector('#'+grp+' .stylebtn.on');return e?e.dataset.v:'';}
async function doMain(mode){var msg=document.getElementById('mn_msg'),st=document.getElementById('mn_studio'),cnt=document.getElementById('mn_cnt');
 var imgs=STU.mn_imgs||[];if(!imgs.length){msg.textContent='请先上传商品图片';return;}
 var style=mnVal('mn_styles')||'clean_marketplace',creative=mnVal('mn_creatives')||'creative';
 var count=+((document.getElementById('mn_num')||{}).value)||4;
 var desc=((document.getElementById('mn_desc')||{}).value||'').trim();
 var b1=document.getElementById('mn_btn');b1.disabled=true;
 msg.textContent='生成 '+count+' 张主图中…（约 20-90 秒/张）';
 st.innerHTML='<div class="iempty"><span class="typing">飞猴正在绘制主图…</span></div>';
 try{var j=await apiP('/api/img/main',{images:imgs,style:style,creative:creative,mode:'batch',count:count,desc:desc,seed:Date.now()&0xffffff});
  if(j.ok){var names=j.images||[];cnt.textContent=(j.styleLabel||'')+' · '+(j.engine||'');msg.textContent='✓ 完成 '+names.length+' 张'+(j.note?'（'+j.note+'）':'');st.innerHTML=imgGrid(names);}
  else{msg.textContent='✗ '+j.error;st.innerHTML='<div class="iempty">✗ '+_esc(j.error)+'</div>';}}
 catch(e){msg.textContent='✗ '+e;}b1.disabled=false;}
// 模特换装
var TO_ACC=[['hat','帽子'],['sunglasses','墨镜'],['scarf','围巾'],['handbag','手提袋'],['backpack','背包'],['shoes','鞋子'],['shirt','衬衫'],['bath_towel','浴巾']];
function toInit(){var c=document.getElementById('to_acc');if(c&&!c._b){c._b=1;c.innerHTML=TO_ACC.map(function(x){return '<span class="chk2" data-v="'+x[0]+'" onclick="this.classList.toggle(\'on\')">'+x[1]+'</span>';}).join('');}}
function toAccSel(){return [].slice.call(document.querySelectorAll('#to_acc .chk2.on')).map(function(x){return x.dataset.v;});}
async function doTryon(mode){var msg=document.getElementById('to_msg'),st=document.getElementById('to_studio'),cnt=document.getElementById('to_cnt');
 var rules={accessoryEnabled:toAccSel().length>0,accessories:toAccSel()};
 var b1=document.getElementById('to_btn'),b2=document.getElementById('to_confirm');
 if(mode==='preview'){if(!STU.to_product||!STU.to_model){msg.textContent='请上传商品图和模特图';return;}
  b1.disabled=true;msg.textContent='生成确认图中…（约 30-60 秒）';st.innerHTML='<div class="iempty"><span class="typing">飞猴正在生成确认图…</span></div>';
  try{var j=await apiP('/api/img/tryon',{mode:'preview',product:STU.to_product,model:STU.to_model,rules:rules});
   if(j.ok){var n=(j.images||[])[0];if(n){STU.to_preview_name=n;st.innerHTML='<div style="margin-bottom:8px;font-size:12px;color:var(--mut)">确认图（满意则点确认生成 4 张）</div>'+imgGrid([n]);b2.style.display='block';cnt.textContent='待确认';msg.textContent='✓ 确认图已生成';}}
   else{msg.textContent='✗ '+j.error;st.innerHTML='<div class="iempty">✗ '+_esc(j.error)+'</div>';}}
  catch(e){msg.textContent='✗ '+e;}b1.disabled=false;
 }else{if(!STU.to_preview_name){msg.textContent='请先生成确认图';return;}
  b2.disabled=true;msg.textContent='生成 4 张展示图中…（约 1-3 分钟）';
  try{var pv=await urlToDataURL('/asset/file?type=media&name='+encodeURIComponent(STU.to_preview_name));
   var j2=await apiP('/api/img/tryon',{mode:'confirm',preview:pv,product:STU.to_product,model:STU.to_model,rules:rules,desc:((document.getElementById('to_desc')||{}).value||'').trim(),count:+((document.getElementById('to_num')||{}).value)||4});
   if(j2.ok){var names=j2.images||[];cnt.textContent=(names.length+1)+' 张'+(j2.note?'（'+j2.note+'）':'');msg.textContent='✓ 完成';st.innerHTML=imgGrid([STU.to_preview_name].concat(names));}
   else{msg.textContent='✗ '+j2.error;}}
  catch(e){msg.textContent='✗ '+e;}b2.disabled=false;}}
// 模特换装 · 批量
var TOB_ITEMS=[];
function toMode(m){document.getElementById('to_single').style.display=(m==='single')?'block':'none';document.getElementById('to_batch').style.display=(m==='batch')?'block':'none';
 document.getElementById('tom_single').classList.toggle('on',m==='single');document.getElementById('tom_batch').classList.toggle('on',m==='batch');
 if(m==='batch'){stuThumbs('tob_p');stuThumbs('tob_m');}}
async function doTryonBatch(){var msg=document.getElementById('tob_msg'),res=document.getElementById('tob_results');
 var ps=STU.tob_p_imgs||[],ms=STU.tob_m_imgs||[];if(!ps.length||!ms.length){msg.textContent='请上传商品图和模特图';return;}
 document.getElementById('tob_btn').disabled=true;msg.textContent='批量生成确认图 + 英文标题中…（每项约 30-60 秒，单次最多 12 项）';res.innerHTML='<div class="iempty"><span class="typing">批量生成中…</span></div>';
 try{var j=await apiP('/api/img/tryon-batch',{products:ps,models:ms,matchMode:document.getElementById('tob_match').value,rules:{gender:document.getElementById('tob_gender').value}});
  if(j.ok){TOB_ITEMS=j.items||[];renderTob();var s=j.summary||{};msg.textContent='✓ 完成 '+(s.success||0)+'/'+(s.total||TOB_ITEMS.length)+'，可逐个或一键确认生成展示图';}
  else{msg.textContent='✗ '+j.error;}}
 catch(e){msg.textContent='✗ '+e;}document.getElementById('tob_btn').disabled=false;}
function renderTob(){var res=document.getElementById('tob_results');if(!TOB_ITEMS.length){res.innerHTML='<div class="iempty">暂无结果</div>';return;}
 var mu=function(n){return '/asset/file?type=media&name='+encodeURIComponent(n);};
 res.innerHTML='<div class="alist">'+TOB_ITEMS.map(function(it,i){
  var imgs=(it.images||[]).map(function(n){return '<img src="'+mu(n)+'" style="width:52px;height:52px;object-fit:cover;border-radius:6px;border:1px solid var(--bd);cursor:pointer" onclick="openImg(\''+mu(n)+'\')">';}).join('');
  var st=it.status==='success'?('✅ 完成 '+(it.images||[]).length+' 图'):(it.status==='failed'?('✗ 失败 '+_esc(it.error||'')):'⏳ 待确认');
  return '<div class="arow" style="align-items:flex-start"><div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:700">#'+(i+1)+' · '+st+'</div><div class="hint" style="margin:2px 0">'+_esc((it.title||'').slice(0,90))+'</div><div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:3px">'+imgs+'</div></div>'+(it.status==='waiting_confirm'?'<span class="actbtn" onclick="doTryonConfirmOne('+i+')">确认→4图</span>':'')+'</div>';}).join('')+'</div>';}
async function doTryonConfirmOne(i){var it=TOB_ITEMS[i];if(!it||it.status!=='waiting_confirm')return;var msg=document.getElementById('tob_msg');msg.textContent='#'+(i+1)+' 生成 4 张展示图中…（约 1-3 分钟）';
 try{var pv=await urlToDataURL('/asset/file?type=media&name='+encodeURIComponent(it.preview));
  var j=await apiP('/api/img/tryon',{mode:'confirm',preview:pv,product:STU.tob_p_imgs[it.productIdx],model:STU.tob_m_imgs[it.modelIdx],rules:{}});
  if(j.ok){it.images=[it.preview].concat(j.images||[]);it.status='success';renderTob();msg.textContent='✓ #'+(i+1)+' 完成';}
  else{msg.textContent='✗ '+j.error;}}
 catch(e){msg.textContent='✗ '+e;}}
async function doTryonConfirmAll(){for(var i=0;i<TOB_ITEMS.length;i++){if(TOB_ITEMS[i].status==='waiting_confirm'){await doTryonConfirmOne(i);}}}
async function doTryonExport(){var done=TOB_ITEMS.filter(function(it){return (it.images||[]).length;});if(!done.length){toast('暂无可导出的图片');return;}
 toast('打包中…');var j=await apiP('/api/img/tryon-export',{items:done.map(function(it){return {title:it.title,images:it.images};})});
 if(j.ok){window.open('/asset/file?type=media&name='+encodeURIComponent(j.zip),'_blank');toast('已打包 '+j.count+' 张，开始下载');}else toast(j.error||'导出失败');}
// 抠图改图
async function doEditor(mode){var msg=document.getElementById('ed_msg'),st=document.getElementById('ed_studio'),cnt=document.getElementById('ed_cnt');
 if(mode==='extract'){if(!STU.ed_ref){msg.textContent='请上传参考图';return;}
  document.getElementById('ed_extract').disabled=true;msg.textContent='抠图中…（约 20-60 秒）';
  try{var j=await apiP('/api/img/editor',{mode:'extract',ref:STU.ed_ref});
   if(j.ok){var n=(j.images||[])[0];if(n){var u='/asset/file?type=media&name='+encodeURIComponent(n);STU.ed_pattern=await urlToDataURL(u);document.getElementById('ed_patternz').innerHTML='<img src="'+u+'">';
    st.innerHTML='<div style="font-size:12px;color:var(--mut);margin-bottom:6px">抠图结果（已填入②，可继续贴图）</div>'+imgGrid([n]);msg.textContent='✓ 抠图完成';}}
   else{msg.textContent='✗ '+j.error;}}
  catch(e){msg.textContent='✗ '+e;}document.getElementById('ed_extract').disabled=false;
 }else{if(!STU.ed_pattern||!STU.ed_product){msg.textContent='请准备透明图案和目标商品图';return;}
  document.getElementById('ed_apply').disabled=true;msg.textContent='贴图中…（约 20-60 秒）';
  try{var j3=await apiP('/api/img/editor',{mode:'apply',pattern:STU.ed_pattern,product:STU.ed_product,desc:((document.getElementById('ed_desc')||{}).value||'').trim(),count:+((document.getElementById('ed_num')||{}).value)||1});
   if(j3.ok){var d=j3.detected||{};cnt.textContent='识别：'+(d.labelZh||'')+' '+Math.round((d.confidence||0)*100)+'%';
    st.innerHTML='<div style="font-size:12px;color:var(--mut);margin-bottom:6px">贴图效果（'+(d.labelZh||'')+' · '+_esc(d.suggestedPlacement||'')+'）</div>'+imgGrid(j3.images||[]);msg.textContent='✓ 贴图完成';}
   else{msg.textContent='✗ '+j3.error;}}
  catch(e){msg.textContent='✗ '+e;}document.getElementById('ed_apply').disabled=false;}}
// 详情页
async function doDetail(){var msg=document.getElementById('dt_msg'),st=document.getElementById('dt_studio'),cat=document.getElementById('dt_cat');
 if(!STU.dt_img){msg.textContent='请上传商品图';return;}
 document.getElementById('dt_btn').disabled=true;msg.textContent='识图 + 生成文案中…（约 20-50 秒）';st.innerHTML='<div class="iempty"><span class="typing">AI 正在识图、生成英文详情…</span></div>';
 try{var j=await apiP('/api/img/detail',{image:STU.dt_img,notes:document.getElementById('dt_notes').value.trim()});
  if(j.ok){cat.textContent=(j.categoryZh||'')+' · '+(j.engine||'');msg.textContent='✓ 完成';st.innerHTML=j.html||'';}
  else{msg.textContent='✗ '+j.error;st.innerHTML='<div class="iempty">✗ '+_esc(j.error||'生成失败')+'</div>';}}
 catch(e){msg.textContent='✗ '+e;st.innerHTML='<div class="iempty">✗ '+_esc(''+e)+'</div>';}document.getElementById('dt_btn').disabled=false;}
// 视频
async function doVid(kind){var P=kind,msg=document.getElementById(P+'_msg'),st=document.getElementById(P+'_studio'),cnt=document.getElementById(P+'_cnt'),btn=document.getElementById(P+'_btn');
 var desc,gtype,ratio,ref='',dur=5;
 if(kind==='t2v'){desc=document.getElementById('t2v_desc').value.trim();gtype=document.getElementById('t2v_type').value;ratio=document.getElementById('t2v_ratio').value;dur=document.getElementById('t2v_dur').value;if(!desc){msg.textContent='请填写脚本';return;}}
 else{ref=STU.i2v_img||'';desc=document.getElementById('i2v_desc').value.trim();gtype=document.getElementById('i2v_type').value;ratio=document.getElementById('i2v_ratio').value;dur=document.getElementById('i2v_dur').value;if(!ref){msg.textContent='请上传商品图';return;}}
 btn.disabled=true;msg.textContent='视频生成中…（约 1-3 分钟，请勿关闭）';st.innerHTML='<div class="iempty"><span class="typing">飞猴正在生成视频…</span></div>';
 try{var j=await apiP('/api/video',{desc:desc,gtype:gtype,ratio:ratio,ref_b64:ref,duration:dur});
  if(j.ok&&j.videos&&j.videos.length){cnt.textContent=j.videos.length+' 个 · '+(j.engine||'');msg.textContent='✓ 完成';st.innerHTML=vidGrid(j.videos);}
  else{msg.textContent='✗ '+(j.error||'失败');st.innerHTML='<div class="iempty">✗ '+_esc(j.error||'生成失败')+'</div>';}}
 catch(e){msg.textContent='✗ '+e;}btn.disabled=false;}
// 一键翻译
var TR_LANGS=['英语','印尼语','马来语','泰语','越南语','菲律宾语','西班牙语','葡萄牙语','俄语','日语','德语','法语','意大利语','阿拉伯语'];
var TR_IMGS=[];
function trInit(){var s=document.getElementById('tr_lang');if(s&&!s._b){s._b=1;s.innerHTML=TR_LANGS.map(function(l){return '<option>'+l+'</option>';}).join('');}}
function trCount(){var n=TR_IMGS.filter(function(x){return x.sel;}).length;document.getElementById('tr_cnt').textContent=TR_IMGS.length?(n+' / '+TR_IMGS.length+' 选中'):'';}
function trRenderGrid(){var g=document.getElementById('tr_grid');if(!TR_IMGS.length){g.innerHTML='<div class="iempty">读取链接或上传图片后，在这里勾选要翻译的图片。</div>';trCount();return;}
 g.innerHTML='<div class="istudio-grid">'+TR_IMGS.map(function(im,i){return '<div class="acard" style="width:120px"><img class="im" style="width:120px;height:120px" src="'+im.src+'" referrerpolicy="no-referrer" onclick="openImg(\''+im.src+'\')"><div class="ft"><label style="cursor:pointer;flex:1"><input type="checkbox" class="trchk" '+(im.sel?'checked':'')+' onchange="TR_IMGS['+i+'].sel=this.checked;trCount()" style="accent-color:var(--mint)"> 翻译</label></div></div>';}).join('')+'</div>';trCount();}
async function trRead(){var u=document.getElementById('tr_url').value.trim();if(!u){toast('请粘贴 1688 链接');return;}var msg=document.getElementById('tr_msg');document.getElementById('tr_read').disabled=true;msg.textContent='读取商品图中…（约 10-40 秒）';
 try{var j=await apiP('/api/translate/read',{url:u});if(j.ok){if(j.title)document.getElementById('tr_title').value=j.title;TR_IMGS=(j.imgUrls||[]).map(function(s){return {src:s,sel:true};});trRenderGrid();msg.textContent='✓ 读到 '+TR_IMGS.length+' 张图，标题已填入';}else{msg.textContent='✗ '+j.error;}}
 catch(e){msg.textContent='✗ '+e;}document.getElementById('tr_read').disabled=false;}
async function trUpload(ev){var fs=[].slice.call(ev.target.files);for(var i=0;i<fs.length;i++){var d=await fileToScaledDataURL(fs[i],1536,0.85);TR_IMGS.push({src:d,sel:true});}ev.target.value='';trRenderGrid();}
async function doTranslate(){var msg=document.getElementById('tr_msg'),res=document.getElementById('tr_result');var lang=document.getElementById('tr_lang').value;var title=document.getElementById('tr_title').value.trim();
 var sel=TR_IMGS.filter(function(x){return x.sel;}).map(function(x){return x.src;});
 if(!title&&!sel.length){msg.textContent='请填标题或勾选图片';return;}
 document.getElementById('tr_btn').disabled=true;res.innerHTML='';msg.textContent='翻译中…（图片翻译较慢，每张约 20-60 秒）';
 var html='';
 if(title){try{var jt=await apiP('/api/translate/title',{title:title,lang:lang});if(jt.ok)html+='<div class="card" style="max-width:none;margin:0 0 10px"><div class="hint" style="margin:0 0 4px">翻译标题（'+_esc(lang)+'）</div><div style="font-weight:700">'+_esc(jt.title)+'</div></div>';else html+='<div class="iempty">✗ 标题翻译：'+_esc(jt.error)+'</div>';}catch(e){}}
 if(sel.length){msg.textContent='标题完成，正在翻译 '+sel.length+' 张图片…';try{var ji=await apiP('/api/translate/images',{images:sel,lang:lang});if(ji.ok)html+='<div class="hint" style="margin:6px 0">翻译后图片（'+(ji.images||[]).length+' 张）'+(ji.note?'·'+_esc(ji.note):'')+'</div>'+imgGrid(ji.images||[]);else html+='<div class="iempty">✗ 图片翻译：'+_esc(ji.error)+'</div>';}catch(e){html+='<div class="iempty">✗ '+_esc(''+e)+'</div>';}}
 res.innerHTML=html;msg.textContent='✓ 完成';document.getElementById('tr_btn').disabled=false;}
var ASSETS=null,asTab='images',asPage={images:1,videos:1,reports:1},AS_PP=20;
async function assetsLoad(){const b=document.getElementById('as_body');b.innerHTML='<span class="hint">加载中…</span>';
 try{ASSETS=await apiG('/api/assets');var a=ASSETS;var m=document.getElementById('as_meta');if(m)m.textContent='图片 '+(a.images||[]).length+' · 视频 '+(a.videos||[]).length+' · 报告 '+(a.reports||[]).length;asShow(asTab);}catch(e){b.innerHTML='✗ '+e;}}
function asGo(k,p){asPage[k]=p;asShow(k);}
function _asPager(k,total){var pg=asPage[k]||1,tp=Math.max(1,Math.ceil(total/AS_PP));if(pg>tp){pg=tp;asPage[k]=pg;}if(tp<=1)return '';
 return '<div style="text-align:center;margin:14px 0;font-size:13px;color:var(--mut)"><span class="actbtn" onclick="asGo(\''+k+'\','+Math.max(1,pg-1)+')">‹ 上一页</span> 第 <b>'+pg+'</b> / '+tp+' 页（共 '+total+'）<span class="actbtn" onclick="asGo(\''+k+'\','+Math.min(tp,pg+1)+')">下一页 ›</span></div>';}
function _asSlice(k,arr){var pg=asPage[k]||1;return arr.slice((pg-1)*AS_PP,pg*AS_PP);}
function asShow(k){asTab=k;var a=ASSETS||{};
 var defs=[['images','🎨 图片',(a.images||[]).length],['videos','🎬 视频',(a.videos||[]).length],['reports','📄 报告',(a.reports||[]).length],['ingest','🛒 选品记录',a.ingest?a.ingest.count:0]];
 document.getElementById('as_tabs').innerHTML=defs.map(d=>'<span class="astab'+(d[0]===k?' on':'')+'" onclick="asShow(\''+d[0]+'\')">'+d[1]+' <b>'+d[2]+'</b></span>').join('');
 var b=document.getElementById('as_body'),mu=n=>'/asset/file?type=media&name='+encodeURIComponent(n);
 if(k==='images'){var im=a.images||[];b.innerHTML=im.length?('<div class="istudio-grid">'+_asSlice(k,im).map(x=>'<div class="acard"><img class="im" src="'+mu(x.name)+'" onclick="openImg(\''+mu(x.name)+'\')"><div class="ft"><span title="'+_esc(x.name)+'">'+_esc(x.name)+'</span><a class="actbtn" href="'+mu(x.name)+'" download>下载</a><span class="delx" onclick="delAsset(\'media\',\''+x.name+'\')">×</span></div></div>').join('')+'</div>'+_asPager(k,im.length)):'<div class="iempty">暂无图片，AI 生图成功后在此查看。</div>';}
 else if(k==='videos'){var vd=a.videos||[];b.innerHTML=vd.length?('<div class="istudio-grid">'+_asSlice(k,vd).map(x=>'<div class="acard"><video class="im" src="'+mu(x.name)+'" onclick="openVideo(\''+mu(x.name)+'\')"></video><div class="ft"><span title="'+_esc(x.name)+'">'+_esc(x.name)+'</span><a class="actbtn" href="'+mu(x.name)+'" download>下载</a><span class="delx" onclick="delAsset(\'media\',\''+x.name+'\')">×</span></div></div>').join('')+'</div>'+_asPager(k,vd.length)):'<div class="iempty">暂无视频，AI 视频成功后在此查看。</div>';}
 else if(k==='reports'){var rp=a.reports||[];b.innerHTML=rp.length?('<div class="alist">'+_asSlice(k,rp).map(x=>'<div class="arow"><span class="an" onclick="viewReport(\''+x.name+'\')">📄 '+_esc(x.name)+'</span><span class="hint">'+x.size_kb+'KB</span><span class="delx" onclick="delAsset(\'report\',\''+x.name+'\')">删除</span></div>').join('')+'</div>'+_asPager(k,rp.length)):'<div class="iempty">暂无分析报告。</div>';}
 else{b.innerHTML=a.ingest?'<div class="alist"><div class="arow"><span class="an">🛒 最近扩展采集</span><span class="hint">'+a.ingest.count+' 个商品</span></div></div>':'<div class="iempty">暂无选品记录。</div>';}}
function delAsset(t,n){if(!confirm('确定删除 '+n+' ？'))return;apiP('/api/asset/delete',{type:t,name:n}).then(j=>{if(j.ok)assetsLoad();else alert(j.error);});}
function el(t,c,h){const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.textContent=h;return e;}
function add(role,text){const m=document.getElementById('msgs');const w=m.querySelector('.wrap');
const row=el('div','row '+(role==='u'?'u':role==='e'?'e':'a'));const box=el('div');
if(role!=='u')box.appendChild(el('div','who',role==='e'?'错误':'飞猴'));
const b=el('div','bub',text);box.appendChild(b);row.appendChild(box);w.appendChild(row);
m.scrollTop=m.scrollHeight;return b;}
function _esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function _inl(s){return _esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>');}
function _tbl(rows){const parse=r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());
const data=rows.filter(r=>!/^\s*\|[\s:|-]+\|\s*$/.test(r)).map(parse);if(!data.length)return '';
let h='<table><thead><tr>'+data[0].map(c=>'<th>'+_inl(c)+'</th>').join('')+'</tr></thead><tbody>';
for(const row of data.slice(1))h+='<tr>'+row.map(c=>'<td>'+_inl(c)+'</td>').join('')+'</tr>';return h+'</tbody></table>';}
function mdToHtml(md){const L=(md||'').split('\n'),o=[];let i=0;
while(i<L.length){const line=L[i];
if(/^\s*\|.*\|\s*$/.test(line)){const t=[];while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i])){t.push(L[i]);i++;}o.push(_tbl(t));continue;}
if(/^\s*####?\s+/.test(line)){o.push('<h4>'+_inl(line.replace(/^\s*####?\s+/,''))+'</h4>');i++;continue;}
if(/^\s*##\s+/.test(line)){o.push('<h3>'+_inl(line.replace(/^\s*##\s+/,''))+'</h3>');i++;continue;}
if(/^\s*#\s+/.test(line)){o.push('<h3>'+_inl(line.replace(/^\s*#\s+/,''))+'</h3>');i++;continue;}
if(/^\s*---\s*$/.test(line)){o.push('<hr>');i++;continue;}
if(/^\s*[-*]\s+/.test(line)){const it=[];while(i<L.length&&/^\s*[-*]\s+/.test(L[i])){it.push('<li>'+_inl(L[i].replace(/^\s*[-*]\s+/,''))+'</li>');i++;}o.push('<ul>'+it.join('')+'</ul>');continue;}
if(line.trim()===''){i++;continue;}
o.push('<p>'+_inl(line)+'</p>');i++;}
return o.join('');}
var chatHist=[];
// ── 对话历史（本地存储，多会话查看/切换）──
var FH_SESS=[];try{FH_SESS=JSON.parse(localStorage.getItem('fh_sessions')||'[]');}catch(e){FH_SESS=[];}
var curSid='';
function _saveSess(){try{localStorage.setItem('fh_sessions',JSON.stringify(FH_SESS.slice(-40)));}catch(e){}}
function saveSession(){if(!chatHist.length)return;
 var title=((chatHist.find(function(m){return m.role==='user';})||{}).content||'对话').slice(0,28);
 if(!curSid)curSid='s'+Math.random().toString(36).slice(2,9);
 var obj={id:curSid,title:title,hist:chatHist.slice(-80),ts:Date.now()};
 var idx=FH_SESS.findIndex(function(x){return x.id===curSid;});
 if(idx>=0)FH_SESS[idx]=obj;else FH_SESS.push(obj);_saveSess();
 var t=document.getElementById('chat_title');if(t)t.textContent=title;renderSidebar();}
var _CHAT_WELCOME='<div class="row a"><div><div class="who">飞猴</div><div class="bub">你好，我是「飞猴」跨境电商智能体 👋 新对话开始，直接提问吧。</div></div></div>';
// 历史对话常驻左侧栏（类 ChatGPT）
function renderSidebar(){var box=document.getElementById('chatlist');if(!box)return;
 var s=FH_SESS.slice().reverse();
 box.innerHTML=s.length?s.map(function(o){return '<div class="sitem'+(o.id===curSid?' on':'')+'" onclick="loadSession(\''+o.id+'\')"><span class="t">💬 '+_esc(o.title||'对话')+'</span><span class="x" title="删除" onclick="event.stopPropagation();delSession(\''+o.id+'\')">×</span></div>';}).join(''):'<div class="hint" style="padding:8px 12px">暂无历史对话</div>';}
function newChat(){saveSession();curSid='';chatHist=[];
 document.querySelector('#msgs .wrap').innerHTML=_CHAT_WELCOME;
 var t=document.getElementById('chat_title');if(t)t.textContent='';document.getElementById('msgs').scrollTop=0;renderSidebar();}
function loadSession(id){saveSession();var o=FH_SESS.find(function(x){return x.id===id;});if(!o)return;
 curSid=id;chatHist=(o.hist||[]).slice();
 var w=document.querySelector('#msgs .wrap');w.innerHTML='';
 chatHist.forEach(function(m){var b=add(m.role==='user'?'u':'a','');if(m.role==='user')b.textContent=m.content;else b.innerHTML=mdToHtml(m.content||'');});
 var t=document.getElementById('chat_title');if(t)t.textContent=o.title;renderSidebar();document.getElementById('msgs').scrollTop=1e9;}
function delSession(id){FH_SESS=FH_SESS.filter(function(x){return x.id!==id;});_saveSess();
 if(id===curSid){curSid='';chatHist=[];document.querySelector('#msgs .wrap').innerHTML=_CHAT_WELCOME;var t=document.getElementById('chat_title');if(t)t.textContent='';}
 renderSidebar();}
function showHistory(){renderSidebar();}  // 兼容旧调用：历史已常驻左栏
// ── 语音输入（浏览器原生语音转文字）──
var ATT=[],REC=null,RECON=false;
function toggleMic(){var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
 if(!SR){toast('此浏览器不支持语音输入，建议用 Chrome / Edge');return;}
 if(RECON){try{REC.stop();}catch(e){}return;}
 REC=new SR();REC.lang='zh-CN';REC.interimResults=true;REC.continuous=true;
 var base=document.getElementById('inp').value;
 REC.onresult=function(ev){var s='';for(var i=ev.resultIndex;i<ev.results.length;i++)s+=ev.results[i][0].transcript;var inp=document.getElementById('inp');inp.value=(base?base+' ':'')+s;autg(inp);};
 REC.onend=function(){RECON=false;document.getElementById('micbtn').classList.remove('on');};
 REC.onerror=function(e){RECON=false;document.getElementById('micbtn').classList.remove('on');if(e&&e.error==='not-allowed')toast('请允许麦克风权限');};
 try{REC.start();RECON=true;document.getElementById('micbtn').classList.add('on');toast('说话中…再点 🎤 停止');}catch(e){}}
// ── 上传图片 / 文件 ──
async function chatAttach(ev){var fs=[].slice.call(ev.target.files);
 for(var i=0;i<fs.length;i++){var f=fs[i];
  if(f.type.indexOf('image')===0){try{var d=await fileToScaledDataURL(f,1280,0.85);ATT.push({kind:'image',name:f.name,dataUrl:d});}catch(e){}}
  else if(/\.(txt|csv|md|json)$/i.test(f.name)||f.type.indexOf('text')===0){try{var tx=await f.text();ATT.push({kind:'text',name:f.name,text:tx.slice(0,8000)});}catch(e){}}
  else{ATT.push({kind:'file',name:f.name});}}
 ev.target.value='';renderAtt();}
function renderAtt(){var r=document.getElementById('attach_row');if(!r)return;
 r.innerHTML=ATT.map(function(a,i){return '<div class="attchip">'+(a.kind==='image'?'<img src="'+a.dataUrl+'">':'📄')+'<span class="nm">'+_esc(a.name)+'</span><span class="rm" onclick="ATT.splice('+i+',1);renderAtt()">×</span></div>';}).join('');}
async function send(){const i=document.getElementById('inp');let t=i.value.trim();
 var imgs=ATT.filter(function(a){return a.kind==='image';}).map(function(a){return a.dataUrl;});
 var texts=ATT.filter(function(a){return a.kind==='text';});
 var files=ATT.filter(function(a){return a.kind==='file';}).map(function(a){return a.name;});
 if(!t&&!imgs.length&&!texts.length&&!files.length)return;
 if(RECON){try{REC.stop();}catch(e){}}
 var disp=t;if(texts.length||files.length)disp+=(disp?'\n':'')+'📎 '+texts.concat(files.map(function(n){return {name:n};})).map(function(x){return x.name;}).join('、');
 var payloadMsg=t;texts.forEach(function(x){payloadMsg+='\n\n【附件 '+x.name+'】\n'+x.text;});
 if(files.length)payloadMsg+='\n\n（附件：'+files.join('、')+'，暂仅记录文件名）';
 i.value='';autg(i);ATT=[];renderAtt();
 add('u',disp+(imgs.length?'  🖼×'+imgs.length:''));
 const s=document.getElementById('snd');s.disabled=true;
 const b=add('a','');b.innerHTML='<span class="typing">飞猴思考中…</span>';
 chatHist.push({role:'user',content:disp});let reply='';var planP=null,act='chat';
 try{var j;
  if(imgs.length){j=await fetch('/api/chat/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:payloadMsg,images:imgs})}).then(function(r){return r.json();});}
  else{planP=apiP('/api/plan',{message:payloadMsg,history:chatHist.slice(-6)}).catch(function(){return null;});
   j=await fetch('/api/agent/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:payloadMsg,history:chatHist.slice(-6)})}).then(function(r){return r.json();});}
  act=(j&&j.action)||'chat';
  if(j.ok){reply=j.reply||'(空回复)';b.innerHTML=(act!=='chat'?'<span class="acttag">⚡ 已执行</span> ':'')+mdToHtml(reply);}else{reply=j.reply||j.error||'请求失败';b.innerHTML=(act!=='chat'?'<span class="acttag bad">执行失败</span> ':'')+mdToHtml(reply);if(!reply){b.parentElement.parentElement.className='row e';b.textContent='请求失败';}}}
 catch(e){b.parentElement.parentElement.className='row e';b.textContent='网络错误：'+e;}
 if(reply)chatHist.push({role:'assistant',content:reply});
 saveSession();
 if(act==='auto_pipeline'&&j&&j.job){showCollectConfirm(j.job);}
 if(act&&act!=='chat'&&act!=='auto_pipeline'){var TT={'box.count':'box','box.list':'box','box.delete_chinese':'box','box.delete_all':'box','box.translate':'box','box.list_tiktok':'box','pipeline':'box','analyze':'analysis','image.generate':'image'};var tt=TT[act];if(tt){setTimeout(function(){tab(tt);if(tt==='box'&&typeof boxLoad==='function')boxLoad(1);},700);}}
 if(planP&&act==='chat'){try{const plan=await planP;if(plan&&plan.ok)renderPlan(b.parentElement,plan);}catch(e){}}
 s.disabled=false;document.getElementById('msgs').scrollTop=1e9;}
// 全自动采集前的确认弹窗（可改功能开关，10 秒倒计时自动开始）
var CC_JOB=null,CC_TIMER=null;
function showCollectConfirm(job){
 if(CC_TIMER){clearInterval(CC_TIMER);CC_TIMER=null;}
 CC_JOB=job||{};
 var ck=function(id,on,lb){return '<label style="display:flex;align-items:center;gap:7px;margin:6px 0;font-size:13.5px"><input type="checkbox" id="'+id+'"'+(on?' checked':'')+'> '+lb+'</label>';};
 var h='<div style="padding-right:24px"><h3 style="margin:0 0 4px">🚀 确认采集设置</h3>'
  +'<div class="hint" style="margin:0 0 8px">关键词 '+((job.keywords||[]).length)+' 个 · 每词 '+(job.perKw||10)+' 个'+(job.topN?(' · 仅保留评分最高 '+job.topN+' 个'):'')+' · 语言 '+_esc(job.lang||'英语')+'</div>'
  +ck('cc_score',job.score!==false,'AI 评分')
  +ck('cc_translate',!!job.translate,'翻译标题')
  +ck('cc_transimg',!!job.transImages,'翻译图片')
  +ck('cc_optimize',job.optimize!==false,'自动优化标题/图片（删违禁词+剔除工厂/低质图）')
  +ck('cc_listtk',!!job.listTiktok,'采集后自动上架 TikTok')
  +ck('cc_tkauto',!!job.tkAuto,'直接发布（关=认领预填后停）')
  +ck('cc_oneclick',!!job.oneClick,'一键采集（关=逐个打开商品演示）')
  +'<div style="display:flex;gap:10px;margin-top:14px;align-items:center"><button class="btn" style="margin:0" onclick="ccStart()">立即开始</button><span class="actbtn" onclick="ccCancel()">取消</span><span class="hint" style="margin:0">将在 <b id="cc_cd">10</b> 秒后自动开始</span></div></div>';
 openHtml(h,true);
 var n=10;
 CC_TIMER=setInterval(function(){var e=document.getElementById('cc_cd');if(!e){clearInterval(CC_TIMER);CC_TIMER=null;return;}n--;e.textContent=n;if(n<=0){clearInterval(CC_TIMER);CC_TIMER=null;ccStart();}},1000);
}
function ccCancel(){if(CC_TIMER){clearInterval(CC_TIMER);CC_TIMER=null;}closeModal(null,true);toast('已取消采集');}
async function ccStart(){if(CC_TIMER){clearInterval(CC_TIMER);CC_TIMER=null;}
 var g=function(id){var e=document.getElementById(id);return e?e.checked:false;};
 var job=Object.assign({},CC_JOB,{score:g('cc_score'),translate:g('cc_translate'),transImages:g('cc_transimg'),optimize:g('cc_optimize'),listTiktok:g('cc_listtk'),tkAuto:g('cc_tkauto'),oneClick:g('cc_oneclick'),fast:true});
 closeModal(null,true);
 try{await apiP('/api/collect-job/create',{opts:job});add('a','').innerHTML='<span class="acttag">⚡ 已下发</span> 采集任务已下发——请保持「采集插件」侧边栏打开，它会在几秒内自动接到任务并执行：采集→评分→优化→翻译→上架。进度在插件里实时显示。';}
 catch(e){var b=add('a','');b.parentElement.parentElement.className='row e';b.textContent='下发失败：'+e;}
 document.getElementById('msgs').scrollTop=1e9;}
var INTENT_LABELS={small_talk:'闲聊',identity_intro:'身份介绍',capability_intro:'能力介绍',product_material_plan:'素材生成规划',plan:'素材生成规划',image_generation_advice:'图片生成建议',listing_optimization:'Listing优化',workflow_execution:'工作流执行',troubleshooting:'故障排查',system_usage:'系统使用',strategy_consulting:'运营策略咨询',clarify:'信息补充',unknown:'通用问答'};
function renderPlan(box,plan){const tasks=plan.tasks||[],cq=plan.clarifyingQuestions||[],sg=plan.suggestions||[];
 const a=plan.analysis||{};const ana=[];if(a.productType)ana.push('商品：'+a.productType);if(a.platform)ana.push('平台：'+a.platform);if(a.goal)ana.push('目标：'+a.goal);
 if(!tasks.length&&!(plan.needsClarification&&cq.length)&&!ana.length)return;let h='';
 if(plan.intent||ana.length){h+='<div class="planc" style="padding:8px 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+(plan.intent?'<span class="stepbadge">🎯 '+_esc(INTENT_LABELS[plan.intent]||plan.intent)+'</span>':'')+(ana.length?'<span style="font-size:12px;color:var(--mut)">'+ana.map(_esc).join('　｜　')+'</span>':'')+'</div>';}
 if(plan.needsClarification&&cq.length&&!tasks.length){h+='<div class="planc"><div class="planh">🤔 先确认几点（点一下填入输入框）</div>'+cq.map(function(q){return '<div class="planq" onclick="askFill(this)">'+_esc(q)+'</div>';}).join('')+'</div>';}
 if(tasks.length){h+='<div class="planc"><div class="planh">🧭 建议执行流程 · '+tasks.length+' 步</div>'+tasks.map(function(tk,i){return '<div class="plant"><div class="pnum">'+(i+1)+'</div><div class="pmain"><div class="ptitle">'+tk.emoji+' '+_esc(tk.title)+' <span class="pprio '+tk.priority+'">'+tk.priority+'</span></div>'+(tk.reason?'<div class="preason">'+_esc(tk.reason)+'</div>':'')+'</div><button class="actbtn" onclick="tab(\''+tk.tab+'\')">去处理 →</button></div>';}).join('')+'</div>';}
 if(sg.length){h+='<div class="plantip">💡 '+sg.map(_esc).join('　·　')+'</div>';}
 const d=document.createElement('div');d.className='planwrap';d.innerHTML=h;box.appendChild(d);document.getElementById('msgs').scrollTop=1e9;}
function askFill(el){const i=document.getElementById('inp');i.value=el.textContent.trim();i.focus();autg(i);}
var TC_ST={pending:['待执行','#fef3c7','#b45309'],in_progress:['执行中','#dbeafe','#1d4ed8'],completed:['已完成','#dcfce7','#15803d'],cancelled:['已取消','#f1f5f9','#64748b']};
async function openTaskCenter(){var j=await apiG('/api/agent/tasks');var s=j.stats||{},ts=j.tasks||[];
 var done=s.completed||0,total=s.total||0,pct=total?Math.round(done/total*100):0;
 var h='<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;padding-right:26px;flex-wrap:wrap"><h3 style="margin:0;flex:1">📋 任务中心</h3>'
  +'<span class="stepbadge" style="background:#fef3c7;color:#b45309">待办 '+(s.pending||0)+'</span><span class="stepbadge" style="background:#dbeafe;color:#1d4ed8">进行 '+(s.in_progress||0)+'</span><span class="stepbadge" style="background:#dcfce7;color:#15803d">完成 '+done+'</span><span class="actbtn" onclick="openTaskCenter()">↻</span></div>';
 if(total)h+='<div style="font-size:11px;color:var(--mut);margin-bottom:4px">工作流进度 '+done+'/'+total+' · '+pct+'%</div><div style="background:#eef2f7;border-radius:8px;height:8px;margin-bottom:12px"><div style="height:8px;border-radius:8px;background:var(--mint);width:'+pct+'%"></div></div>';
 h+=ts.length?('<div class="alist">'+ts.map(taskRow).join('')+'</div>'):'<div class="hint">暂无任务。在「对话」里说出需求（如：给蓝牙耳机做一套TikTok素材），任务会自动入库。</div>';
 openHtml(h,true);}
function taskRow(t){var sc=TC_ST[t.status]||['','#eee','#333'];
 var btns='';if(t.status==='pending')btns='<span class="actbtn" onclick="taskAct(\''+t.id+'\',\'in_progress\')">开始</span><span class="actbtn" style="color:#dc2626" onclick="taskAct(\''+t.id+'\',\'cancelled\')">取消</span>';
 else if(t.status==='in_progress')btns='<span class="actbtn" onclick="taskBump(\''+t.id+'\')">+10%</span><span class="actbtn" onclick="taskAct(\''+t.id+'\',\'completed\')">完成</span>';
 var jump=t.tab?'<span class="actbtn" onclick="closeModal(null,true);tab(\''+t.tab+'\')">去做→</span>':'';
 return '<div class="arow"><div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">'+(t.emoji||'')+' '+_esc(t.title||'')+' <span class="stepbadge" style="background:'+sc[1]+';color:'+sc[2]+'">'+sc[0]+'</span></div>'
  +'<div class="hint" style="margin:2px 0 0">第 '+t.stepIndex+'/'+t.totalSteps+' 步'+(t.progress?(' · '+t.progress+'%'):'')+(t.reason?(' · '+_esc(t.reason)):'')+'</div></div>'+jump+btns+'</div>';}
async function taskAct(id,status){var j=await apiP('/api/agent/task',{id:id,status:status});if(!j.ok){toast(j.error||'更新失败');return;}openTaskCenter();}
async function taskBump(id){var j0=await apiG('/api/agent/tasks');var t=(j0.tasks||[]).filter(function(x){return x.id===id;})[0];var p=Math.min(100,((t&&t.progress)||10)+10);var j=await apiP('/api/agent/task',{id:id,progress:p});if(j.ok)openTaskCenter();else toast(j.error||'更新失败');}
function autg(t){t.style.height='auto';t.style.height=Math.min(t.scrollHeight,140)+'px';}
async function refresh(){try{const j=await(await fetch('/api/status')).json();
const ok=j.gateway==='active';
document.getElementById('gw').textContent=j.gateway;document.getElementById('md').textContent=j.model;
document.getElementById('gdot').className='dot '+(ok?'ok':'bad');
document.getElementById('s_gw').textContent=j.gateway;document.getElementById('s_md').textContent=j.model;
document.getElementById('s_ag').textContent=j.agent;}catch(e){}}
async function saveModel(){const m=document.getElementById('mmsg');m.style.display='block';m.textContent='提交中…';
const body={base_url:s_url.value.trim(),api_key:s_key.value,model:s_model.value.trim(),compat:s_compat.value};
try{const j=await(await fetch('/api/model',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(body)})).json();m.className='note '+(j.ok?'ok':'bad');
m.textContent=j.ok?('✓ '+j.msg):('✗ '+j.error);refresh();}catch(e){m.textContent='✗ '+e;}}
async function saveCreds(){const m=document.getElementById('cmsg');m.style.display='block';m.textContent='保存中…';
const body={MIAOSHOU_APP_KEY:c_mkey.value.trim(),MIAOSHOU_APP_SECRET:c_msec.value.trim()};
var oi=document.getElementById('c_ozid'),ok=document.getElementById('c_ozkey');if(oi&&oi.value.trim())body.OZON_CLIENT_ID=oi.value.trim();if(ok&&ok.value.trim())body.OZON_API_KEY=ok.value.trim();
try{const j=await(await fetch('/api/creds',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(body)})).json();m.className='note '+(j.ok?'ok':'bad');
m.textContent=j.ok?('✓ '+j.msg):('✗ '+j.error);}catch(e){m.textContent='✗ '+e;}}
function saveFeishu(){var m=document.getElementById('fsmsg');m.style.display='block';m.className='note';m.textContent='保存中…';
 apiP('/api/feishu/config',{app_id:document.getElementById('fs_id').value.trim(),app_secret:document.getElementById('fs_sec').value.trim(),verify_token:document.getElementById('fs_vt').value.trim()}).then(function(j){m.className='note '+(j.ok?'ok':'bad');m.textContent=(j.ok?'✓ 已保存飞书配置':'✗ '+(j.error||'失败'));}).catch(function(e){m.className='note bad';m.textContent='✗ '+e;});}
function copyHook(){var t=document.getElementById('fs_hook').textContent;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){toast('已复制事件地址');}).catch(function(){toast('请手动复制');});}
// 飞书会话实时同步到网页对话
var FS_LAST=0;
function fsPoll(){apiP('/api/feishu/messages',{since:FS_LAST}).then(function(j){if(!j||!j.ok)return;FS_LAST=j.last||FS_LAST;
 (j.messages||[]).forEach(function(mm){var b=add(mm.role==='user'?'u':'a','');b.innerHTML='<span class="acttag" style="background:#3370ff">飞书</span> '+mdToHtml(mm.text||'');});
}).catch(function(){});}
// ── 盒子激活（中心管控）──
var LIC_ST={active:'✅ 已激活',expired:'⛔ 已到期',disabled:'⛔ 已停用',none:'未激活',invalid:'激活码无效',unused:'未激活',standalone:'独立模式（免激活）'};
function licInit(){apiP('/api/license',{}).then(function(j){if(!j||!j.ok)return;
 var e=function(id){return document.getElementById(id);};
 if(e('lic_bid'))e('lic_bid').textContent=j.box_id||'-';
 if(e('lic_st'))e('lic_st').textContent=LIC_ST[j.status]||j.status||'-';
 if(e('lic_exp'))e('lic_exp').textContent=j.expires_at?('到期 '+j.expires_at+'（剩 '+(j.days_left||0)+' 天）'):'';
 var active=(j.status==='active');var standalone=(j.status==='standalone');
 if(e('lic_form'))e('lic_form').style.display=standalone?'none':'block';
 if(e('lic_title'))e('lic_title').textContent=standalone?'授权状态':(active?'授权状态 · 续费':'盒子激活');
 if(e('lic_btn'))e('lic_btn').textContent=active?'续费':'激活';
 if(e('lic_codelabel'))e('lic_codelabel').textContent=active?'续费激活码（到期前向卖家购买后填入）':'激活码';
 if(e('lic_hint'))e('lic_hint').textContent=standalone?'本盒子为独立模式，无需激活。':(active?'已激活。续费请向卖家购买新激活码后填入并提交。':'激活码由卖家提供。激活后即可使用全部功能。');
 if(e('lic_code'))e('lic_code').value=active?'':(j.code||'');}).catch(function(){});}
licInit();
function doActivate(){var m=document.getElementById('lic_msg');m.style.display='block';m.className='note';m.textContent='提交中…';
 apiP('/api/activate',{code:document.getElementById('lic_code').value.trim()}).then(function(j){m.className='note '+(j.ok?'ok':'bad');
  m.textContent=j.ok?('✓ '+(j.msg||'成功')):('✗ '+(j.error||'失败'));if(j.ok)licInit();})
 .catch(function(e){m.className='note bad';m.textContent='✗ '+e;});}
(function fsInit(){var h=document.getElementById('fs_hook');if(h)h.textContent=location.origin+'/api/feishu/event';
 apiP('/api/feishu/messages',{since:0}).then(function(j){if(j&&j.ok)FS_LAST=j.last||0;}).catch(function(){}).then(function(){setInterval(fsPoll,4000);});})();
(function(){const c=document.getElementById('chips');CHIPS.forEach(x=>{const e=el('div','chip',x);
e.onclick=()=>{const i=document.getElementById('inp');i.value=x.replace(/^[^一-龥A-Za-z0-9]+/,'').trim();i.focus();autg(i);};c.appendChild(e);});
const i=document.getElementById('inp');i.addEventListener('input',()=>autg(i));
i.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
refresh();setInterval(refresh,15000);renderSidebar();})();
</script></body></html>"""


if __name__ == "__main__":
    _START_TS[0] = time.time()
    threading.Thread(target=_reporter_loop, daemon=True).start()
    threading.Thread(target=_tunnel_loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print("飞猴 Web 前端启动: http://%s:%d  (agent=%s, openclaw=%s)" % (BIND, PORT, AGENT, OPENCLAW))
    srv.serve_forever()
