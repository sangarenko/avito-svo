#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_bot.py - Telegram-бот @avitosobiralkabot.

Сбор телефонов рекрутёров с Авито в ОДНОМ режиме — ПОИСК
«сво по контракту» (moskva_i_mo?q=сво+по+контракту, «По дате»,
лимит 300 номеров, тег БД search_svo_kontrakt). Категория
«Работа → Военный» не используется: по ней выходило в 5-6 раз
меньше номеров на карточку, чем по поиску. БД пишется ПО ХОДУ
сбора (каждый номер сразу, не в конце).

Расписание:
    * 07:00, 08:00, 09:00, 10:00, 11:00 МСК — ТИХИЙ плановый сбор
      «сво по контракту» (макс 300, бюджет 30 мин) БЕЗ сообщений;
    * 12:00 МСК — эксель-отчёт в чат (ретраи каждую минуту до 12:59);
    * остальное время — тишина (никаких рассылок и вопросов).
    Уже собранное пропускается — прогон добирает только новое.

Кнопки клавиатуры:
    📊 Получить эксель отчёт  — свежий эксель из базы по нажатию;
    🔍 Спарсить «сво по контракту» (поиск) — полный прогон поиска
                               (макс 300 номеров) + эксель;
    ⏹ Остановить парсер      — мягкая остановка (SIGTERM: доканчивает
                               текущую карточку, сохраняет всё).

Вход на Авито выполняется руками в реальном Firefox (профиль
ai-agent), firefox_session_monitor экспортирует сессию в
avito_session.json — от бота входа не требуется.

Прочее:
    * ALLOWED_CHATS — чужие чаты молча игнорируются, базу не раздаём;
    * subprocess в своей группе процессов (killpg по таймауту);
      offset апдейтов персистится в bot_state.json;
    * два прогона невозможны (flock .parser.lock в ff_collect).

State: BASE_DIR/bot_state.json
    offset          - последний обработанный update_id
    last_report     - день, за который отчёт уже отправлен
    report_tries    - неудачные попытки отправки за день daily_date
    collect_log     - {день: [часы], которые уже собраны} (чистим старьё)

Пайплайн: BASE_DIR/{ff_collect,excel_report}.py, БД BASE_DIR/avito.db,
ff_last_run.json (итог прогона ff_collect).
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.environ.get("AVITO_BASE_DIR", "/root/avito-svo")
BOT_STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")
DB_FILE = os.path.join(BASE_DIR, "avito.db")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
TOKEN = "8984620445:AAEv9naITLzEWjqGlWCtP9xSUnEMZyHUbz8"
API = "https://api.telegram.org/bot%s/%%s" % TOKEN
TZ_NAME = "Europe/Moscow"

# --- расписание: тихий сбор 07-11, отчёт в 12:00 -----------------------
COLLECT_HOURS = (7, 8, 9, 10, 11)  # тихий сбор в начале каждого часа
#: во всех часах — поиск «сво по контракту» (категория «Военный» не используется)
SCHEDULE_MODES = {h: "search" for h in COLLECT_HOURS}
#: лимит номеров для планового/ручного прогона поиска
SEARCH_MAX_PHONES = 300
REPORT_HOUR = 12                    # эксель-отчёт в чат
REPORT_RETRY_UNTIL = 59            # минута, до которой ретраим отчёт
MAX_REPORT_TRIES = 10              # потом сдаёмся до завтра (молча)

PARSER_TIMEOUT = 35 * 60           # секунд (плановый сбор ff_collect:
                                   # бюджет 30 мин + запас)
PARSER_TIMEOUT_DEEP = 50 * 60      # секунд (кнопки «Спарсить»:
                                   # глубокий прогон, бюджет 40 мин)
FF_LAST_RUN_FILE = os.path.join(BASE_DIR, "ff_last_run.json")
EXCEL_TIMEOUT = 180                # секунд

BUTTON_EXCEL = "📊 Получить эксель отчёт"
BUTTON_REPARSE = "🔄 Спарсить «Военный» (категория)"
BUTTON_SEARCH = "🔍 Спарсить «сво по контракту» (поиск)"
BUTTON_STOP = "⏹ Остановить парсер"
KEYBOARD = {"keyboard": [[{"text": BUTTON_EXCEL}],
                         [{"text": BUTTON_SEARCH}],
                         [{"text": BUTTON_STOP}]],
            "resize_keyboard": True}

#: файл-флаг «остановлено юзером»: воркеры видят его и молчат
STOP_FLAG = os.path.join(BASE_DIR, ".parser_stop")


def _allowed_chats() -> list:
    """Кому можно получать базу: env AVITO_ALLOWED_CHATS или владелец."""
    raw = (os.environ.get("AVITO_ALLOWED_CHATS") or "").strip()
    if raw:
        try:
            ids = [int(x) for x in raw.replace(" ", "").split(",") if x]
            if ids:
                return ids
        except ValueError:
            pass
    return [431524111]


ALLOWED_CHATS = _allowed_chats()

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover
    TZ = None


def msk_now() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def log(msg: str) -> None:
    print("[%s] %s" % (msk_now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


# ---------------------------------------------------------------------------
# state (потокобезопасный read-modify-write)
# ---------------------------------------------------------------------------

STATE_LOCK = threading.Lock()

# состояние ручного глубокого прогона (кнопка «Спарсить заново»)
PARSE_LOCK = threading.Lock()
PARSE_JOB = {"active": False, "started": None}


def load_state() -> dict:
    try:
        with open(BOT_STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log("save_state: %s" % e)


def update_state(**kw) -> dict:
    """Атомарно (по локу) дописать ключи в state-файл."""
    with STATE_LOCK:
        st = load_state()
        st.update(kw)
        save_state(st)
        return st


def read_last_run() -> dict:
    """Итог последнего прогона парсера (для подписи к авто-экселю)."""
    try:
        with open(LAST_RUN_FILE, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _day_count(day: str) -> int:
    """Сколько объявлений в базе за день (first_seen = день)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True,
                              timeout=5)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM ads WHERE substr(first_seen,1,10)=?",
                (day,)).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# telegram api
# ---------------------------------------------------------------------------

def tg(method: str, **kw) -> dict:
    try:
        r = requests.post(API % method, json=kw, timeout=60)
        return r.json()
    except Exception as e:
        log("tg %s error: %s" % (method, e))
        return {}


def send_doc(chat_id: int, path: str, caption: str = "") -> bool:
    try:
        with open(path, "rb") as f:
            r = requests.post(
                API % "sendDocument",
                data={"chat_id": chat_id, "caption": caption,
                      "reply_markup": json.dumps(KEYBOARD)},
                files={"document": (os.path.basename(path), f)},
                timeout=180)
            return bool(r.json().get("ok"))
    except Exception as e:
        log("send_doc error: %s" % e)
        return False


def send_kb_text(chat_id: int, text: str) -> bool:
    res = tg("sendMessage", chat_id=chat_id, text=text,
             reply_markup=KEYBOARD)
    return bool(res.get("ok"))


# ---------------------------------------------------------------------------
# subprocess-хелперы
# ---------------------------------------------------------------------------

def _ff_last_run() -> dict:
    """JSON последнего прогона ff_collect (ff_last_run.json), или {}."""
    try:
        with open(FF_LAST_RUN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _db_stats() -> dict:
    """Статистика avito.db: всего / за сегодня / с телефонами."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True,
                              timeout=5)
        today = msk_now().strftime("%Y-%m-%d")
        total = con.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        day = con.execute(
            "SELECT COUNT(*) FROM ads WHERE substr(first_seen,1,10)=?",
            (today,)).fetchone()[0]
        phones = con.execute(
            "SELECT COUNT(*) FROM ads WHERE phone IS NOT NULL "
            "AND phone != ''").fetchone()[0]
        con.close()
        return {"total": total, "today": day, "phones": phones}
    except Exception:
        return {}


def _run_subprocess(cmd: list, timeout: int) -> tuple:
    """Запустить процесс в своей группе процессов (killpg по таймауту).

    Возвращает (rc, stdout, хвост stderr). DISPLAY/:1 и AVITO_BASE_DIR
    прокидываются всегда (headed Chromium на VNC).
    """
    env = dict(os.environ)
    env["DISPLAY"] = ":1"          # headed Chromium на VNC
    env["AVITO_BASE_DIR"] = BASE_DIR
    log("запуск: %s" % " ".join(cmd[1:]))
    try:
        p = subprocess.Popen(
            cmd, cwd=BASE_DIR, env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace")
    except Exception as e:
        log("не запустился: %s" % e)
        return -1, "", str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        log("таймаут %ds, убиваем группу процессов" % timeout)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            out, err = p.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        return -1, out or "", (err or "")[-500:]
    return p.returncode, out or "", (err or "")[-500:]


def run_parser(extra_args: list | None = None,
               timeout: int = PARSER_TIMEOUT) -> dict:
    """Запустить ff_collect — сбор поиском «сво по контракту».

    Вход через строку поиска (moskva_i_mo?q=сво+по+контракту),
    сортировка «По дате», обход выдачи ПО ПОРЯДКУ (сверху вниз,
    дубли описаний пропускаются) + страницы пагинации; телефоны —
    из hover-попапов без захода в объявления, лимит номеров и
    бюджет времени задаётся аргументами. Итог читается из
    ff_last_run.json (stdout — живой лог).
    """
    try:                          # зачистить протухший флаг остановки
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except OSError:
        pass
    cmd = [sys.executable, os.path.join(BASE_DIR, "ff_collect.py")] \
        + (extra_args or [])
    log("сбор: ff_collect %s" % " ".join(cmd[2:]))
    rc, out, err = _run_subprocess(cmd, timeout)
    data = _ff_last_run()
    reason = str(data.get("stop_reason") or "")
    already = (rc == 2)
    ok = (rc == 0 and bool(data.get("ok"))
          and reason not in ("no_session",))
    if not ok and not already:
        log("сбор не удался: rc=%s stop_reason=%s stderr=%s"
            % (rc, reason or "?", (err or "")[-300:]))
    return {"ok": ok, "data": data, "rc": rc, "stderr": err,
            "already_running": already}


def build_excel() -> str:
    """Собрать Эксель (excel_report.py); вернуть путь или ''.

    Строго: любая неудача (rc!=0, таймаут, нет JSON/пути) = ''.
    УСТАРЕВШИЙ файл из reports/ НЕ используется - свежая подпись
    не может стоять на старых данных.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    env = dict(os.environ)
    env["AVITO_BASE_DIR"] = BASE_DIR
    cmd = [sys.executable, os.path.join(BASE_DIR, "excel_report.py")]
    try:
        p = subprocess.run(cmd, cwd=BASE_DIR, env=env, timeout=EXCEL_TIMEOUT,
                           capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        log("excel_report: таймаут")
        return ""
    if p.returncode != 0:
        log("excel_report rc=%s: %s" % (p.returncode, (p.stderr or "")[-300:]))
        return ""
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"path"' in line:
            try:
                if json.loads(line).get("path"):
                    return json.loads(line)["path"]
            except ValueError:
                continue
    log("excel_report: нет JSON с пути в stdout")
    return ""


# ---------------------------------------------------------------------------
# кнопка «Остановить парсер»: мягкая остановка сбора
# ---------------------------------------------------------------------------

def _stop_parser_procs() -> int:
    """Остановить ff_collect и его Firefox. Возвращает число убитых.

    Порядок: SIGTERM (мягко — ff_collect доканчивает карточку,
    сохраняет БД и сессию) → пауза → SIGKILL выжившим.
    ВАЖНО: убивается только Firefox коллектора (профиль ff_profile);
    реальный Firefox юзера (ai-agent) и монитор не трогаются.
    """
    killed = 0
    me = os.getpid()
    pids = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", "ff_collect\\.py"],
            capture_output=True, text=True, timeout=10).stdout
        pids = [int(p) for p in out.split() if p.strip().isdigit()]
    except Exception:
        pids = []
    for pid in pids:
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    try:      # Firefox коллектора (только ff_profile!)
        subprocess.run(["pkill", "-TERM", "-f", "firefox.*ff_profile"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    if killed:
        time.sleep(15)            # время на мягкое завершение
        for pid in pids:          # добить зависших
            if pid == me:
                continue
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                log("парсер %d не завершился — SIGKILL" % pid)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        try:
            subprocess.run(["pkill", "-KILL", "-f",
                            "firefox.*ff_profile"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    return killed


def _handle_stop(chat_id: int) -> None:
    """Кнопка ⏹: флаг тишины для воркеров + остановка процессов."""
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(msk_now().isoformat(timespec="seconds"))
    except OSError:
        pass
    n = _stop_parser_procs()
    if n > 0:
        send_kb_text(chat_id, "⏹ Парсер останавливаю (доканчивает "
                              "текущую карточку и сохраняет базу — "
                              "пара секунд).")
    else:
        try:
            if os.path.exists(STOP_FLAG):
                os.remove(STOP_FLAG)
        except OSError:
            pass
        send_kb_text(chat_id, "Парсер не запущен — останавливать "
                              "нечего.")


def _stopped_by_user() -> bool:
    """Был ли установлен флаг остановки; снимает его."""
    try:
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# кнопки «Спарсить»: полный глубокий прогон + эксель в чат
# (два режима: категория «Военный» и поиск «сво по контракту»)
# ---------------------------------------------------------------------------

def _reparse_worker(chat_id: int) -> None:
    """Фоновый поток: сбор ff_collect (вся категория «Военный») +
    свежий эксель + итоговая подпись. Никаких промежуточных сообщений."""
    try:
        res = run_parser(extra_args=["--time-budget", "2400"],
                         timeout=PARSER_TIMEOUT_DEEP)
        if _stopped_by_user():
            send_kb_text(chat_id, "⏹ Сбор остановлен. Собранное до "
                                  "этого места — в базе; эксель — "
                                  "кнопкой «Получить эксель отчёт».")
            return
        d = res.get("data") or {}
        if res.get("already_running"):
            send_kb_text(chat_id, "⏳ Сбор уже выполняется — дождись "
                                  "завершения, эксель придёт сам.")
            return
        if not res["ok"]:
            reason = d.get("stop_reason") or ("rc=%s" % res.get("rc"))
            send_kb_text(chat_id, "⚠️ Сбор не удался (%s). "
                                  "Попробуй ещё раз позже."
                                  % str(reason)[:80])
            return
        path = build_excel()
        phones = d.get("phones") or []
        st_ = _db_stats()
        caption = ("avito · Работа→Военный · вся категория · страниц: %s · "
                   "объявлений: %s · номеров: %d · база: %s"
                   % (d.get("pages"), d.get("total_ads"), len(phones),
                      st_.get("total")))
        if phones:
            head = ", ".join(phones[:12])
            if len(phones) > 12:
                head += " …и ещё %d" % (len(phones) - 12)
            caption += "\nномера: " + head
        if path:
            send_doc(chat_id, path, caption=caption)
        else:
            send_kb_text(chat_id, "Сбор прошёл (телефонов %d), но эксель "
                                  "не собрался — нажми «Получить эксель "
                                  "отчёт»." % len(phones))
    except Exception as e:
        log("reparse_worker: %s" % e)
        try:
            send_kb_text(chat_id, "⚠️ Полный прогон сломался: %s"
                                  % str(e)[:80])
        except Exception:
            pass
    finally:
        with PARSE_LOCK:
            PARSE_JOB["active"] = False


def _handle_reparse(chat_id: int) -> None:
    with PARSE_LOCK:
        if PARSE_JOB.get("active"):
            send_kb_text(chat_id, "⏳ Полный сбор уже идёт (запущен %s) — "
                                  "дождись экселя по завершению."
                                  % (PARSE_JOB.get("started") or "?"))
            return
        PARSE_JOB["active"] = True
        PARSE_JOB["started"] = msk_now().strftime("%H:%M")
    send_kb_text(chat_id, "🔄 Запускаю сбор: Работа → Военный → по дате "
                          "→ Москва и МО → ВСЯ категория (доскролл "
                          "каждой страницы + все страницы, 10–40 мин) — "
                          "эксель пришлю по завершению. Каждый номер "
                          "пишется в базу сразу.")
    threading.Thread(target=_reparse_worker, args=(chat_id,),
                     daemon=True).start()


def _search_worker(chat_id: int) -> None:
    """Фоновый поток: сбор ff_collect в режиме ПОИСКА «сво по
    контракту» (макс 300 номеров) + свежий эксель + подпись."""
    try:
        _free_mem_for_run("ручной поиск")
        res = run_parser(
            extra_args=["--mode", "search",
                        "--max-phones", str(SEARCH_MAX_PHONES),
                        "--time-budget", "2400"],
            timeout=PARSER_TIMEOUT_DEEP)
        if _stopped_by_user():
            send_kb_text(chat_id, "⏹ Сбор остановлен. Собранное до "
                                  "этого места — уже в базе; эксель — "
                                  "кнопкой «Получить эксель отчёт».")
            return
        d = res.get("data") or {}
        if res.get("already_running"):
            send_kb_text(chat_id, "⏳ Сбор уже выполняется — дождись "
                                  "завершения, эксель придёт сам.")
            return
        if not res["ok"]:
            reason = d.get("stop_reason") or ("rc=%s" % res.get("rc"))
            send_kb_text(chat_id, "⚠️ Поиск не удался (%s). "
                                  "Попробуй ещё раз позже."
                                  % str(reason)[:80])
            return
        path = build_excel()
        phones = d.get("phones") or []
        st_ = _db_stats()
        caption = ("avito · поиск «сво по контракту» · по дате · макс %s · "
                   "страниц: %s · объявлений: %s · номеров: %d · база: %s"
                   % (SEARCH_MAX_PHONES, d.get("pages"),
                      d.get("total_ads"), len(phones), st_.get("total")))
        if phones:
            head = ", ".join(phones[:12])
            if len(phones) > 12:
                head += " …и ещё %d" % (len(phones) - 12)
            caption += "\nномера: " + head
        if path:
            send_doc(chat_id, path, caption=caption)
        else:
            send_kb_text(chat_id, "Поиск прошёл (телефонов %d), но эксель "
                                  "не собрался — нажми «Получить эксель "
                                  "отчёт»." % len(phones))
    except Exception as e:
        log("search_worker: %s" % e)
        try:
            send_kb_text(chat_id, "⚠️ Прогон поиска сломался: %s"
                                  % str(e)[:80])
        except Exception:
            pass
    finally:
        with PARSE_LOCK:
            PARSE_JOB["active"] = False


def _handle_search(chat_id: int) -> None:
    with PARSE_LOCK:
        if PARSE_JOB.get("active"):
            send_kb_text(chat_id, "⏳ Полный сбор уже идёт (запущен %s) — "
                                  "дождись экселя по завершению."
                                  % (PARSE_JOB.get("started") or "?"))
            return
        PARSE_JOB["active"] = True
        PARSE_JOB["started"] = msk_now().strftime("%H:%M")
    send_kb_text(chat_id, "🔍 Запускаю сбор: поиск Авито «сво по "
                          "контракту» → Москва и МО → «По дате» → "
                          "обход выдачи с телефонами из попапов "
                          "(макс %s номеров, 10–40 мин). Уже знакомые "
                          "объявления проскакиваю быстро. Эксель пришлю "
                          "по завершению." % SEARCH_MAX_PHONES)
    threading.Thread(target=_search_worker, args=(chat_id,),
                     daemon=True).start()


# ---------------------------------------------------------------------------
# ежедневный поток: тихий сбор 07-11 (только поиск) + эксель-отчёт в 12:00
# ---------------------------------------------------------------------------

def _prune_collect_log(st: dict, today: str) -> dict:
    """Держим в collect_log только последние 7 дней."""
    cl = st.get("collect_log") or {}
    if not isinstance(cl, dict):
        cl = {}
    days = sorted(cl.keys())
    for d in days[:-7]:
        if d < today:
            cl.pop(d, None)
    return cl


def _free_mem_for_run(tag: str = "") -> None:
    """Перед прогоном убедиться, что памяти хватает (~2 ГБ).

    Посторонний Firefox (профиль ai-agent) может отъедать 0.5-0.8 ГБ,
    а контент-процесс сбора распухает до ~1.9 ГБ — при нехватке
    earlyoom убивает именно сбор. При нехватке прибираем браузер
    ai-agent (SIGTERM): ai-agent сам его пересоздаёт, для него это
    безопасно.
    """
    try:
        avail = 0
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable"):
                    avail = int(ln.split()[1]) // 1024
                    break
        if avail >= 2000:
            return
        ps = subprocess.run(["ps", "-eo", "pid,args"],
                            capture_output=True, text=True, timeout=10)
        pids = []
        for ln in ps.stdout.splitlines():
            args = ln.strip()
            if ("/root/.mozilla/firefox/ai-agent" in args
                    or "persistent_browser_firefox.py" in args):
                try:
                    pids.append(int(args.split(None, 1)[0]))
                except ValueError:
                    pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if pids:
            time.sleep(3)
            log("память перед прогоном%s: %d МБ — прибрал браузер "
                "ai-agent (PID %s), он пересоздастся сам"
                % ((" " + tag) if tag else "", avail,
                   ",".join(str(p) for p in pids)))
        else:
            log("память перед прогоном%s: %d МБ — прибирать нечего, "
                "идём как есть (swap докроет)"
                % ((" " + tag) if tag else "", avail))
    except Exception as e:
        log("free_mem: %s" % e)


def _collect_step(now: datetime) -> None:
    """Один тихий плановый сбор (начало часа 07-11).

    Режим: поиск «сво по контракту» (макс 300) во всех часах —
    категорию «Военный» не собираем.
    """
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    done = (st.get("collect_log") or {}).get(today) or []
    if now.hour in done:
        return
    with PARSE_LOCK:
        manual_active = bool(PARSE_JOB.get("active"))
    if manual_active:
        log("плановый сбор %02d:00 пропущен — идёт ручной полный прогон"
            % now.hour)
        return
    mode = SCHEDULE_MODES.get(now.hour, "category")
    extra = ["--time-budget", "1800"]
    if mode == "search":
        extra = ["--mode", "search",
                 "--max-phones", str(SEARCH_MAX_PHONES),
                 "--time-budget", "1800"]
    label = ("поиск «сво по контракту» (макс %s)"
             % SEARCH_MAX_PHONES) if mode == "search" \
        else "категория «Военный»"
    log("тихий плановый сбор %s %02d:00 — %s" % (today, now.hour, label))
    _free_mem_for_run("%02d:00" % now.hour)
    res = run_parser(extra_args=extra)
    _stopped_by_user()          # плановый сбор молчит и про остановку
    if res["ok"]:
        d = res["data"]
        log("плановый сбор %02d:00 (%s) ок: телефонов %d, объявлений %s, "
            "новых в базу %s"
            % (now.hour, mode, len(d.get("phones") or []),
               d.get("total_ads"), d.get("new")))
        # БЕЗ внешнего STATE_LOCK — update_state() берёт его сам;
        # вложенный не-реентерабельный лок замораживает оба потока
        st = load_state()
        cl = _prune_collect_log(st, today)
        done = cl.get(today) or []
        if now.hour not in done:
            done.append(now.hour)
        cl[today] = done
        update_state(collect_log=cl)
    else:
        # не помечаем час сделанным — на следующем часу повторится
        # (последняя попытка 11:00); дальше день закрывает отчёт 12:00
        log("плановый сбор %02d:00 (%s) не удался — повторю в следующий час"
            % (now.hour, mode))


def _report_step(now: datetime) -> None:
    """Эксель-отчёт в 12:00 (ретраи каждую минуту до 12:59)."""
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    if st.get("last_report") == today:
        return
    tries = st.get("report_tries") or 0
    if st.get("report_date") != today:
        tries = 0
    if tries >= MAX_REPORT_TRIES:
        return                    # сдались (молча) — не спамим до завтра
    if now.hour == REPORT_HOUR and now.minute > REPORT_RETRY_UNTIL \
            and tries > 0:
        return

    st_ = _db_stats()
    caption = ("avito · поиск «сво по контракту» · %s · за день: %s · "
               "с телефонами: %s · всего: %s"
               % (now.strftime("%d.%m"), st_.get("today"),
                  st_.get("phones"), st_.get("total")))
    path = build_excel()
    delivered = False
    if path:
        for chat_id in ALLOWED_CHATS:
            if send_doc(chat_id, path, caption=caption):
                delivered = True
            else:
                log("отчёт не доставлен в чат %s" % chat_id)
    if delivered:
        log("эксель-отчёт доставлен: %s (за день %s, с телефонами %s, "
            "всего %s)"
            % (os.path.basename(path), st_.get("today"),
               st_.get("phones"), st_.get("total")))
        update_state(last_report=today, report_date=today,
                     report_tries=0)
    else:
        tries += 1
        log("отчёт %s не собрался/не ушёл (попытка %d/%d)"
            % (today, tries, MAX_REPORT_TRIES))
        update_state(report_date=today, report_tries=tries)


def _daily_step() -> None:
    """Один шаг цикла ежедневного потока (вызывается из daily_worker)."""
    now = msk_now()
    # тихий сбор в начале каждого часа 07..11 (все часы —
    # только поиск «сво по контракту»)
    if now.hour in COLLECT_HOURS and now.minute < 5:
        _collect_step(now)
        time.sleep(120)
        return
    # отчёт в 12:00 (и ретро-досыл при позднем старте бота)
    if now.hour >= REPORT_HOUR:
        _report_step(now)
    time.sleep(60)


def daily_worker() -> None:
    """Фоновый поток: кнопка в главном потоке отвечает всегда."""
    log("поток расписания запущен: тихий сбор 07-11 — только поиск "
        "«сво по контракту» (макс 300), отчёт в 12:00")
    while True:
        try:
            _daily_step()
        except Exception as e:
            log("daily_worker: %s" % e)
            time.sleep(60)


def process_update(upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    if chat_id not in ALLOWED_CHATS:
        return                          # чужие - молчание, базу не раздаём
    text = (msg.get("text") or "").strip()
    if not text:
        return
    low = text.lower()

    # --- /войти: вход выполняется в реальном Firefox, боту не нужен ---
    if low.startswith("/войти") or low.startswith("/login"):
        send_kb_text(chat_id,
                     "Вход на Авито выполняется в реальном Firefox на "
                     "сервере (профиль ai-agent, виден через noVNC): "
                     "залогинься там — монитор автоматически экспортирует "
                     "сессию для сбора, от бота ничего не нужно.")
        return

    # --- кнопка «Остановить парсер» ---
    if text == BUTTON_STOP or low.startswith("/стоп") \
            or low.startswith("/stop") or low.startswith("/останов"):
        _handle_stop(chat_id)
        return

    # --- «Военный» не собираем: только поиск «сво по контракту» ---
    if text == BUTTON_REPARSE or low.startswith("/спарсить") \
            or low.startswith("/перепарсить") or low.startswith("/parse") \
            or low.startswith("/reparse") or low.startswith("/военный"):
        send_kb_text(chat_id,
                     "ℹ️ Категорию «Военный» не собираем: по ней выходит "
                     "в 5-6 раз меньше номеров, чем по поиску. "
                     "Собираем только поиск «сво по контракту» — "
                     "кнопка 🔍 или команда /поиск.")
        return

    # --- кнопка/команда прогона поиска «сво по контракту» ---
    if text == BUTTON_SEARCH or low.startswith("/поиск") \
            or low.startswith("/search") or low.startswith("/контракт") \
            or low.startswith("/сво"):
        _handle_search(chat_id)
        return

    # --- эксель по нажатию ---
    if text == BUTTON_EXCEL or low.startswith("/start") \
            or low.startswith("/эксель") or low.startswith("/excel"):
        path = build_excel()
        if path:
            lr = read_last_run()
            send_doc(chat_id, path,
                     caption="avito · контракт сво · %s · всего: %s"
                             % (msk_now().strftime("%d.%m %H:%M"),
                                lr.get("db_total")))
        else:
            send_kb_text(chat_id, "⚠️ Эксель не собрался, попробуй ещё раз")
    # всё остальное - молчание


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def main() -> int:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tg("setMyCommands", commands=[])      # убрать старое меню команд
    me = tg("getMe")
    log("бот %s стартовал (сбор: поиск «сво по контракту» "
        "(макс 300), БД по ходу прогона, тихий сбор 07-11 + отчёт-"
        "эксель в 12:00, защита памяти перед прогонами, чаты: %s)"
        % (me.get("result", {}).get("username", "?"),
           ",".join(str(c) for c in ALLOWED_CHATS)))
    threading.Thread(target=daily_worker, daemon=True).start()
    offset = load_state().get("offset")
    while True:
        try:
            res = requests.get(
                API % "getUpdates",
                params={"timeout": 25, "offset": offset}, timeout=35)
            for upd in res.json().get("result", []):
                offset = upd["update_id"] + 1
                update_state(offset=offset)
                try:
                    process_update(upd)
                except Exception as e:
                    log("process_update: %s" % e)
        except Exception as e:
            log("getUpdates: %s" % e)
            time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
