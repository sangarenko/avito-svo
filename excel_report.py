#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
excel_report.py - Эксель-отчёт из avito.db (openpyxl).

Формат отчёта:
  * только источники проекта: сбор через категорию «Военный» ИЛИ
    поиск «сво по контракту», и ТОЛЬКО строки с телефоном
    (строки без номеров и чужие категории не попадают);
  * колонки: Название · Телефон · Ссылка · Источник · Одинаковых
    вакансий · Описание — последняя ШИРОКАЯ, текст сниппета
    карточки (серые строки условий/обязанностей), читаётся
    навскидку; «Источник» — откуда вакансия: категория «Военный»
    или поиск «сво по контракту»;
  * «Одинаковых вакансий» — сколько в базе объявлений с тем же
    текстом (клоны-спам одного рекрутера, нормализация как в
    ff_collect.norm_title); 1 = уникальная;
  * 4-я колонка «Вакансий компании» ПОДГОТОВЛЕНА, но ВЫКЛЮЧЕНА
    (включение: env AVITO_COMPANY_COL=1 у юнита avito-svo-bot);
  * каждый день - ОТДЕЛЬНЫЙ лист с именем даты «17.09», «16.09»...,
    листы ПО УБЫВАНИЮ, внутри листа сортировка по дате (свежие сверху).

Последняя строка stdout - JSON {"path": ..., "rows": ..., "sheets": [...],
"companies": N} (лишние ключи потребителями игнорируются).
"""

from __future__ import annotations

import json
import os
import re
import sys
from difflib import SequenceMatcher

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from avito_lib import BASE_DIR, DB, msk_now  # noqa: E402

REPORTS_DIR = os.path.join(BASE_DIR, "reports")

HEAD_FILL = PatternFill("solid", fgColor="00AAFF")
HEAD_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="0563C1", underline="single")

#: 4-я колонка «Вакансий компании»: сделана, но по умолчанию ВЫКЛЮЧЕНА
SHOW_COMPANY = os.environ.get("AVITO_COMPANY_COL") == "1"

HEADERS = ["Название", "Телефон", "Ссылка", "Источник",
           "Одинаковых вакансий"]
WIDTHS = [55, 18, 50, 24, 12]
if SHOW_COMPANY:
    HEADERS = HEADERS + ["Вакансий компании"]
    WIDTHS = WIDTHS + [14]
# «Описание» — ВСЕГДА последняя, широкая, «вправо до конца»
HEADERS = HEADERS + ["Описание"]
WIDTHS = WIDTHS + [110]
DESC_COL = len(HEADERS)          # номер колонки описания
#: номер колонки «Одинаковых вакансий» (сдвигается «Источником»)
CLONES_COL = HEADERS.index("Одинаковых вакансий") + 1
#: сколько символов умещается в строку колонки описания (~ширина)
_DESC_CHARS_PER_LINE = 105

#: источники проекта (всё остальное — легаси): сбор через
#: категорию «Военный» и поиск «сво по контракту»
CATEGORIES = ("rabota_voennyi", "search_svo_kontrakt")
CATEGORY_LABELS = {
    "rabota_voennyi": "«Военный» (категория)",
    "search_svo_kontrakt": "Поиск «сво по контракту»",
}

#: слова-шум в именах компаний (правовые формы и пр.) — при сравнении
#: «похожих» компаний не учитываются
_COMPANY_NOISE = {
    "ооо", "оао", "зао", "пао", "ао", "ип", "чп", "гк", "общество",
    "с", "компания", "бренд", "брэнд", "brand", "ltd", "llc", "inc",
    "co", "company", "офис", "центр",
}


def _norm_company(name: str) -> str:
    """Нормализованное имя компании для сравнения «похожести».

    Нижний регистр, ё→е, пунктуация выкидывается, правовые формы
    (ООО/ИП/...) и прочий шум не учитываются, слова сортируются —
    «Служба ZV ООО» и «ZV служба» дают одинаковый ключ.
    """
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`()\[\]{}.,;:!?*#№/\\\-–—_]+", " ", s)
    toks = [t for t in s.split() if t and t not in _COMPANY_NOISE]
    return " ".join(sorted(set(toks)))


def _company_counts(db) -> tuple:
    """(seller_name -> сколько вакансий этой компании во всей базе,
    число уникальных компаний-кластеров).

    Похожие компании склеиваются в одну:
      1) точное совпадение нормализованных имён (порядок слов и
         правовые формы не важны: «Служба ZV ООО» == «ZV служба»);
      2) подмножество токенов («Консалт Плюс» <= «Консалт плюс Москва»);
      3) нечёткая склейка похожих ключей (SequenceMatcher >= 0.8).
    Счёт по кластеру суммируется — «уже похожие» компании отсеяны,
    число честное (это и есть доп-услуга: сколько вакансий у компании).
    """
    rows = db.con.execute(
        "SELECT seller_name, COUNT(*) AS c FROM ads "
        "WHERE seller_name IS NOT NULL AND seller_name<>'' "
        "GROUP BY seller_name").fetchall()
    counts = {dict(r)["seller_name"]: dict(r)["c"] for r in rows}
    if not counts:
        return {}, 0

    # 1) точная группировка по нормализованному имени
    groups: dict = {}
    for name in counts:
        key = _norm_company(name)
        if not key:               # имя целиком из «шума» — не склеиваем
            key = "\x00" + name
        groups.setdefault(key, []).append(name)

    # 2)+3) жадная склейка кластеров: подмножество токенов или ratio>=0.8
    clusters = [[k] for k in groups]

    def _tokset(cl: list) -> set:
        s: set = set()
        for k in cl:
            if k.startswith("\x00"):
                return set()          # «шумовые» имена не склеиваем
            s |= set(k.split())
        return s

    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                ta, tb = _tokset(clusters[i]), _tokset(clusters[j])
                similar = bool(ta) and bool(tb) and (
                    ta <= tb or tb <= ta
                    or SequenceMatcher(
                        None, " ".join(sorted(ta)),
                        " ".join(sorted(tb))).ratio() >= 0.8)
                if similar:
                    clusters[i].extend(clusters[j])
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break

    # счёт по кластеру = сумма по всем вариациям имени
    out = {}
    for cl in clusters:
        total = sum(counts[n] for k in cl for n in groups[k])
        for k in cl:
            for n in groups[k]:
                out[n] = total
    return out, len(clusters)


def _norm_title(s: str) -> str:
    """Нормализация текста вакансии — КАК В ff_collect.norm_title
    (нижний регистр, без пунктуации): «Одинаковых вакансий» считает
    клоны тем же ключом, которым коллектор их отсеивает."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (s or "").lower()).strip()


def _clone_counts(db) -> dict:
    """norm_title -> сколько объявлений с этим текстом в проектных
    источниках (включая клоны со статусом dup_title и саму строку).

    «Защитник неба в составе группы мог» × 45 → в экселе будет 45:
    видно, сколько спама наделал рекрутер вокруг одной вакансии.
    """
    rows = db.con.execute(
        "SELECT title FROM ads WHERE category IN (%s)"
        % ",".join("?" * len(CATEGORIES)),
        CATEGORIES).fetchall()
    counts: dict = {}
    for r in rows:
        nt = _norm_title(r[0] or "")
        if nt:
            counts[nt] = counts.get(nt, 0) + 1
    return counts


def _clean_url(url: str) -> str:
    """Ссылка без трекинг-хвоста (?context=H4sIA...): кликабельна и коротка."""
    return (url or "").split("?")[0].split("#")[0]


def _phone_cell(d: dict) -> str:
    """Телефон для ячейки: номер либо честная причина его отсутствия."""
    phone = d.get("phone") or ""
    if phone:
        return phone
    if d.get("phone_status") == "login_required":
        return "нужен вход на Авито"
    return ""


def _descr_cell(d: dict) -> str:
    """Текст колонки «Описание».

    Приоритет: сниппет карточки (серые строки условий/обязанностей,
    собирается ff_collect) → зарплата · город → название.
    """
    descr = (d.get("description") or "").strip()
    if descr:
        return descr[:500]
    parts = []
    for k in ("price", "location"):
        v = (d.get(k) or "").strip()
        if v:
            parts.append(v)
    if parts:
        return " · ".join(parts)
    return (d.get("title") or "").strip()


def _fill_day_sheet(ws, rows: list, company_counts: dict,
                    clone_counts: dict) -> int:
    """Заполнить один дневной лист; вернуть число строк."""
    ws.append(HEADERS)
    for c, _ in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        d = dict(r)
        url = _clean_url(d.get("url"))
        descr = _descr_cell(d)
        clones = clone_counts.get(_norm_title(d.get("title") or ""), 1)
        src = CATEGORY_LABELS.get(d.get("category") or "", "")
        row = [d.get("title") or "", d.get("phone") or "", url, src,
               clones]
        if SHOW_COMPANY:
            row.append(company_counts.get(d.get("seller_name") or "", 0))
        row.append(descr)
        ws.append(row)
        row_i = ws.max_row
        if url:
            link_cell = ws.cell(row=row_i, column=3)
            link_cell.hyperlink = url
            link_cell.font = LINK_FONT
        # «Одинаковых вакансий» — по центру
        ws.cell(row=row_i, column=CLONES_COL).alignment = Alignment(
            horizontal="center", vertical="top")
        # «Описание» — во всю ширину, перенос, высота под текст
        dc = ws.cell(row=row_i, column=DESC_COL)
        dc.alignment = Alignment(wrap_text=True, vertical="top")
        lines = max(1, -(-len(descr) // _DESC_CHARS_PER_LINE))
        ws.row_dimensions[row_i].height = max(
            15, 14 * min(lines, 5))

    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:%s%d" % (
        get_column_letter(len(HEADERS)), max(ws.max_row, 1))
    return ws.max_row - 1


def _sheet_names(dates: list) -> dict:
    """Имя листа для каждой даты: «17.09» (день.месяц, дата МСК)."""
    names = {}
    for d in dates:
        day = d[8:10].lstrip("0") or "?"
        names[d] = "%s.%s" % (day, d[5:7])
    return names


def build(db_path: str) -> tuple:
    db = DB(db_path)
    # пересобираем агрегат продавцов — данные для счётчика вакансий
    # компаний всегда свежие (даже пока колонка выключена)
    db.refresh_sellers()
    company_counts, n_companies = _company_counts(db)
    wb = Workbook()

    rows = db.con.execute(
        """SELECT id, title, url, first_seen, phone, phone_status,
                  seller_name, price, location, description, category
           FROM ads
           WHERE category IN (%s)
             AND phone IS NOT NULL AND phone <> ''
           ORDER BY first_seen DESC, id DESC"""
        % ",".join("?" * len(CATEGORIES)),
        CATEGORIES).fetchall()

    # сколько одинаковых объявлений у каждой вакансии (клоны+спам)
    clone_counts = _clone_counts(db)

    # ---------- разбивка по дням (дата МСК из first_seen) ----------
    groups: dict = {}
    for r in rows:
        d = dict(r)
        key = (d.get("first_seen") or "")[:10] or "без-даты"
        groups.setdefault(key, []).append(d)

    dates = sorted((k for k in groups if k != "без-даты"), reverse=True)
    names = _sheet_names(dates)

    sheet_titles = []
    first_sheet = True
    total_rows = 0
    for dk in dates:
        title = names[dk]
        ws = wb.active
        if first_sheet:
            ws.title = title
            first_sheet = False
        else:
            ws = wb.create_sheet(title)
        sheet_titles.append(title)
        total_rows += _fill_day_sheet(ws, groups[dk], company_counts,
                                      clone_counts)

    # объявления без даты - в конец
    if "без-даты" in groups:
        ws = wb.create_sheet("без даты") if sheet_titles else wb.active
        if not sheet_titles:
            ws.title = "без даты"
        sheet_titles.append("без даты")
        total_rows += _fill_day_sheet(ws, groups["без-даты"],
                                      company_counts, clone_counts)

    # пустая база: дефолтный пустой лист переименовываем в «Нет данных»
    if not sheet_titles:
        ws = wb.active
        ws.title = "Нет данных"
        ws.append(HEADERS)
        sheet_titles.append("Нет данных")

    db.close()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(
        REPORTS_DIR, "avito_svo_%s.xlsx" % msk_now().strftime("%Y%m%d_%H%M%S"))
    wb.save(path)
    # атомарная замена latest: не бывает полу-записанного файла
    latest = os.path.join(REPORTS_DIR, "avito_svo_latest.xlsx")
    tmp = latest + ".tmp"
    try:
        wb.save(tmp)
        os.replace(tmp, latest)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path, total_rows, sheet_titles, n_companies


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        BASE_DIR, "avito.db")
    try:
        path, rows, sheets, companies = build(db_path)
        print(json.dumps({"path": path, "rows": rows, "sheets": sheets,
                          "companies": companies}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"path": "", "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
