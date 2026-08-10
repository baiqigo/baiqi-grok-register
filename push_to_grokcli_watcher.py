"""grokcli-2api 自动推送 watcher：监控 panel cpa_auth 目录，新 xAI auth 自动 docker exec 导入。

用法：python push_to_grokcli_watcher.py [--once]
--once: 只扫描导入一次（供 CI/收尾），否则常驻轮询。
"""
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CP_AUTH_DIR = Path(__file__).resolve().parent / "cpa_auth"
STATE_FILE = Path(__file__).resolve().parent / ".grokcli_imported.json"
CONTAINER = "grokcli-2api"


def _load_imported():
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_imported(imported):
    STATE_FILE.write_text(
        json.dumps(sorted(imported), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def import_into_grokcli(path: Path) -> str:
    """docker exec 导入单个 auth 文件，返回结果摘要。"""
    script = (
        "import json,sys; from grok2api.pool.accounts import import_auth_payload; "
        "payload=json.load(sys.stdin); r=import_auth_payload(payload, merge=True); "
        "print('__GROKCLI_IMPORT_RESULT__='+json.dumps(r, ensure_ascii=False))"
    )
    with open(path, "rb") as f:
        data = f.read()
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "sh", "-c", f"cd /app && python -c \"{script}\""],
        input=data,
        capture_output=True,
        timeout=60,
    )
    out = proc.stdout.decode("utf-8", "replace")
    for line in reversed(out.splitlines()):
        if line.startswith("__GROKCLI_IMPORT_RESULT__="):
            return line.split("=", 1)[1][:200]
    return f"import failed rc={proc.returncode} err={proc.stderr.decode('utf-8','replace')[:120]}"


def scan_once(log=print) -> int:
    imported = _load_imported()
    files = sorted(glob.glob(str(CP_AUTH_DIR / "xai-*.json")))
    new = 0
    for p in files:
        if p in imported:
            continue
        if os.path.getsize(p) == 0:
            continue
        try:
            log(f"[grokcli-push] importing {Path(p).name} ...")
            result = import_into_grokcli(Path(p))
            log(f"[grokcli-push] {Path(p).name}: {result}")
            imported.add(p)
            new += 1
        except Exception as e:  # noqa: BLE001
            log(f"[grokcli-push] {Path(p).name} ERR: {e}")
    if new:
        _save_imported(imported)
    return new


def main():
    once = "--once" in sys.argv
    if once:
        n = scan_once()
        print(f"[grokcli-push] imported {n} new auth(s)")
        return
    print(f"[grokcli-push] watching {CP_AUTH_DIR} (ctrl+C to stop)")
    while True:
        try:
            scan_once()
        except Exception as e:  # noqa: BLE001
            print(f"[grokcli-push] scan err: {e}")
        time.sleep(10)


if __name__ == "__main__":
    main()
