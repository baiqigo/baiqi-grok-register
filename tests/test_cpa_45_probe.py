# -*- coding: utf-8 -*-
"""cpa_45_probe 单测 + add_sso_to_cpa gate 单测（纯 mock，不碰网络 / 邮箱池）。

A3: 注册 + CPA Device Flow 拿到 token 后，先探测该号是否有 grok-4.5 模型权限，
有才推 CPA + grokcli-2api，没有就跳过所有写入（SSO 已由调用方保存）。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cpa_45_probe
import grok_register_ttk  # noqa: E402


class _FakeResp:
    """urllib response 替身：支持 with 上下文 + .read() + .status。"""

    def __init__(self, status=200, raw=b""):
        self.status = status
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_opener(fake_opener):
    return patch.object(cpa_45_probe, "_opener", return_value=fake_opener)


def _transport_error_opener():
    return MagicMock(open=MagicMock(side_effect=urllib.error.URLError("timed out")))


def _http_error_opener(status=401):
    body = io.BytesIO(b'{"error":"unauthorized"}')
    exc = urllib.error.HTTPError("https://cli-chat-proxy.grok.com/v1/models", status, "denied", {}, body)
    return MagicMock(open=MagicMock(side_effect=exc))


# ---------------------------------------------------------------- probe 单测
def test_probe_has_grok45_when_models_include_it():
    fake = MagicMock(open=MagicMock(return_value=_FakeResp(200, b'{"data":[{"id":"grok-3"},{"id":"grok-4.5"}]}')))
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=1)
    assert result["has_grok_45"] is True
    assert result["ok"] is True
    assert result["reason"] == "ok"
    assert "grok-4.5" in result["model_ids"]
    assert result["status"] == 200


def test_probe_no_grok45_returns_models_ok_but_no_grok45():
    fake = MagicMock(open=MagicMock(return_value=_FakeResp(200, b'{"data":[{"id":"grok-3"}]}')))
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=1)
    assert result["has_grok_45"] is False
    assert result["ok"] is False
    assert result["reason"] == "models_ok_but_no_grok45"
    assert result["model_ids"] == ["grok-3"]


def test_probe_empty_models_reason_empty_models():
    fake = MagicMock(open=MagicMock(return_value=_FakeResp(200, b'{"data":[]}')))
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=1)
    assert result["has_grok_45"] is False
    assert result["reason"] == "empty_models"
    assert result["model_ids"] == []


def test_probe_http_401_reason_http_error():
    fake = _http_error_opener(401)
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=1)
    assert result["has_grok_45"] is False
    assert result["ok"] is False
    assert result["reason"] == "http_error"
    assert result["status"] == 401


def test_probe_transport_error_reason_transport_error():
    fake = _transport_error_opener()
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=1)
    assert result["has_grok_45"] is False
    assert result["ok"] is False
    assert result["reason"] == "transport_error"
    assert result["status"] == 0


def test_probe_retries_calls_opener_n_times():
    fake = _transport_error_opener()
    with _patch_opener(fake):
        result = cpa_45_probe.probe_grok_45("tok", retries=3)
    assert fake.open.call_count == 3
    assert result["attempt_count"] == 3
    assert result["has_grok_45"] is False


def test_probe_default_base_url_is_cli_chat_proxy():
    fake = MagicMock(open=MagicMock(return_value=_FakeResp(200, b'{"data":[]}')))
    with _patch_opener(fake):
        cpa_45_probe.probe_grok_45("tok", retries=1)
    req = fake.open.call_args[0][0]
    assert req.full_url == "https://cli-chat-proxy.grok.com/v1/models"


def test_probe_headers_include_auth_and_45_override():
    fake = MagicMock(open=MagicMock(return_value=_FakeResp(200, b'{"data":[]}')))
    with _patch_opener(fake):
        cpa_45_probe.probe_grok_45("tok", retries=1)
    req = fake.open.call_args[0][0]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-grok-model-override"] == "grok-4.5"
    assert headers["x-xai-token-auth"] == "xai-grok-cli"
    assert headers["accept"] == "application/json"


# ---------------------------------------------------------- add_sso_to_cpa gate
def _make_config(tmp: Path) -> dict:
    return {
        "cpa_auto_add": True,
        "cpa_token_mode": "device_protocol",
        "cpa_auth_dir": str(tmp / "cpa_auth"),
        "cpa_remote_url": "",
        "cpa_management_key": "",
        "grok2api_auth_dir": str(tmp / "g2a"),
        "cpa_require_grok_45": True,
        "cpa_probe_base_url": "",
        "cpa_probe_events_path": str(tmp / "probe_events.jsonl"),
        "cpa_probe_retries": 3,
        "proxy": "",
    }


def _patch_sso_to_token():
    return patch.object(
        grok_register_ttk._s2cpa,
        "sso_to_token",
        return_value={"access_token": "acc", "refresh_token": "ref", "expires_in": 21600, "token_type": "Bearer"},
    )


def _patch_token_to_cpa_record():
    return patch.object(
        grok_register_ttk._s2cpa,
        "token_to_cpa_record",
        return_value={"access_token": "acc", "refresh_token": "ref", "email": "a@b.com"},
    )


def test_add_sso_to_cpa_skips_all_writes_when_no_grok45(tmp_path):
    cfg = _make_config(tmp_path)
    probe = {"has_grok_45": False, "ok": False, "reason": "models_ok_but_no_grok45",
             "model_ids": ["grok-3"], "status": 200, "latency_ms": 5, "error": ""}
    with patch.object(grok_register_ttk, "config", cfg), _patch_sso_to_token(), _patch_token_to_cpa_record(), \
         patch.object(grok_register_ttk, "_resolve_cpa_proxy", return_value=""), \
         patch.object(grok_register_ttk, "_probe_account_grok_45", return_value=probe), \
         patch.object(grok_register_ttk._s2cpa, "write_cpa_auth") as w, \
         patch.object(grok_register_ttk._s2cpa, "write_grok2api_auth") as wg, \
         patch.object(grok_register_ttk._s2cpa, "upload_cpa_auth_remote") as up:
        ok = grok_register_ttk.add_sso_to_cpa("sso-token", email="a@b.com")
    assert ok is False
    w.assert_not_called()
    wg.assert_not_called()
    up.assert_not_called()
    events = Path(cfg["cpa_probe_events_path"]).read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    ev = json.loads(events[0])
    assert ev["email"] == "a@b.com"
    assert ev["has_grok_45"] is False
    assert ev["reason"] == "models_ok_but_no_grok45"
    assert ev["pushed"] is False
    assert ev["model_ids"] == ["grok-3"]


def test_add_sso_to_cpa_writes_when_has_grok45(tmp_path):
    cfg = _make_config(tmp_path)
    probe = {"has_grok_45": True, "ok": True, "reason": "ok",
             "model_ids": ["grok-4.5"], "status": 200, "latency_ms": 5, "error": ""}
    with patch.object(grok_register_ttk, "config", cfg), _patch_sso_to_token(), _patch_token_to_cpa_record(), \
         patch.object(grok_register_ttk, "_resolve_cpa_proxy", return_value=""), \
         patch.object(grok_register_ttk, "_probe_account_grok_45", return_value=probe), \
         patch.object(grok_register_ttk._s2cpa, "write_cpa_auth") as w, \
         patch.object(grok_register_ttk._s2cpa, "write_grok2api_auth") as wg, \
         patch.object(grok_register_ttk._s2cpa, "upload_cpa_auth_remote") as up:
        ok = grok_register_ttk.add_sso_to_cpa("sso-token", email="a@b.com")
    assert ok is True
    w.assert_called_once()
    wg.assert_called_once()
    up.assert_not_called()
    events = Path(cfg["cpa_probe_events_path"]).read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    assert json.loads(events[0])["pushed"] is True


def test_add_sso_to_cpa_probe_disabled_still_writes(tmp_path):
    cfg = _make_config(tmp_path)
    cfg["cpa_require_grok_45"] = False
    with patch.object(grok_register_ttk, "config", cfg), _patch_sso_to_token(), _patch_token_to_cpa_record(), \
         patch.object(grok_register_ttk, "_resolve_cpa_proxy", return_value=""), \
         patch.object(grok_register_ttk, "_probe_account_grok_45") as probe, \
         patch.object(grok_register_ttk._s2cpa, "write_cpa_auth") as w, \
         patch.object(grok_register_ttk._s2cpa, "write_grok2api_auth") as wg:
        ok = grok_register_ttk.add_sso_to_cpa("sso-token", email="a@b.com")
    assert ok is True
    probe.assert_not_called()
    w.assert_called_once()
    wg.assert_called_once()


def test_add_sso_to_cpa_probe_exception_fails_closed(tmp_path):
    cfg = _make_config(tmp_path)
    with patch.object(grok_register_ttk, "config", cfg), _patch_sso_to_token(), _patch_token_to_cpa_record(), \
         patch.object(grok_register_ttk, "_resolve_cpa_proxy", return_value=""), \
         patch.object(grok_register_ttk, "_probe_account_grok_45", side_effect=RuntimeError("boom")), \
         patch.object(grok_register_ttk._s2cpa, "write_cpa_auth") as w, \
         patch.object(grok_register_ttk._s2cpa, "write_grok2api_auth") as wg:
        ok = grok_register_ttk.add_sso_to_cpa("sso-token", email="a@b.com")
    assert ok is False
    w.assert_not_called()
    wg.assert_not_called()


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
                print(f"OK {name}")
            except Exception:
                failed += 1
                traceback.print_exc()
    if failed:
        raise SystemExit(1)
    print("OK test_cpa_45_probe")
