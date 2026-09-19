#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_geo.py — модуль города/региона парсера Авито («контракт сво»).

Отвечает за ГЕОграфию запроса:
    * поиск идёт ПО ВСЕЙ РОССИИ — база выдачи /all (канонический адрес,
      сюда же Авито редиректит /rossiya; в гео-URL город не подставляется,
      чтобы выдача не «схлопывалась» в один город);
    * строит канонические URL поиска и URL ВКЛАДОК категорий
      (/all/vakansii?q=...&s=104): из ссылок Авито вырезается мусор
      (context=, cd=1, utm), сохраняются только q и сортировка s=104
      «по дате» — смешивание старых/новых ликвидировано именно здесь;
    * знает слаги популярных городов (для будущего сбора по городу):
      city_url("krasnodar", "vakansii") -> /krasnodar/vakansii?...

Всё чистые функции над строками — модуль тестируется без браузера.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlsplit, urlunsplit

#: Поисковый запрос.
SEARCH_QUERY = "контракт сво"

#: База выдачи «вся Россия» (каноническая; /rossiya редиректит сюда).
ALL_RUSSIA_BASE = "all"

#: Сортировка «по дате» (s=104) — новые сверху. ЖЁСТКО держим её в
#: каждом URL: Авито при редиректах любит её терять, из-за чего к новым
#: подмешивались старые.
DATE_SORT = "104"

#: Канонический URL поиска по всей России.
SEARCH_URL = "https://www.avito.ru/%s?q=%s&s=%s" % (
    ALL_RUSSIA_BASE, quote_plus(SEARCH_QUERY), DATE_SORT)

#: URL страницы-рубрикатора (cd=1): смешанная выдача НЕ редиректится в
#: доминантную категорию, сверху — полный рубрикатор вкладок. Используется
#: для ОБНАРУЖЕНИЯ вкладок (результаты с авто-промо лежат ниже вьюпорта —
#: на VNC не мелькают; сами items с этой страницы НЕ собираются).
RUBRICATOR_URL = "https://www.avito.ru/%s?cd=1&q=%s&s=%s" % (
    ALL_RUSSIA_BASE, quote_plus(SEARCH_QUERY), DATE_SORT)

#: Слаги популярных городов — для будущего сбора по конкретному городу.
CITY_SLUGS = {
    "moskva": "Москва",
    "sankt-peterburg": "Санкт-Петербург",
    "krasnodar": "Краснодар",
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "nizhniy_novgorod": "Нижний Новгород",
    "kazan": "Казань",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "rostov": "Ростов-на-Дону",
    "ufa": "Уфа",
    "krasnoyarsk": "Красноярск",
    "voronezh": "Воронеж",
    "perm": "Пермь",
    "volgograd": "Волгоград",
    "sochi": "Сочи",
}


def canonical_search_url(city: str | None = None) -> str:
    """Канонический URL поиска (вся Россия или конкретный город)."""
    base = (city or ALL_RUSSIA_BASE).strip("/") or ALL_RUSSIA_BASE
    return "https://www.avito.ru/%s?q=%s&s=%s" % (
        base, quote_plus(SEARCH_QUERY), DATE_SORT)


def tab_url(slug: str | None, pageno: int = 1) -> str:
    """Канонический URL ВКЛАДКИ категории выдачи, страница pageno.

    slug=None/"" — смешанная выдача (без вкладки). Сохраняем только
    q и s=104; cd=1/context/utm вырезаются (мусор из ссылок Авито).
    """
    base = ALL_RUSSIA_BASE
    s = (slug or "").strip("/")
    if s and s != ALL_RUSSIA_BASE:
        base = "%s/%s" % (ALL_RUSSIA_BASE, s)
    url = "https://www.avito.ru/%s?q=%s&s=%s" % (
        base, quote_plus(SEARCH_QUERY), DATE_SORT)
    if pageno and pageno > 1:
        url += "&p=%d" % pageno
    return url


def serp_slug(url: str) -> str:
    """Слаг вкладки (категории) из URL выдачи; "" = смешанная выдача.

    /all/vakansii?q=...      -> "vakansii"
    /all?q=...               -> ""            (смешанная)
    /krasnodar/vakansii?...  -> "vakansii"    (город не важен)
    """
    try:
        path = urlsplit(url or "").path
    except ValueError:
        return ""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    # пропускаем первый сегмент, если это город/страна (не «all»):
    # в гео-выдаче /krasnodar/vakansii категория — второй сегмент
    if parts[0] in (ALL_RUSSIA_BASE, "rossiya") or parts[0] in CITY_SLUGS:
        parts = parts[1:]
    if not parts:
        return ""
    slug = parts[0]
    # служебные страницы не считаем вкладками выдачи
    if slug in ("my", "favorites", "login", "blocked", "s"):
        return ""
    return slug


def clean_query_params(href: str) -> dict:
    """Параметры ссылки Авито без мусора (context, cd, utm, list)."""
    try:
        query = urlsplit(href or "").query
    except ValueError:
        return {}
    out = {}
    for part in query.split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k in ("q", "s", "p", "cd"):
            out[k] = v
    return out
