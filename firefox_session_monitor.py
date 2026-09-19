#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firefox_session_monitor.py - авто-экспорт сессии Авито из реального Firefox.

Контекст: владелец логинится на Авито руками в антидетект-Firefox
(/root/.mozilla/firefox/ai-agent - настоящий Firefox без протоколов
автоматизации, Win10-spoof через user.js, весь трафик через SOCKS5
127.0.0.1:10808). Парсер же ждёт сессию в avito_session.json в формате
playwright storage_state.

Скрипт каждые 15 секунд снимает копию cookies.sqlite профиля (Firefox
держит её залоченной - читаем копию), ищет куки логина Авито и при их
появлении экспортирует ВСЕ avito-куки в avito_session.json + пишет
login_state.json. Браузер не трогает, в ТГ не пишет, ничего не убивает.

Запуск (постоянный systemd-юнит avito-ff-monitor.service):
  Environment=HOLD_SECONDS=31536000 (≈год), Restart=always.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time

PROFILE = "/root/.mozilla/firefox/ai-agent"
BASE_DIR = "/root/avito-svo"
SESSION_FILE = os.path.join(BASE_DIR, "avito_session.json")
LOGIN_STATE_FILE = os.path.join(BASE_DIR, "login_state.json")
SNAP = "/tmp/ff_cookies_snap.sqlite"

#: куки, которые Авито ставит ТОЛЬКО вошедшему юзеру
#: (набор из avito_classify._LOGIN_COOKIES + расширение)
LOGIN_COOKIES = {"auth", "sessid", "sessionid", "avito_user", "userid",
                 "login", "sess", "avito_user_id"}

#: moz_cookies.sameSite -> playwright storage_state
SAME_SITE = {0: "Lax", 1: "None", 2: "Lax", 3: "Strict"}

POLL_S = 15.0
HOLD_S = int(os.environ.get("HOLD_SECONDS", "31536000"))  # ≈год


def log(msg: str) -> None:
    print("[ff-monitor %s] %s" % (time.strftime("%H:%M:%S"), msg),
          flush=True)


def firefox_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "firefo[x].*--profile"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def snapshot_cookies() -> bool:
    """Скопировать cookies.sqlite (+wal/shm) в /tmp; True при успехе."""
    ok = True
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(PROFILE, "cookies.sqlite" + suffix)
        dst = SNAP + suffix
        try:
            if os.path.exists(src):
                shutil.copy2(src, dst)
            else:
                os.path.exists(dst) and os.remove(dst)
        except FileNotFoundError:
            pass
        except Exception:
            ok = False  # середина записи Firefox - снимем на следующем такте
    return ok


def read_avito_rows():
    """Все avito-куки из снимка: [(name,value,host,path,expiry,sec,http,ss)]."""
    con = sqlite3.connect(SNAP, timeout=5)
    try:
        return con.execute(
            "SELECT name, value, host, path, expiry, isSecure, "
            "isHttpOnly, sameSite FROM moz_cookies "
            "WHERE host LIKE '%avito%' AND value != ''").fetchall()
    finally:
        con.close()


def is_logged_in(rows) -> bool:
    names = {(r[0] or "").lower() for r in rows}
    return bool(names & LOGIN_COOKIES)


def rows_hash(rows) -> str:
    """Хеш набора кук (имя+значение+срок) - детект ротации сессии.

    Пока куки в реальном Firefox не меняются - файл сессии НЕ трогаем:
    его мог обновить ff_collect более свежей ротацией (парсер тоже
    живёт с этой сессией). Экспортируем только реальное изменение.
    """
    h = hashlib.md5()
    for r in sorted(rows, key=lambda x: (x[0] or "", x[2] or "")):
        h.update(("%s=%s;%s" % (r[0], r[1], r[4])).encode(
            "utf-8", "replace"))
    return h.hexdigest()


def export_session(rows) -> int:
    """Сконвертировать куки в playwright storage_state и записать атомарно."""
    cookies = []
    for name, value, host, path, expiry, secure, http, ss in rows:
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "expires": float(expiry) if expiry else -1,
            "httpOnly": bool(http),
            "secure": bool(secure),
            "sameSite": SAME_SITE.get(ss, "Lax"),
        })
    state = {"cookies": cookies, "origins": []}
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, SESSION_FILE)
    return len(cookies)


def write_login_state(logged_in: bool, n_cookies: int = 0) -> None:
    try:
        tmp = LOGIN_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"logged_in": logged_in,
                       "cookies": n_cookies,
                       "source": "firefox",
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S+03:00")},
                      f, ensure_ascii=False)
        os.replace(tmp, LOGIN_STATE_FILE)
    except OSError:
        pass


def main() -> int:
    log("старт: слежу за входом на Авито в Firefox (профиль %s)" % PROFILE)
    log("сессия будет экспортирована в %s" % SESSION_FILE)
    deadline = time.time() + HOLD_S
    announced = False      # «вход зафиксирован» уже логировали
    logged_off = False     # логаут после входа
    last_hash = ""         # хеш последнего экспорта (детект ротации)
    ff_closed_noted = False

    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            if not snapshot_cookies():
                continue
            rows = read_avito_rows()
            logged = is_logged_in(rows)
            h = rows_hash(rows)
            if logged and not announced:
                n = export_session(rows)
                write_login_state(True, n)
                announced = True
                logged_off = False
                last_hash = h
                log("ВХОД ЗАФИКСИРОВАН: экспортировано %d avito-кук -> %s"
                    % (n, SESSION_FILE))
                continue
            if logged and announced and h != last_hash:
                # куки в реальном Firefox реально изменились (ротация
                # при живом браузере) - экспортируем свежие; без
                # изменений файл не трогаем (его обновляет и ff_collect)
                n = export_session(rows)
                write_login_state(True, n)
                last_hash = h
                log("сессия обновлена (%d кук, ротация в браузере)" % n)
                continue
            if not logged and announced and not logged_off:
                logged_off = True
                write_login_state(False)
                log("куки логина пропали (выход?) - сессия осталась в файле, "
                    "но login_state=False")
            if not announced and not logged_off:
                write_login_state(False, len(rows))
        except sqlite3.DatabaseError as e:
            # кривой снимок (Firefox писал в момент копирования) - пропускаем
            log("снимок не читается (%s), пропускаю такт" % e)
        except Exception as e:
            log("ошибка такта: %s" % e)

        if not firefox_running():
            if not ff_closed_noted:
                ff_closed_noted = True
                log("Firefox закрыт - продолжаю следить (жду следующий вход)")
        else:
            ff_closed_noted = False

    log("дедлайн (%d ч) - выхожу, сессия остаётся в файле" % (HOLD_S // 3600))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
