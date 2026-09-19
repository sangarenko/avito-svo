#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api_helper.py — JSON-API управления сбором (консоль/SSH).

Наружу сервер ничего не слушает — доступ только по root-SSH.

Подкоманды (argv[1]):
  status  — состояние: идёт ли сбор, статистика базы (всего/за дни),
            последний прогон (ff_last_run.json), последние объявления,
            хвост живого журнала ff_collect.log;
  collect — запустить сбор сейчас (ff_collect: Firefox + сессия
            из avito_session.json, nohup, в фоне; env как у
            systemd: DISPLAY, AVITO_PROXY). По умолчанию — поиск
            «сво по контракту» через строку поиска (лимит 300
            номеров); опционально: api_helper.py collect
            [search|category] [max_phones]. Если сбор уже идёт —
            {"ok": true, "already_running": true};
  stop    — остановить идущий сбор (мягко: SIGTERM, данные
            сохраняются); если сбор не идёт — {"ok": true,
            "not_running": true};
  report  — собрать свежий Эксель (excel_report.py) и отдать base64.

stdout = ровно ОДНА строка JSON.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE_DIR = os.environ.get("AVITO_BASE_DIR", "/root/avito-svo")
DB_PATH = os.path.join(BASE_DIR, "avito.db")
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")
#: итог прогона ff_collect (Firefox-коллектор «Работа → Военный»)
FF_LAST_RUN_FILE = os.path.join(BASE_DIR, "ff_last_run.json")
PARSER_LOG = os.path.join(BASE_DIR, "parser.log")
#: живой журнал ff_collect
FF_LOG = os.path.join(BASE_DIR, "ff_collect.log")
SESSION_FILE = os.path.join(BASE_DIR, "avito_session.json")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
RUN_LOG = os.path.join(BASE_DIR, "ff_collect.log")
#: файл-флаг «сбор на паузе» (ручной вход юзера на Авито)
PAUSE_FLAG = os.path.join(BASE_DIR, ".parser_paused")

#: окружение ручного запуска коллектора — как у systemd-юнита
#: (proxy.conf: AVITO_PROXY=socks5://127.0.0.1:10808, DISPLAY=:1)
LAUNCH_ENV = {
    "DISPLAY": ":1",
    "AVITO_PROXY": "socks5://127.0.0.1:10808",
}

try:
    from zoneinfo import ZoneInfo
    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover
    _MSK = None


def _now() -> datetime:
    return datetime.now(_MSK) if _MSK else datetime.utcnow() + timedelta(hours=3)


def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parser_running() -> bool:
    """Идёт ли сбор: держит ли кто-то flock .parser.lock.

    Это тот же лок, который берёт коллектор; -w 1 — гонка двух
    status-запросов не даёт ложного «running».
    """
    lock = os.path.join(BASE_DIR, ".parser.lock")
    try:
        r = subprocess.run(["flock", "-w", "1", lock, "-c", "true"],
                           capture_output=True, timeout=10)
        return r.returncode != 0
    except Exception:
        return False


def _bot_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "avito-svo-bot"],
                           capture_output=True, timeout=10, text=True)
        return (r.stdout or "").strip() == "active"
    except Exception:
        return False


def _log_tail(n: int = 40) -> list:
    """Хвост живого журнала коллектора (ff_collect.log, затем parser.log)."""
    for path in (FF_LOG, PARSER_LOG):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            if lines:
                return lines[-n:]
        except Exception:
            continue
    return []


def cmd_status() -> None:
    db = {}
    recent = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True,
                              timeout=5)
        con.row_factory = sqlite3.Row
        today = _now().strftime("%Y-%m-%d")
        db["total"] = con.execute("SELECT COUNT(*) c FROM ads").fetchone()["c"]
        db["today"] = con.execute(
            "SELECT COUNT(*) c FROM ads WHERE substr(first_seen,1,10)=?",
            (today,)).fetchone()["c"]
        db["with_phone"] = con.execute(
            "SELECT COUNT(*) c FROM ads WHERE phone IS NOT NULL "
            "AND phone != ''").fetchone()["c"]
        db["sellers"] = con.execute(
            "SELECT COUNT(*) c FROM sellers").fetchone()["c"]
        per_day = []
        for r in con.execute(
                "SELECT substr(first_seen,1,10) d, COUNT(*) c FROM ads "
                "GROUP BY d ORDER BY d DESC LIMIT 10"):
            per_day.append({"day": r["d"], "count": r["c"]})
        db["per_day"] = per_day
        for r in con.execute(
                "SELECT id, title, url, first_seen, phone, phone_status "
                "FROM ads ORDER BY first_seen DESC, id DESC LIMIT 12"):
            recent.append(dict(r))
        con.close()
    except Exception as e:
        db = {"error": str(e)}

    _out({
        "ok": True,
        "now": _now().isoformat(),
        "running": _parser_running(),
        "paused": os.path.exists(PAUSE_FLAG),
        "bot_active": _bot_active(),
        "login_saved": os.path.exists(SESSION_FILE),
        "last_run": _read_json(FF_LAST_RUN_FILE)
                   or _read_json(LAST_RUN_FILE),
        "db": db,
        "recent": recent,
        "log_tail": _log_tail(),
    })


def cmd_collect() -> None:
    if os.path.exists(PAUSE_FLAG):
        _out({"ok": False, "paused": True,
              "error": "сбор на паузе: идёт ручной вход на Авито "
                       "(уберите .parser_paused для возобновления)"})
        return
    if _parser_running():
        _out({"ok": True, "already_running": True})
        return
    # режим сбора: search (поиск «сво по контракту», основной) или
    # category (категория «Военный», резервный)
    mode = (sys.argv[2] if len(sys.argv) > 2 else "search").strip().lower()
    if mode not in ("category", "search"):
        mode = "search"
    try:
        max_phones = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    except ValueError:
        max_phones = 300
    # ff_collect: Firefox + сессия из avito_session.json, бюджет
    # 40 мин (поиск — с лимитом номеров); без сессии сам откажется
    # с no_session
    cmd = [sys.executable, "-u",
           os.path.join(BASE_DIR, "ff_collect.py"),
           "--time-budget", "2400"]
    if mode == "search":
        cmd += ["--mode", "search", "--max-phones", str(max_phones)]
    env = dict(os.environ)
    env.update(LAUNCH_ENV)
    try:
        with open(RUN_LOG, "ab") as lf:
            subprocess.Popen(
                cmd, cwd=BASE_DIR, env=env, stdout=lf, stderr=lf,
                stdin=subprocess.DEVNULL,
                start_new_session=True)
        _out({"ok": True, "started": True, "mode": mode})
    except Exception as e:
        _out({"ok": False, "error": str(e)})


def cmd_stop() -> None:
    """Остановить сбор (SIGTERM).

    SIGTERM — ff_collect завершается мягко (доканчивает
    карточку, сохраняет БД и сессию); через 8с выжившим SIGKILL.
    Firefox юзера (профиль ai-agent) не трогается — только
    ff_profile коллектора.
    """
    if not _parser_running():
        _out({"ok": True, "not_running": True})
        return
    pids = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", "ff_collect\\.py"],
            capture_output=True, text=True, timeout=10).stdout
        pids = [int(p) for p in out.split() if p.strip().isdigit()]
    except Exception:
        pids = []
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    try:
        subprocess.run(["pkill", "-TERM", "-f", "firefox.*ff_profile"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    if killed:
        time.sleep(15)
        for pid in pids:
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            subprocess.run(["pkill", "-KILL", "-f",
                            "firefox.*ff_profile"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    _out({"ok": True, "stopped": True, "killed": killed})


def cmd_report() -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    env = dict(os.environ)
    env["AVITO_BASE_DIR"] = BASE_DIR
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "excel_report.py")],
            cwd=BASE_DIR, env=env, timeout=180,
            capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        _out({"ok": False, "error": "excel_report: таймаут"})
        return
    except Exception as e:
        _out({"ok": False, "error": str(e)})
        return
    if p.returncode != 0:
        _out({"ok": False, "error": "excel_report rc=%s: %s"
              % (p.returncode, (p.stderr or "")[-300:])})
        return
    data = None
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"path"' in line:
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if not data or not data.get("path"):
        _out({"ok": False, "error": "excel_report: нет JSON с путём"})
        return
    try:
        with open(data["path"], "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        _out({"ok": False, "error": "читаю xlsx: %s" % e})
        return
    _out({
        "ok": True,
        "filename": os.path.basename(data["path"]),
        "rows": data.get("rows"),
        "sheets": data.get("sheets"),
        "b64": b64,
    })


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    try:
        if cmd == "status":
            cmd_status()
        elif cmd == "collect":
            cmd_collect()
        elif cmd == "stop":
            cmd_stop()
        elif cmd == "report":
            cmd_report()
        else:
            _out({"ok": False, "error": "unknown command: %r" % cmd})
            return 2
    except Exception as e:
        _out({"ok": False, "error": str(e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
