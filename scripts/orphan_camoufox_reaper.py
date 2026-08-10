#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Orphan Camoufox/Firefox reaper for grok-register-panel.

Kills:
  1) camoufox.exe / firefox.exe whose parent PID is already dead
  2) camoufox processes whose -profile path is under grok-register-camoufox
     and the owner PID encoded in the profile dir name is dead
  3) stale managed profile dirs (owner process gone)

Safe to run while batch is live: only dead-parent / dead-owner targets.

Usage:
  python -u scripts/orphan_camoufox_reaper.py --once
  python -u scripts/orphan_camoufox_reaper.py --loop --interval 300
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROFILE_MARKER = "grok-register-camoufox"
PROFILE_NAME_RE = re.compile(r"^(\d+)-\d+-[0-9a-fA-F]{8}$")
BROWSER_NAMES = ("camoufox.exe", "firefox.exe", "camoufox", "firefox")


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    cmd: str


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [orphan-reaper] {msg}", flush=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _list_browser_procs() -> list[Proc]:
    """Windows: list camoufox/firefox with parent + command line."""
    names = " OR ".join(f"Name = '{n}'" for n in ("camoufox.exe", "firefox.exe"))
    ps = (
        f"Get-CimInstance Win32_Process -Filter \"{names}\" | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).strip()
    except Exception as exc:
        _log(f"list procs failed: {exc}")
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[Proc] = []
    for row in data or []:
        try:
            pid = int(row.get("ProcessId") or 0)
            ppid = int(row.get("ParentProcessId") or 0)
            name = str(row.get("Name") or "")
            cmd = str(row.get("CommandLine") or "")
        except Exception:
            continue
        if pid > 0:
            out.append(Proc(pid=pid, ppid=ppid, name=name, cmd=cmd))
    return out


def _profile_owner_pid(cmd: str) -> int | None:
    """Extract owner pid from -profile .../grok-register-camoufox/<pid>-<tid>-<uuid>."""
    if PROFILE_MARKER not in cmd:
        return None
    m = re.search(
        rf"{re.escape(PROFILE_MARKER)}[\\/](\d+)-\d+-[0-9a-fA-F]{{8}}",
        cmd,
        flags=re.I,
    )
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _is_panel_browser(cmd: str) -> bool:
    c = (cmd or "").lower()
    return (
        PROFILE_MARKER in c
        or "grok-register-panel" in c
        or "grok-register-camoufox" in c
        or "camoufox\\cache" in c
        or "camoufox/cache" in c
    )


def _kill_pid_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    # Already gone counts as success (avoids noisy kill-failed on children
    # after parent /T reap).
    if not _pid_alive(pid):
        return True
    try:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if r.returncode == 0:
            return True
        # taskkill non-zero often means process exited mid-kill
        return not _pid_alive(pid)
    except Exception:
        return not _pid_alive(pid)


def _should_kill(p: Proc) -> tuple[bool, str]:
    """Decide whether a camoufox/firefox proc is an orphan.

    Profile path encodes the creating python PID as owner:
      grok-register-camoufox/<owner_pid>-<tid>-<uuid>

    Rules:
      - owner alive  → NEVER kill (live worker; parent may be intermediate)
      - owner dead   → kill
      - no owner + parent dead → kill
      - no owner + parent alive → keep
    """
    owner = _profile_owner_pid(p.cmd)
    if owner is not None:
        if _pid_alive(owner):
            return False, f"live_owner={owner}"
        return True, f"owner_dead={owner}"

    if not _pid_alive(p.ppid):
        return True, f"ppid_dead={p.ppid}"
    return False, f"live_ppid={p.ppid}"


def reap_orphan_browsers() -> dict:
    procs = _list_browser_procs()
    killed: list[int] = []
    skipped_live = 0
    already_dead = 0

    # Prefer killing process-tree roots first so /T reaps children cleanly.
    # Heuristic: procs whose ppid is not another camoufox/firefox in the set.
    browser_pids = {p.pid for p in procs}
    roots = [p for p in procs if p.ppid not in browser_pids]
    children = [p for p in procs if p.ppid in browser_pids]
    ordered = roots + children

    for p in ordered:
        if p.pid in killed:
            continue
        if not _is_panel_browser(p.cmd) and PROFILE_MARKER not in (p.cmd or ""):
            if "camoufox" not in p.name.lower():
                continue

        if not _pid_alive(p.pid):
            already_dead += 1
            continue

        do_kill, reason = _should_kill(p)
        if not do_kill:
            skipped_live += 1
            continue

        ok = _kill_pid_tree(p.pid)
        if ok:
            killed.append(p.pid)
            _log(f"killed pid={p.pid} name={p.name} ({reason})")
        else:
            # Only log failure if still alive after attempt
            if _pid_alive(p.pid):
                _log(f"kill failed pid={p.pid} name={p.name} ({reason})")
            else:
                killed.append(p.pid)

    profiles = cleanup_stale_profile_dirs()
    return {
        "scanned": len(procs),
        "killed": len(set(killed)),
        "killed_pids": sorted(set(killed)),
        "skipped_live": skipped_live,
        "already_dead": already_dead,
        "profiles_removed": profiles,
    }


def cleanup_stale_profile_dirs() -> int:
    """Remove managed profile dirs whose owner PID is dead."""
    root = os.path.join(tempfile.gettempdir(), PROFILE_MARKER)
    # Also check UserData temp used on this machine
    extra_roots = [
        root,
        r"E:\xiazai\UserData\Temp\grok-register-camoufox",
        os.path.join(r"E:\xiazai\UserData\Temp", PROFILE_MARKER),
    ]
    cleaned = 0
    for base in extra_roots:
        if not base or not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                path = os.path.join(base, entry)
                if not os.path.isdir(path):
                    continue
                m = PROFILE_NAME_RE.fullmatch(entry)
                if not m:
                    continue
                owner = int(m.group(1))
                if _pid_alive(owner):
                    continue
                try:
                    import shutil

                    shutil.rmtree(path, ignore_errors=True)
                    if not os.path.isdir(path):
                        cleaned += 1
                        _log(f"removed stale profile {path}")
                except Exception as exc:
                    _log(f"profile rm fail {path}: {exc}")
        except Exception as exc:
            _log(f"scan profile root fail {base}: {exc}")
    return cleaned


def main() -> int:
    ap = argparse.ArgumentParser(description="Orphan Camoufox reaper for panel")
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--loop", action="store_true", help="loop forever")
    ap.add_argument("--interval", type=int, default=300, help="loop interval seconds")
    args = ap.parse_args()
    if not args.once and not args.loop:
        args.once = True

    def _pass() -> dict:
        res = reap_orphan_browsers()
        _log(
            f"done scanned={res['scanned']} killed={res['killed']} "
            f"profiles={res['profiles_removed']} live_skipped={res['skipped_live']} "
            f"already_dead={res.get('already_dead', 0)}"
        )
        return res

    if args.once and not args.loop:
        _pass()
        return 0

    interval = max(60, int(args.interval or 300))
    _log(f"loop start interval={interval}s")
    while True:
        try:
            _pass()
        except Exception as exc:
            _log(f"pass error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
