"""妙手（Miaoshou）CLI 客户端封装。

把旧 zhuiwen_web.py 散落的 SELECT_CMD 调用收成一个 client。妙手 = 第三方 1688 采集 +
采集箱 + TikTok 上架 SaaS，经 select.py（开发）或编译二进制 zhuiwen-select（盒子）CLI
调用；凭证由本进程注入子进程环境（MIAOSHOU_APP_KEY/SECRET）。

mode 全集（反推自旧调用点，见 docs/sourcing-client-migration.md ADR-002）：
url / save / box / detail / edit / delete / images / shops / tkcall。
本 client 封装后处理用到的原语；tk_list_items 的「认领→上架」编排是更上层逻辑（T3.2），
用这些原语拼。

失败（非零退出 / 超时 / 解析失败）一律抛 MiaoshouError，供 post_process task 捕获标 failed。
参数构造严格对齐旧 zhuiwen_web 调用点，不臆造 CLI flag。
"""
import json
import os
import subprocess
from typing import Any, Callable

from app.core.config import get_settings

# runner 签名：(cmd, timeout, env) -> (returncode, stdout, stderr)
Runner = Callable[[list[str], int, dict[str, str]], "tuple[int, str, str]"]


class MiaoshouError(RuntimeError):
    """妙手调用失败（非零退出 / 超时 / 解析失败）。"""


def _default_cmd() -> list[str]:
    s = get_settings()
    binary = os.path.expanduser(s.miaoshou_select_bin)
    if os.path.exists(binary):
        return [binary]
    return ["python3", os.path.expanduser(s.miaoshou_select_py)]


def _default_env() -> dict[str, str]:
    s = get_settings()
    env = dict(os.environ)
    if s.miaoshou_app_key:
        env["MIAOSHOU_APP_KEY"] = s.miaoshou_app_key
    if s.miaoshou_app_secret:
        env["MIAOSHOU_APP_SECRET"] = s.miaoshou_app_secret
    return env


def _default_runner(cmd: list[str], timeout: int, env: dict[str, str]) -> "tuple[int, str, str]":
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时"
    except Exception as e:  # noqa: BLE001 —— 子进程任何异常都归一为结构化错误
        return 1, "", str(e)


class MiaoshouClient:
    def __init__(
        self,
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
        runner: Runner | None = None,
        timeout: int = 180,
    ) -> None:
        self._cmd = list(cmd) if cmd is not None else _default_cmd()
        self._env = env if env is not None else _default_env()
        self._run = runner or _default_runner
        self._timeout = timeout

    def _call(self, args: list[str], *, timeout: int | None = None) -> str:
        code, out, err = self._run(self._cmd + args, timeout or self._timeout, self._env)
        if code != 0:
            lines = (err or out or "妙手调用失败").strip().splitlines()
            raise MiaoshouError(lines[-1] if lines else "妙手调用失败")
        return out

    @staticmethod
    def _parse(out: str, default: str) -> Any:
        try:
            return json.loads(out.strip() or default)
        except json.JSONDecodeError as e:
            raise MiaoshouError(f"妙手返回解析失败: {e}") from e

    # ── 1688 采集 ─────────────────────────────────────────────
    def url_fetch(self, urls: list[str], limit: int | None = None) -> list[dict]:
        """妙手 fetch offer URL → 商品详情列表（mode url）。妙手自抓，过它自己的风控。"""
        urls = [u for u in urls if u]
        if not urls:
            raise MiaoshouError("无有效 URL")
        args = ["--mode", "url", "--limit", str(limit or len(urls)), "--urls", *urls]
        return self._parse(self._call(args, timeout=520), "[]")

    # ── 采集箱 ────────────────────────────────────────────────
    def box(self, page: int = 1, limit: int = 20) -> list[dict]:
        args = ["--mode", "box", "--limit", str(limit), "--page", str(page), "--format", "json"]
        return self._parse(self._call(args, timeout=60), "[]")

    def detail(self, item_id: str) -> dict:
        return self._parse(self._call(["--mode", "detail", "--id", str(item_id)], timeout=40), "{}")

    def edit(self, item_id: str, changes: dict) -> dict:
        """改箱内条目（标题/图回写，翻译/优化用）。mode edit --id --changes json。"""
        out = self._call(
            ["--mode", "edit", "--id", str(item_id),
             "--changes", json.dumps(changes or {}, ensure_ascii=False)],
            timeout=60,
        )
        return self._parse(out, '{"ok": true}')

    def delete(self, ids: list[str]) -> dict:
        ids = [str(i) for i in ids if str(i).strip()]
        if not ids:
            return {"deleted": 0}
        return self._parse(self._call(["--mode", "delete", "--ids", *ids], timeout=60), "{}")

    # ── TikTok 店铺 / 上架原语 ─────────────────────────────────
    def shops(self) -> list[dict]:
        """列已绑定 TikTok 店铺。"""
        return self._parse(self._call(["--mode", "shops"], timeout=40), "[]")

    def tkcall(self, endpoint: str, body: dict) -> dict:
        """妙手 TikTok 采集箱接口代理（统一签名）。上架编排用它拼（T3.2）。"""
        out = self._call(
            ["--mode", "tkcall", "--tk", endpoint,
             "--body", json.dumps(body or {}, ensure_ascii=False)],
            timeout=70,
        )
        return self._parse(out, "{}")
