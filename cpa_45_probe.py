"""grok-4.5 权限探测（纯 urllib，无第三方依赖）。

A3 推送过滤：注册 + CPA Device Flow 拿到 token 后，先探测该号是否有
grok-4.5 模型权限，有才推 CPA + grokcli-2api，没有就跳过所有写入。

对齐 nosms `cpa_xai/probe.py` 的 /models 探测 + 重试 + 结果分类：
  - 带 x-grok-model-override: grok-4.5 等 CLI 请求头（对齐 schema.py）
  - proxy → direct 回退
  - 只有 has_grok_45=True 才算通过（硬 gate，fail-closed）
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_MODEL = "grok-4.5"
CLI_VERSION = "0.2.93"
CLIENT_SURFACE = "grok-cli"
CLIENT_IDENTIFIER = "grokcli-2api"

LogFn = Callable[[str], None]

_TRANSPORT_MARKERS = (
    "ssl",
    "eof",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "tunnel connection failed",
    "bad gateway",
    "proxy",
    "urlopen error",
    "remote end closed",
    "10053",
    "10054",
    "syscall",
)


def build_request_headers(*, model: str = DEFAULT_MODEL) -> dict[str, str]:
    """CLI chat-proxy 请求头，对齐 nosms schema.build_request_headers。"""
    return {
        "Authorization": "",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-surface": CLIENT_SURFACE,
        "x-grok-client-identifier": CLIENT_IDENTIFIER,
        "x-grok-model-override": model,
        "User-Agent": f"grok-cli/{CLI_VERSION}",
    }


def _auth_headers(access_token: str, *, model: str = DEFAULT_MODEL) -> dict[str, str]:
    headers = build_request_headers(model=model)
    headers["Authorization"] = f"Bearer {access_token}"
    # /models 返回 JSON 而非 SSE
    headers["Accept"] = "application/json"
    return headers


def _normalize_proxy(proxy: str | None) -> str:
    p = str(proxy or "").strip()
    if not p:
        return ""
    if "://" not in p:
        p = f"http://{p}"
    return p


def _proxy_label(proxy: str | None) -> str:
    return _normalize_proxy(proxy) or "(direct)"


def _opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    """带可选代理的 urllib opener；空代理时用纯直连 opener。"""
    p = _normalize_proxy(proxy)
    if p:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p})
        )
    return urllib.request.build_opener()


def _is_transport_error_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _TRANSPORT_MARKERS)


def classify_probe_result(result: dict[str, Any]) -> str:
    """分类单次 /models 探测结果。

    硬 gate 相关取值：
      - ok
      - models_ok_but_no_grok45  （4.5 真阴性）
      - empty_models
      - http_error
      - transport_error
      - invalid_response
    """
    if result.get("has_grok_45"):
        return "ok"
    if result.get("ok") and result.get("model_ids"):
        return "models_ok_but_no_grok45"
    if result.get("ok") and not result.get("model_ids"):
        return "empty_models"
    status = int(result.get("status") or 0)
    err = str(result.get("error") or "")
    if status > 0:
        return "http_error"
    if _is_transport_error_text(err):
        return "transport_error"
    return "invalid_response"


def _probe_models_once(
    access_token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    proxy: str | None = None,
    proxy_mode: str = "direct",
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/models"
    headers = _auth_headers(access_token)
    opener = _opener(proxy)
    proxy_label = _proxy_label(proxy)
    req = urllib.request.Request(url, headers=headers, method="GET")
    t0 = time.time()

    def _out(**extra) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "has_grok_45": False,
            "model_ids": [],
            "status": 0,
            "error": "",
            "proxy_mode": proxy_mode,
            "proxy": proxy_label,
            "latency_ms": int((time.time() - t0) * 1000),
        }
        out.update(extra)
        out["reason"] = classify_probe_result(out)
        return out

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            latency_ms = int((time.time() - t0) * 1000)
            try:
                body = json.loads(raw) if raw else {}
            except Exception as exc:  # noqa: BLE001
                return _out(
                    status=getattr(resp, "status", 200),
                    error=f"invalid JSON: {exc}",
                    latency_ms=latency_ms,
                )
            if not isinstance(body, dict):
                return _out(
                    status=getattr(resp, "status", 200),
                    error=f"unexpected body type: {type(body).__name__}",
                    latency_ms=latency_ms,
                )
            ids = [x.get("id") for x in body.get("data") or [] if isinstance(x, dict)]
            out = {
                "ok": True,
                "has_grok_45": any(i == DEFAULT_MODEL for i in ids),
                "model_ids": ids,
                "status": getattr(resp, "status", 200),
                "error": "",
                "proxy_mode": proxy_mode,
                "proxy": proxy_label,
                "latency_ms": latency_ms,
            }
            out["reason"] = classify_probe_result(out)
            return out
    except urllib.error.HTTPError as e:
        return _out(
            status=e.code,
            error=e.read().decode("utf-8", errors="replace")[:500],
        )
    except Exception as e:  # noqa: BLE001
        return _out(status=0, error=str(e))


def probe_grok_45(
    access_token: str,
    *,
    base_url: str | None = None,
    proxy: str | None = None,
    retries: int = 3,
    timeout: float = 30.0,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """探测 access_token 是否有 grok-4.5 模型权限。

    返回 {ok, has_grok_45, model_ids, reason, status, error, latency_ms, ...}。
    硬 gate：只有 has_grok_45=True 才算可用；重试只减少传输/空响应误报。
    """
    access_token = str(access_token or "").strip()
    if not access_token:
        return {
            "ok": False,
            "has_grok_45": False,
            "model_ids": [],
            "reason": "invalid_response",
            "status": 0,
            "error": "missing access_token",
            "proxy_mode": "none",
            "proxy": "(none)",
            "latency_ms": None,
            "attempt_count": 1,
            "elapsed_ms": 0,
        }
    retries = max(1, int(retries or 1))
    base = str(base_url or DEFAULT_BASE_URL).rstrip("/")

    resolved = _normalize_proxy(proxy)
    modes: list[tuple[str, str | None]] = []
    if resolved:
        modes.append(("proxy", resolved))
    # 始终保留直连尝试，避免单一代理出口坏掉导致误判
    modes.append(("direct", None))

    rank = {
        "ok": 5,
        "models_ok_but_no_grok45": 4,
        "empty_models": 3,
        "http_error": 2,
        "invalid_response": 1,
        "transport_error": 0,
    }

    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    started_at = time.time()

    def _better(a: dict[str, Any] | None, b: dict[str, Any]) -> dict[str, Any]:
        if a is None:
            return b
        ra = rank.get(str(a.get("reason") or ""), -1)
        rb = rank.get(str(b.get("reason") or ""), -1)
        if rb > ra:
            return b
        if rb == ra and len(b.get("model_ids") or []) > len(a.get("model_ids") or []):
            return b
        return a

    for attempt in range(1, retries + 1):
        for mode, mode_proxy in modes:
            one = _probe_models_once(
                access_token,
                base_url=base,
                timeout=timeout,
                proxy=mode_proxy,
                proxy_mode=mode,
            )
            one["attempt"] = attempt
            attempts.append(one)
            if log is not None:
                log(
                    "probe try "
                    f"attempt={attempt}/{retries} mode={mode} "
                    f"reason={one.get('reason')} status={one.get('status')} "
                    f"has_grok_45={one.get('has_grok_45')} ids={one.get('model_ids')} "
                    f"latency_ms={one.get('latency_ms')} "
                    f"error={str(one.get('error') or '')[:120]}"
                )
            best = _better(best, one)
            if one.get("has_grok_45"):
                out = dict(one)
                out["ok"] = True
                out["attempt_count"] = len(attempts)
                out["elapsed_ms"] = int((time.time() - started_at) * 1000)
                return out

    final = dict(best or {
        "ok": False,
        "has_grok_45": False,
        "model_ids": [],
        "reason": "invalid_response",
        "status": 0,
        "error": "probe produced no attempts",
        "proxy_mode": "none",
        "proxy": "(none)",
        "latency_ms": None,
    })
    final["attempt_count"] = len(attempts)
    final["elapsed_ms"] = int((time.time() - started_at) * 1000)
    final["has_grok_45"] = bool(final.get("has_grok_45"))
    if not final.get("has_grok_45"):
        final["ok"] = False
    return final
