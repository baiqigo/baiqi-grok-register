# -*- coding: utf-8 -*-
"""Outlook / Hotmail OTP reader for grok-register-panel (ported from nosms).

MSA tokens exported as (outlook.office.com / IMAP audience):

  email----password----refresh_token----client_id

Primary receive path:
  1) plus-alias (base+g<hex>@domain) reuse of a pool base mailbox
  2) IMAP XOAUTH2 (primary)
  3) optional Office REST (off by default)
  4) Graph Mail.Read only for real Graph JWTs

Pure stdlib + function-body requests/curl_cffi, single file, IMAP+OAuth2
(not the HTTP-injection template of the other providers). Config-driven,
matching nosms's outlook_mail.py with a panel-facing dispatcher signature.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import json
import os
import re
import secrets
import threading
import time
import warnings
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover
    cffi_requests = None

try:
    import requests as std_requests
except Exception:  # pragma: no cover
    std_requests = None


LogFn = Optional[Callable[[str], None]]
CancelFn = Optional[Callable[[], bool]]

DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# Remote IMAP bridge (Daytona sandbox: 443-only egress, outlook IMAP 993 is blocked).
# When configured, fetch_messages_imap / _purge_mailbox POST to the bridge over HTTPS.
_IMAP_HTTP_URL: str = ""
_IMAP_HTTP_TOKEN: str = ""


def configure_imap_http_bridge(url: str = "", token: str = "") -> None:
    """Set the remote IMAP bridge endpoint (e.g. https://host/imap). Empty = local IMAP."""
    global _IMAP_HTTP_URL, _IMAP_HTTP_TOKEN
    _IMAP_HTTP_URL = str(url or "").strip().rstrip("/")
    _IMAP_HTTP_TOKEN = str(token or "").strip()
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993

# Config files resolve relative to the panel project ROOT, not this file's dir.
ROOT = Path(__file__).resolve().parent.parent


def _project_path(rel: str) -> Path:
    """Resolve a pool/used/dead path; absolute paths pass through untouched."""
    rel = str(rel or "").strip()
    p = Path(rel)
    if p.is_absolute():
        return p
    return ROOT / rel


def _cfg_true(config: dict, key: str, default: bool = True) -> bool:
    """Read a boolean config toggle tolerant of both native bool and webui-saved strings."""
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _env_floats(name: str, default: List[float]) -> List[float]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        vals = [float(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        return default
    return vals or default


# Connection-level retry for the flaky direct outlook.office365.com:993 link
# (intermittent WinError 10060 timeout). Overridable via env:
#   IMAP_CONNECT_RETRIES=N  /  IMAP_CONNECT_RETRY_DELAYS=2,4
IMAP_CONNECT_RETRIES = _env_int("IMAP_CONNECT_RETRIES", 3)
IMAP_CONNECT_RETRY_DELAYS = _env_floats("IMAP_CONNECT_RETRY_DELAYS", [2.0, 4.0])

IMAP_TOKEN_ENDPOINTS = [
    {"name": "LIVE+scope", "url": "https://login.live.com/oauth20_token.srf", "scope": IMAP_SCOPE},
    {
        "name": "V1-COMMON",
        "url": "https://login.microsoftonline.com/common/oauth2/token",
        "scope": "",
        "resource": "https://outlook.office.com/",
    },
    {
        "name": "V1-CONSUMERS",
        "url": "https://login.microsoftonline.com/consumers/oauth2/token",
        "scope": "",
        "resource": "https://outlook.office.com/",
    },
    {
        "name": "CONSUMERS",
        "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "scope": IMAP_SCOPE,
    },
    {
        "name": "COMMON",
        "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": IMAP_SCOPE,
    },
    {"name": "LIVE", "url": "https://login.live.com/oauth20_token.srf", "scope": ""},
    {
        "name": "CONSUMERS-noscope",
        "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "scope": "",
    },
    {
        "name": "COMMON-noscope",
        "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "",
    },
]

GRAPH_TOKEN_ENDPOINTS = [
    {
        "name": "CONSUMERS-Graph",
        "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "scope": GRAPH_SCOPE,
    },
    {
        "name": "COMMON-Graph",
        "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": GRAPH_SCOPE,
    },
    {
        "name": "CONSUMERS-Graph-default",
        "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "scope": "https://graph.microsoft.com/.default offline_access",
    },
    {
        "name": "COMMON-Graph-default",
        "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "https://graph.microsoft.com/.default offline_access",
    },
]

_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3}|[A-Z0-9]{6})(?![A-Za-z0-9-])"
)

_LOCK = threading.RLock()
_POOL: List[Dict[str, str]] = []
_CURSOR = 0
_USED: set[str] = set()  # used signup addresses (aliases or plain emails)
_DEAD: set[str] = set()  # permanently unusable base mailboxes
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _log(cb: LogFn, msg: str) -> None:
    if cb:
        cb(msg)


def _raise_if_cancelled(cancel: CancelFn) -> None:
    if cancel and cancel():
        raise RuntimeError("cancelled")


def _sleep(seconds: float, cancel: CancelFn = None) -> None:
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        _raise_if_cancelled(cancel)
        time.sleep(min(0.25, end - time.time()))


class OutlookAuthExpired(RuntimeError):
    """Refresh token permanently unusable; caller should mark mailbox used and swap."""


_PERMANENT_AUTH_MARKERS = (
    "aadsts70000",
    "aadsts50076",
    "aadsts50173",
    "aadsts70008",
    "aadsts700082",
    "aadsts9002313",
    "grant is expired",
    "the user must sign in again",
    "refresh token has expired",
    "refresh_token is invalid",
    "invalid_grant",
    "user is authenticated but not connected",
)


def is_permanent_auth_error(msg: str) -> bool:
    low = str(msg or "").lower()
    return any(m in low for m in _PERMANENT_AUTH_MARKERS)


def _http_session(proxy: str = "", impersonate: str = "chrome"):
    proxies = None
    proxy = (proxy or "").strip()
    if proxy and proxy.lower() not in {"direct", "none", "off", "disabled"}:
        proxies = {"http": proxy, "https": proxy}
    if cffi_requests is not None:
        sess = cffi_requests.Session(impersonate=impersonate)
        if proxies:
            sess.proxies = proxies
        return sess
    if std_requests is None:
        raise RuntimeError("需要 curl_cffi 或 requests")
    sess = std_requests.Session()
    if proxies:
        sess.proxies.update(proxies)
    return sess


def parse_pool_line(line: str) -> Optional[Dict[str, str]]:
    """Parse one pool line (`----` default, `:` fallback)."""
    line = (line or "").strip().lstrip("﻿")
    if not line or line.startswith("#"):
        return None
    if "----" in line:
        parts = [p.strip() for p in line.split("----")]
    else:
        parts = [p.strip() for p in line.split(":")]
    if len(parts) < 3:
        return None
    email = parts[0]
    password = parts[1] if len(parts) > 1 else ""
    refresh = parts[2] if len(parts) > 2 else ""
    client_id = parts[3] if len(parts) > 3 and parts[3] else DEFAULT_CLIENT_ID
    if not email or "@" not in email or not refresh:
        return None
    return {
        "email": email,
        "password": password,
        "refresh_token": refresh,
        "client_id": client_id or DEFAULT_CLIENT_ID,
    }


def load_pool(pool_file: str | Path) -> List[Dict[str, str]]:
    path = _project_path(str(pool_file))
    if not path.is_file():
        raise RuntimeError(f"Outlook 邮箱池不存在: {path}")
    items: List[Dict[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        row = parse_pool_line(line)
        if row:
            items.append(row)
    if not items:
        raise RuntimeError(f"Outlook 邮箱池为空: {path}")
    return items


def used_file_path(config: dict) -> Path:
    custom = str(config.get("outlook_used_file") or "").strip()
    if custom:
        return _project_path(custom)
    return ROOT / "emails_used_outlook.txt"


def dead_file_path(config: dict) -> Path:
    custom = str(config.get("outlook_dead_file") or "").strip()
    if custom:
        return _project_path(custom)
    return ROOT / "emails_dead_outlook.txt"


def fail_file_path(config: dict) -> Path:
    custom = str(config.get("outlook_fail_file") or "").strip()
    if custom:
        return _project_path(custom)
    return ROOT / "emails_fail_outlook.txt"


def _load_email_set(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        e = line.strip().lower()
        if e and not e.startswith("#"):
            out.add(e)
    return out


def load_used(config: dict) -> set[str]:
    return _load_email_set(used_file_path(config))


def load_dead(config: dict) -> set[str]:
    return _load_email_set(dead_file_path(config))


def load_fail_counts(config: dict) -> Dict[str, int]:
    """累计每个 base 邮箱"收不到验证码而超时"的次数（跨运行 append 累积）。"""
    counts: Dict[str, int] = {}
    path = fail_file_path(config)
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        e = line.strip()
        if e and not e.startswith("#"):
            low = e.lower()
            counts[low] = counts.get(low, 0) + 1
    return counts


def _fail_threshold(config: dict) -> int:
    """累计失败多少次就永久禁用该 base 邮箱（多次收码超时）。"""
    try:
        return max(1, int(config.get("outlook_fail_threshold") or 3))
    except (TypeError, ValueError):
        return 3


def record_fail(config: dict, mailbox_email: str) -> int:
    """记录一次 OTP 收码超时，返回该 base 累计失败次数。"""
    mailbox_email = (mailbox_email or "").strip()
    if not mailbox_email:
        return 0
    path = fail_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        low = mailbox_email.lower()
        with path.open("a", encoding="utf-8") as f:
            f.write(mailbox_email + "\n")
        return load_fail_counts(config).get(low, 0)


def mark_used(config: dict, email: str) -> None:
    """Record a signup address already consumed (prefer alias, not base)."""
    email = (email or "").strip()
    if not email:
        return
    path = used_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _USED.add(email.lower())
        with path.open("a", encoding="utf-8") as f:
            f.write(email + "\n")


def mark_dead(config: dict, mailbox_email: str) -> None:
    """Permanently retire a base mailbox (token/IMAP dead)."""
    mailbox_email = (mailbox_email or "").strip()
    if not mailbox_email:
        return
    path = dead_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _DEAD.add(mailbox_email.lower())
        with path.open("a", encoding="utf-8") as f:
            f.write(mailbox_email + "\n")


def remember_session(email: str, payload: Dict[str, Any]) -> None:
    with _LOCK:
        _SESSIONS[email.lower()] = dict(payload or {})


def get_session(email: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        s = _SESSIONS.get((email or "").lower())
        return dict(s) if s else None


def make_alias(base_email: str, config: Optional[dict] = None) -> str:
    """Return a fresh Outlook plus-alias for this signup; base mailbox unchanged."""
    config = config or {}
    if not _cfg_true(config, "outlook_use_alias", True):
        return base_email
    local, sep, domain = str(base_email or "").strip().partition("@")
    if not local or not sep or not domain:
        raise ValueError(f"invalid Outlook base mailbox: {base_email!r}")
    root_local = local.split("+", 1)[0]
    tag = "g" + secrets.token_hex(5)
    return f"{root_local}+{tag}@{domain}"


def create_mailbox(config: dict) -> Tuple[str, str]:
    """Pick a pool base mailbox and return (signup_email, non-sensitive bearer).

    Alias mode: skip permanently dead bases and bases whose base-prefix already
    appears in used (xAI may remember/reject them); each pick gets a fresh
    plus-alias. Non-alias mode: one signup per base.

    The second return value is a non-sensitive placeholder (``outlook:<email>``),
    not the raw refresh token; wait_code resolves the real token via the in-memory
    session. Fresh alias is marked used here so a crash between create and receive
    never reuses the same signup address.
    """
    pool_file = str(config.get("outlook_pool_file") or "keys/outlook_pool.txt").strip()
    use_alias = _cfg_true(config, "outlook_use_alias", True)
    mark_used_enabled = _cfg_true(config, "outlook_mark_used", True)
    global _POOL, _CURSOR
    with _LOCK:
        _USED.clear()
        _USED.update(load_used(config))
        _DEAD.clear()
        _DEAD.update(load_dead(config))
        if not _POOL:
            _POOL = load_pool(pool_file)
            _CURSOR = 0
        n = len(_POOL)
        if n == 0:
            raise RuntimeError("Outlook 邮箱池为空")
        for _ in range(n):
            item = _POOL[_CURSOR % n]
            _CURSOR += 1
            base_email = item["email"]
            base_l = base_email.lower()
            if base_l in _DEAD:
                continue
            base_local = base_l.split("@")[0]
            # used 文件记录 alias(Fiona+tag@outlook.com) 或其 base；跳过已用过的 base 前缀，
            # 复用可能被 xAI 记住/标记，收不到验证码。
            if any(u.startswith(base_local + "+") for u in _USED):
                continue
            for _try in range(8):
                email = make_alias(base_email, config) if use_alias else base_email
                if email.lower() not in _USED:
                    break
            else:
                continue
            token_like = f"outlook:{email}"
            remember_session(
                email,
                {
                    "provider": "outlook",
                    "email": email,
                    "mailbox_email": base_email,
                    "password": item.get("password", ""),
                    "refresh_token": item["refresh_token"],
                    "client_id": item.get("client_id") or DEFAULT_CLIENT_ID,
                },
            )
            if mark_used_enabled:
                mark_used(config, email)
            return email, token_like
    if use_alias:
        raise RuntimeError("Outlook 邮箱池已用尽（全部在 dead 列表）")
    raise RuntimeError("Outlook 邮箱池已用尽（全部在 used 列表）")


def token_looks_like_jwt(token: str) -> bool:
    return str(token or "").count(".") in {2, 4}


def refresh_access_token(
    refresh_token: str,
    client_id: str = DEFAULT_CLIENT_ID,
    *,
    purpose: str = "imap",
    proxy: str = "",
    log: LogFn = None,
) -> Optional[str]:
    purpose = "graph" if str(purpose or "").lower() == "graph" else "imap"
    endpoints = GRAPH_TOKEN_ENDPOINTS if purpose == "graph" else IMAP_TOKEN_ENDPOINTS
    sess = _http_session(proxy)
    errors: list[str] = []
    for endpoint in endpoints:
        data = {
            "client_id": client_id or DEFAULT_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if endpoint.get("scope"):
            data["scope"] = endpoint["scope"]
        if endpoint.get("resource"):
            data["resource"] = endpoint["resource"]
        try:
            resp = sess.post(
                endpoint["url"],
                data=data,
                headers={"Accept": "application/json"},
                timeout=20,
            )
            payload = resp.json() if getattr(resp, "text", None) else {}
            if resp.status_code == 200 and payload.get("access_token"):
                token = str(payload["access_token"])
                if purpose == "graph" and not token_looks_like_jwt(token):
                    errors.append(f"{endpoint['name']}: opaque token (not Graph JWT)")
                    continue
                new_rt = str(payload.get("refresh_token") or "").strip()
                if new_rt and new_rt != refresh_token:
                    with _LOCK:
                        for s in _SESSIONS.values():
                            if s.get("refresh_token") == refresh_token:
                                s["refresh_token"] = new_rt
                return token
            msg = (
                (payload or {}).get("error_description")
                or (payload or {}).get("error")
                or f"HTTP {resp.status_code}"
            )
            errors.append(f"{endpoint['name']}: {msg}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{endpoint['name']}: {e}")
    if errors:
        _log(log, f"[outlook] token endpoints failed: {errors[0][:180]}")
        if errors and all(is_permanent_auth_error(e) for e in errors):
            raise OutlookAuthExpired(errors[0][:240])
    return None


def _looks_like_xai_mail(subject: str, body: str, sender: str = "") -> bool:
    blob = f"{subject}\n{body}\n{sender}".lower()
    markers = (
        "xai",
        "x.ai",
        "grok",
        "confirmation code",
        "verification code",
        "verify your email",
        "accounts.x.ai",
    )
    return any(m in blob for m in markers)


def extract_code(text: str, subject: str = "", sender: str = "") -> Optional[str]:
    blob = f"{subject}\n{text or ''}"
    m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", blob)
    if m:
        return m.group(1)
    if not _looks_like_xai_mail(subject, text or "", sender):
        return None
    m = re.search(
        r"(?:verification|confirmation|your)\s+code(?:\s+is)?\s*[:：-]?\s*"
        r"(?<![A-Za-z0-9-])([A-Z0-9]{6})(?![A-Za-z0-9-])",
        blob,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    return None


def fetch_messages_office_rest(
    access_token: str,
    *,
    top: int = 10,
    proxy: str = "",
    log: LogFn = None,
) -> Tuple[List[Dict[str, Any]], str]:
    sess = _http_session(proxy)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    urls = [
        (
            "https://outlook.office.com/api/v2.0/me/messages"
            f"?$top={top}&$orderby=ReceivedDateTime desc"
            "&$select=Subject,From,ReceivedDateTime,BodyPreview,Body"
        ),
        (
            "https://outlook.office.com/api/v2.0/me/mailfolders/inbox/messages"
            f"?$top={top}&$orderby=ReceivedDateTime desc"
            "&$select=Subject,From,ReceivedDateTime,BodyPreview,Body"
        ),
        (
            "https://outlook.office.com/api/v2.0/me/mailfolders/junkemail/messages"
            f"?$top={top}&$orderby=ReceivedDateTime desc"
            "&$select=Subject,From,ReceivedDateTime,BodyPreview,Body"
        ),
    ]
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    last_err = ""
    saw_ok = False
    for url in urls:
        try:
            resp = sess.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    last_err = "rest_401" if resp.status_code == 401 else "rest_403"
                    _log(log, f"[outlook] REST {resp.status_code} (disable REST for session)")
                    return out, last_err
                _log(log, f"[outlook] REST {resp.status_code}: {(resp.text or '')[:120]!r}")
                last_err = f"rest_http_{resp.status_code}"
                continue
            saw_ok = True
            vals = (resp.json() or {}).get("value") or []
            for m in vals:
                subject = str(m.get("Subject") or m.get("subject") or "")
                received = str(m.get("ReceivedDateTime") or m.get("receivedDateTime") or "")
                sender = ""
                frm = m.get("From") or m.get("from") or {}
                if isinstance(frm, dict):
                    ea = frm.get("EmailAddress") or frm.get("emailAddress") or {}
                    if isinstance(ea, dict):
                        sender = str(ea.get("Address") or ea.get("address") or "")
                body = ""
                body_obj = m.get("Body") or m.get("body") or {}
                if isinstance(body_obj, dict):
                    body = str(body_obj.get("Content") or body_obj.get("content") or "")
                if not body:
                    body = str(m.get("BodyPreview") or m.get("bodyPreview") or "")
                key = f"{subject}|{received}|{sender}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "subject": subject,
                        "from": sender,
                        "body": body,
                        "received": received,
                    }
                )
            if out:
                break
        except Exception as e:  # noqa: BLE001
            _log(log, f"[outlook] REST error: {e}")
            last_err = f"rest_error:{e}"
            continue
    if out or saw_ok:
        return out, ""
    return out, last_err


def fetch_messages_graph(
    access_token: str,
    *,
    top: int = 10,
    proxy: str = "",
) -> List[Dict[str, Any]]:
    if not token_looks_like_jwt(access_token):
        return []
    sess = _http_session(proxy)
    headers = {"Authorization": f"Bearer {access_token}"}
    out: List[Dict[str, Any]] = []
    for folder in ("inbox", "junkemail"):
        url = (
            f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
            f"?$top={top}&$orderby=receivedDateTime desc"
            f"&$select=subject,from,body,bodyPreview,receivedDateTime"
        )
        try:
            resp = sess.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                continue
            for m in (resp.json() or {}).get("value", []) or []:
                out.append(
                    {
                        "subject": m.get("subject", ""),
                        "from": ((m.get("from") or {}).get("emailAddress") or {}).get(
                            "address", ""
                        ),
                        "body": ((m.get("body") or {}).get("content") or m.get("bodyPreview") or ""),
                        "received": m.get("receivedDateTime", ""),
                    }
                )
        except Exception:
            continue
    return out


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def _retry_connect(
    connect_fn: Callable[[], Any],
    *,
    retries: int,
    delays: List[float],
    log: LogFn = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            obj = connect_fn()
            if attempt > 1:
                _log(log, f"[outlook] IMAP connected OK after {attempt - 1} retries")
            return obj
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries:
                delay = delays[min(attempt - 1, len(delays) - 1)]
                _log(
                    log,
                    f"[outlook] IMAP connect failed: {type(e).__name__}: {e} "
                    f"(try {attempt}/{retries}); retrying in {delay}s",
                )
                sleep_fn(delay)
            else:
                _log(
                    log,
                    f"[outlook] IMAP connect failed: {type(e).__name__}: {e} "
                    f"(try {attempt}/{retries}); giving up",
                )
    assert last_exc is not None
    raise last_exc


def _connect_imap(log: LogFn = None) -> Any:
    return _retry_connect(
        lambda: imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30),
        retries=IMAP_CONNECT_RETRIES,
        delays=IMAP_CONNECT_RETRY_DELAYS,
        log=log,
    )


def _imap_http_post(path: str, payload: dict, timeout: int = 90) -> dict:
    """POST to the remote IMAP bridge (self-signed TLS). Returns parsed JSON."""
    if not _IMAP_HTTP_URL:
        return {"messages": [], "error": "imap_bridge_not_configured"}
    url = f"{_IMAP_HTTP_URL}/{path.lstrip('/')}"
    kwargs = {
        "data": json.dumps(payload).encode("utf-8"),
        "headers": {
            "Authorization": f"Bearer {_IMAP_HTTP_TOKEN}",
            "Content-Type": "application/json",
        },
        "timeout": timeout,
        "verify": False,
    }
    try:
        if cffi_requests is not None:
            resp = cffi_requests.post(url, **kwargs)
        elif std_requests is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                resp = std_requests.post(url, **kwargs)
        else:
            return {"messages": [], "error": "imap_bridge_no_http_lib"}
        try:
            return resp.json()
        except Exception:
            return {"messages": [], "error": f"imap_bridge_bad_json:{resp.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"messages": [], "error": f"imap_bridge:{type(e).__name__}:{e}"}


def fetch_messages_imap(
    email: str,
    access_token: str,
    *,
    folders: Optional[List[str]] = None,
    top: int = 10,
    log: LogFn = None,
) -> Tuple[List[Dict[str, Any]], str]:
    folders = folders or ["INBOX", "Junk"]
    if _IMAP_HTTP_URL:
        data = _imap_http_post(
            "imap/fetch", {"email": email, "access_token": access_token, "folders": folders}
        )
        msgs = list(data.get("messages") or [])
        if top and len(msgs) > top:
            msgs = msgs[:top]
        return msgs, str(data.get("error") or "")
    out: List[Dict[str, Any]] = []
    try:
        imap = _connect_imap(log=log)
    except Exception as e:  # noqa: BLE001
        return out, f"imap_connect:{e}"
    try:
        auth_str = _xoauth2_string(email, access_token)
        try:
            typ, data = imap.authenticate("XOAUTH2", lambda _: auth_str.encode("utf-8"))
        except imaplib.IMAP4.error as e:
            _log(log, f"[outlook] IMAP XOAUTH2 error: {e}")
            err = str(e)
            if is_permanent_auth_error(err):
                return out, "imap_auth_not_connected"
            return out, f"imap_xoauth2:{err}"
        if typ != "OK":
            _log(log, f"[outlook] IMAP XOAUTH2 failed: {typ} {data!r}")
            return out, f"imap_xoauth2_failed:{typ}"
        for folder in folders:
            try:
                typ, _ = imap.select(folder, readonly=True)
                if typ != "OK":
                    for alt in (folder.upper(), folder.lower(), "Junk Email", "JunkEmail"):
                        if alt == folder:
                            continue
                        typ, _ = imap.select(alt, readonly=True)
                        if typ == "OK":
                            folder = alt
                            break
                    if typ != "OK":
                        continue
                typ, data = imap.search(None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    continue
                ids = data[0].split()
                for msg_id in reversed(ids[-top:]):
                    typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                    if typ != "OK" or not msg_data:
                        continue
                    raw = b""
                    for part in msg_data:
                        if (
                            isinstance(part, tuple)
                            and len(part) >= 2
                            and isinstance(part[1], (bytes, bytearray))
                        ):
                            raw += bytes(part[1])
                    if not raw:
                        continue
                    msg = email_lib.message_from_bytes(raw)
                    subject = _decode_mime_header(msg.get("Subject", ""))
                    sender = _decode_mime_header(msg.get("From", ""))
                    received = msg.get("Date", "")
                    body_parts: list[str] = []
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = (part.get_content_type() or "").lower()
                            if ctype in {"text/plain", "text/html"}:
                                try:
                                    payload = part.get_payload(decode=True) or b""
                                    charset = part.get_content_charset() or "utf-8"
                                    body_parts.append(payload.decode(charset, errors="replace"))
                                except Exception:
                                    continue
                    else:
                        try:
                            payload = msg.get_payload(decode=True) or b""
                            charset = msg.get_content_charset() or "utf-8"
                            body_parts.append(payload.decode(charset, errors="replace"))
                        except Exception:
                            body_parts.append(str(msg.get_payload() or ""))
                    out.append(
                        {
                            "subject": subject,
                            "from": sender,
                            "body": "\n".join(body_parts),
                            "received": received,
                            "folder": folder,
                        }
                    )
            except Exception as e:  # noqa: BLE001
                _log(log, f"[outlook] IMAP folder {folder} error: {e}")
                continue
        return out, ""
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _retire_mailbox(config: dict, mailbox_email: str, reason: str, log: LogFn = None) -> None:
    """Never pick this base mailbox again (writes emails_dead_outlook.txt)."""
    if _cfg_true(config, "outlook_mark_used", True):
        mark_dead(config, mailbox_email)
    _log(log, f"[outlook] retire mailbox mark_dead={mailbox_email} reason={reason}")


def _preseed_should_skip(received_ts: float, submitted_at: float, margin_sec: float) -> bool:
    """True when a pre-existing mailbox message is from BEFORE this signup and should be
    swallowed as history (previous signups on the same base), so alias mode ignores stale
    codes instead of reusing them.

    A fresh OTP can carry a Date header ~1-2s EARLIER than `submitted_at`: xAI generates
    the code at the click instant, while `submitted_at` is captured a beat later (after
    debug I/O). With no margin, the fresh code looks "older than submission" and gets
    swallowed -> OTP silently missed -> 180s timeout. So only skip messages strictly
    older than submitted_at - margin; codes within the margin stay candidates.
    """
    try:
        return bool(float(received_ts) < float(submitted_at) - float(margin_sec))
    except (TypeError, ValueError):
        return False


def _purge_mailbox(
    config: dict,
    mailbox_email: str,
    refresh: str,
    client_id: str,
    proxy: str,
    log: LogFn = None,
) -> bool:
    """Best-effort 清空被永久禁用的 base 邮箱 INBOX/Junk（避免过期旧码残留干扰）。

    打网络（IMAP 或远程桥），失败不阻塞禁用与后续流程，只记日志。
    """
    if _IMAP_HTTP_URL:
        try:
            token = refresh_access_token(
                refresh, client_id, purpose="imap", proxy=proxy, log=log
            )
            if not token:
                _log(log, f"[outlook] purge skip: token refresh failed for {mailbox_email}")
                return False
            data = _imap_http_post("imap/purge", {"email": mailbox_email, "access_token": token})
            cleared = int(data.get("cleared") or 0)
            _log(log, f"[outlook] purged {cleared} msgs from {mailbox_email} (bridge)")
            return True
        except Exception as exc:  # noqa: BLE001
            _log(log, f"[outlook] purge failed {mailbox_email}: {exc}")
            return False
    try:
        token = refresh_access_token(
            refresh, client_id, purpose="imap", proxy=proxy, log=log
        )
        if not token:
            _log(log, f"[outlook] purge skip: token refresh failed for {mailbox_email}")
            return False
        imap = _connect_imap(log=log)
        try:
            auth_str = _xoauth2_string(mailbox_email, token)
            imap.authenticate("XOAUTH2", lambda _: auth_str.encode("utf-8"))
            cleared = 0
            for folder in ("INBOX", "Junk"):
                try:
                    typ, _data = imap.select(folder, readonly=False)
                    if typ != "OK":
                        continue
                    typ, data = imap.search(None, "ALL")
                    if typ != "OK" or not data or not data[0]:
                        continue
                    ids = data[0].split()
                    if not ids:
                        continue
                    imap.store(b",".join(ids), "+FLAGS", "\\Deleted")
                    imap.expunge()
                    cleared += len(ids)
                except Exception as fe:  # noqa: BLE001
                    _log(log, f"[outlook] purge folder {folder} error: {fe}")
            _log(log, f"[outlook] purged {cleared} msgs from {mailbox_email}")
            return True
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        _log(log, f"[outlook] purge failed {mailbox_email}: {exc}")
        return False


def wait_code(
    email: str,
    refresh: str = "",
    client_id: str = "",
    config: Optional[dict] = None,
    *,
    timeout: int = 180,
    poll_interval: float = 3.0,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    resend_callback: Callable[[], Any] = None,
    submitted_at: Optional[float] = None,
) -> str:
    """Poll mailbox for xAI OTP.

    Resolves the real refresh token / client_id from the in-memory session
    (created by create_mailbox); the passed `refresh`/`client_id` are only
    fallbacks when no session exists. Retires (bans) base mailbox on permanent
    token refresh failure, IMAP auth dead for N consecutive polls, or OTP
    timeout in non-alias mode.
    """
    config = config or {}
    sess = get_session(email)
    if not sess:
        raise RuntimeError(f"Outlook session missing for {email}; call create_mailbox first")
    mailbox_email = str(sess.get("mailbox_email") or sess.get("email") or email)
    refresh = str(sess.get("refresh_token") or refresh or "")
    client_id = str(sess.get("client_id") or client_id or DEFAULT_CLIENT_ID)
    proxy = str(config.get("outlook_proxy") or config.get("proxy") or "")
    if not refresh:
        raise RuntimeError("Outlook session missing refresh_token")

    deadline = time.time() + max(30, int(timeout))
    seen: set[str] = set()
    mode = ""
    # Pre-seed `seen` with current inbox so alias-mode signups ignore OTPs from
    # previous signups sharing the same base mailbox. Guard with the preseed
    # margin: xAI generates the fresh code at the click instant while
    # `submitted_at` is captured a beat later (after debug I/O), so the fresh
    # OTP's Date header can be ~1-2s EARLIER than submitted_at. Only swallow
    # messages strictly older than submitted_at - margin; anything within the
    # margin stays a candidate so the code we are waiting for is not missed.
    preseed_margin = float(config.get("outlook_preseed_margin_sec", 60) or 60)
    if _cfg_true(config, "outlook_use_alias", True):
        try:
            _tok = refresh_access_token(
                refresh, client_id, purpose="imap", proxy=proxy, log=log_callback
            )
            if _tok:
                _msgs, _ = fetch_messages_imap(
                    mailbox_email, _tok, folders=["INBOX", "Junk"], top=20, log=log_callback
                )
                for _m in _msgs:
                    if submitted_at is not None:
                        _recv = _m.get("received") or ""
                        _dt = None
                        try:
                            _dt = parsedate_to_datetime(_recv)
                        except Exception:
                            _dt = None
                        if _dt is None:
                            # Unparseable date: stay conservative, leave for poll.
                            continue
                        if not _preseed_should_skip(_dt.timestamp(), submitted_at, preseed_margin):
                            # Fresh code (at/just before submission) -> candidate.
                            continue
                    seen.add(
                        f"{_m.get('subject') or ''}|{_m.get('received')}|{_m.get('from') or ''}"
                    )
                _log(log_callback, f"[outlook] pre-seeded {len(seen)} existing messages (alias reuse)")
        except Exception as exc:  # noqa: BLE001
            _log(log_callback, f"[outlook] pre-seed failed: {exc}")
    rest_enabled = _cfg_true(config, "outlook_use_office_rest", False)
    dead_streak = 0
    dead_threshold = max(1, int(config.get("outlook_dead_mailbox_streak") or 2))
    while time.time() < deadline:
        _raise_if_cancelled(cancel_callback)
        messages: List[Dict[str, Any]] = []
        rest_err = ""
        imap_err = ""

        try:
            imap_token = refresh_access_token(
                refresh, client_id, purpose="imap", proxy=proxy, log=log_callback
            )
        except OutlookAuthExpired as exc:
            _retire_mailbox(config, mailbox_email, f"token_expired:{exc}", log_callback)
            raise OutlookAuthExpired(
                f"Outlook refresh token expired for {mailbox_email}: {exc}"
            ) from exc

        if imap_token:
            if mode != "imap":
                _log(log_callback, "[outlook] using IMAP reader")
                mode = "imap"
            imap_msgs, imap_err = fetch_messages_imap(
                mailbox_email, imap_token, folders=["INBOX", "Junk"], top=10, log=log_callback
            )
            messages.extend(imap_msgs)

            if not messages and rest_enabled:
                if mode != "rest":
                    _log(log_callback, "[outlook] trying Office REST reader")
                rest_msgs, rest_err = fetch_messages_office_rest(
                    imap_token, top=12, proxy=proxy, log=log_callback
                )
                messages.extend(rest_msgs)
                if rest_err in {"rest_401", "rest_403"} or str(rest_err).startswith(
                    "rest_http_401"
                ):
                    rest_enabled = False
                    _log(log_callback, "[outlook] REST unauthorized — skip REST for this mailbox")
        else:
            try:
                graph_token = refresh_access_token(
                    refresh, client_id, purpose="graph", proxy=proxy, log=log_callback
                )
            except OutlookAuthExpired as exc:
                _retire_mailbox(config, mailbox_email, f"token_expired:{exc}", log_callback)
                raise OutlookAuthExpired(
                    f"Outlook refresh token expired for {mailbox_email}: {exc}"
                ) from exc
            if graph_token and token_looks_like_jwt(graph_token):
                if mode != "graph":
                    _log(log_callback, "[outlook] fallback Graph reader")
                    mode = "graph"
                messages.extend(fetch_messages_graph(graph_token, top=10, proxy=proxy))
            else:
                _log(log_callback, "[outlook] token refresh failed; retry")
                _sleep(poll_interval, cancel_callback)
                continue

        if not messages:
            rest_dead = rest_err in {"rest_401", "rest_403"} or str(rest_err).startswith(
                "rest_http_401"
            )
            imap_dead = (
                imap_err == "imap_auth_not_connected"
                or "authenticated but not connected" in str(imap_err).lower()
                or str(imap_err).startswith("imap_xoauth2")
                or str(imap_err).startswith("imap_xoauth2_failed")
            )
            if imap_dead:
                dead_streak += 1
                # imap_auth_not_connected 高并发下是 Outlook 常见**瞬态**
                # （15 并发同时 IMAP 连 outlook.office365.com 被节流/握手竞态），
                # 不是 base 邮箱永久坏。必须按 dead_threshold 重试，而不是
                # threshold=1 一次性永久拉黑——上一版把大量正常 base 误杀
                # （单轮 183 个 = 23% 账号白烧）。
                threshold = dead_threshold
                if dead_streak >= threshold:
                    reason = f"rest={rest_err or '-'} imap={imap_err or '-'} streak={dead_streak}"
                    _retire_mailbox(config, mailbox_email, f"reader_dead:{reason}", log_callback)
                    raise OutlookAuthExpired(
                        f"Outlook mailbox unreadable for {mailbox_email}: {reason}"
                    )
                _log(
                    log_callback,
                    f"[outlook] imap_dead streak={dead_streak}/{dead_threshold} "
                    f"(transient, keep trying) imap={imap_err or '-'}",
                )
                _sleep(max(poll_interval, 3.0), cancel_callback)
                continue
            elif rest_dead and not imap_err:
                dead_streak = 0
            else:
                dead_streak = 0
        else:
            dead_streak = 0

        for msg in messages:
            subject = str(msg.get("subject") or "")
            body = str(msg.get("body") or "")
            sender = str(msg.get("from") or "")
            key = f"{subject}|{msg.get('received')}|{sender}"
            if key in seen:
                continue
            seen.add(key)
            code = extract_code(body, subject, sender)
            if code:
                _log(log_callback, f"[outlook] code={code} subject={subject!r}")
                if _cfg_true(config, "outlook_mark_used", True):
                    mark_used(config, email)
                return code
        _sleep(poll_interval, cancel_callback)

    use_alias = _cfg_true(config, "outlook_use_alias", True)
    retired = False
    if _cfg_true(config, "outlook_mark_used", True):
        mark_used(config, email)
    if not use_alias:
        # 非别名模式：base 只能产一个号，单次收码超时即永久禁用。
        _retire_mailbox(config, mailbox_email, "otp_timeout", log_callback)
        retired = True
    fail_count = record_fail(config, mailbox_email)
    # 别名模式：同一 base 多次（>=阈值）收码超时会积累到 fail 列表，
    # 说明该 base 连新验证码都收不到 → 永久禁用并清空其邮箱，避免残留/后续复用。
    if not retired and fail_count >= _fail_threshold(config):
        _retire_mailbox(
            config,
            mailbox_email,
            f"repeated_otp_timeout count={fail_count}",
            log_callback,
        )
        retired = True
    if retired:
        _purge_mailbox(config, mailbox_email, refresh, client_id, proxy, log_callback)
    _log(
        log_callback,
        f"[outlook] otp_timeout mark_used={email} base_kept={use_alias} "
        f"fail_count={fail_count} retired={retired}",
    )
    raise TimeoutError(f"Outlook 在 {timeout}s 内未收到验证码: {email}")


# Backward-compatible aliases retained from the nosms wrapper contract.
outlook_create = create_mailbox
outlook_wait_code = wait_code