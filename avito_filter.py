#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_filter.py - модуль отсева АВТО-мусора.

Два уровня защиты:
    1. ВКЛАДКИ: is_auto_tab(slug) - парсер НИКОГДА не открывает вкладки
       рубрикатора про транспорт (Автомобили, Мото, Запчасти, Гаражи...).
       Обход вкладок живёт в коллекторе, запрет - здесь;
    2. ОБЪЯВЛЕНИЯ: looks_like_auto(title, url, category) - промо-объявления
       из автокатегорий Авито подмешивает в выдачу «не-авто» вкладок
       («ВАЗ (LADA) Largus» на 1-й странице вакансий);
       такие items в БД не пишутся и карточки их не открываются.

Метки согласованы так, чтобы НЕ задевать военные/контрактные объявления:
рус. морфология суффиксная, поэтому маркеры матчатся только в НАЧАЛЕ
слова («водител» ловит «водитель/водителя», но «руль» больше не ловит
«патРУЛЬный» - живая вакансия «Патрульный периметра аэродрома на сво»).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 1. Запретные ВКЛАДКИ рубрикатора (верхний уровень, слаги Авито)
# ---------------------------------------------------------------------------

#: вкладки рубрикатора, куда парсер не заходит НИКОГДА
AUTO_TAB_SLUGS = {
    "avtomobili", "transport", "mototsikly", "mototsikly_i_mototehnika",
    "gruzoviki", "gruzoviki_i_spetstehnika", "spetstehnika",
    "vodnyy_transport", "lodki", "zapchasti", "zapchasti_i_aksessuary",
    "shiny", "diski", "kolesa", "shiny_diski_i_kolesa", "kvadrotsikly",
    "quadratsikly", "snegohody", "trailery", "pritsepy", "vnedorozhniki",
    "karshering", "gruzoperevozki", "mototehnika", "velosipedy",
    "garazhi_i_mashinomesta", "avto", "auto",
}

#: подстроки для страховки от новых имён транспортных вкладок
_AUTO_TAB_PARTS = (
    "avtomobil", "mototsikl", "mototehnika", "gruzovik", "spetsteh",
    "vodnyy_transport", "zapchast", "shiny", "diski", "kolesa",
    "kvadrotsikl", "quadratsikl", "snegohod", "trailer", "pritsep",
    "vnedorozhnik", "karshering", "gruzoperevoz", "velosiped", "garazh",
    "mashinomesto", "lodk", "kater", "yacht",
)

#: слова в НАЗВАНИИ вкладки, означающие транспорт
AUTO_TAB_TITLES = (
    "авто", "автомобил", "мотоцикл", "мототехн", "грузовик", "спецтехн",
    "транспорт", "запчаст", "водный", "гараж", "машиноместо", "велосипед",
    "прицеп", "шины", "диски",
)


def is_auto_tab(slug: str, title: str = "") -> bool:
    """Запретная ли вкладка: парсер в неё не заходит вообще."""
    s = (slug or "").lower().strip("/")
    t = (title or "").lower()
    if not s and not t:
        return False
    if s and (s in AUTO_TAB_SLUGS
              or any(p in s for p in _AUTO_TAB_PARTS)):
        return True
    if t and any(m in t for m in AUTO_TAB_TITLES):
        return True
    return False


# ---------------------------------------------------------------------------
# 2. Фильтр ОБЪЯВЛЕНИЙ (промо-мусор, подмешанный в выдачу вкладки)
# ---------------------------------------------------------------------------

#: слаги автокатегорий в ссылках карточек
AUTO_URL_SLUGS = (
    "avtomobili", "zapchasti", "zapchasti_i_aksessuary", "mototsikly",
    "gruzoviki", "gruzoviki_spetstehnika", "spetstehnika", "vodnyy_transport",
    "lodki", "shiny", "diski", "kolesa", "shiny_diski_i_kolesa",
    "quadratsikly", "kvadrotsikly", "snegohody", "trailery", "pritsepy",
    "vnedorozhniki", "karshering", "gruzoperevozki", "mototehnika",
    "mototsikly_i_mototehnika", "gruzoviki_i_spetstehnika", "transport",
    "velosipedy", "garazhi_i_mashinomesta",
)

#: маркеры автоконтента в заголовках (между водительскими вакансиями,
#: транспортом, запчастями)
AUTO_TITLE_MARKERS = (
    # водительские вакансии и транспортные услуги
    "водител", "шофер", "шофёр", "перегон", "автовоз", "дальнобой",
    "такси", "эвакуатор", "машинист", "тракторист", "автовыкуп",
    "автоподбор", "автосервис", "автомойк", "автосалон", "гараж",
    "разборк", "спецтехник",
    # транспорт
    "автомобил", "мотоцикл", "скутер", "квадроцикл", "снегоход",
    "гидроцикл", "мопед", "багги", "трактор", "мотоблок",
    "минитрактор", "прицеп", "полуприцеп", "тонар", "лодочн", "катер", "яхт",
    # запчасти и агрегаты
    "запчаст", "двигател", "мотор", "кпп", "акпп", "мкпп", "вариатор",
    "коробк передач", "кузов", "бампер", "капот", "крыло", "пороги",
    "фара", "фары", "птф", "противотуманк", "стекло", "лобово",
    "турбин", "турбо", "шрус", "шаровая", "амортизатор", "пружин",
    "торпед", "панел прибор", "сидень", "обшивк", "руль", "маховик",
    "сцеплен", "радиатор", "интеркулер", "глушител", "выхлоп", "ступиц",
    "тормоз", "суппорт", "колодк", "грм", "шкив", "генератор", "стартер",
    "проводк", "зеркал", "стеклоподъ", "багажник", "рейлинг", "фаркоп",
    "буксировочн", "шины", "покрышк", "резин", "колеса", "колёса",
    "колесо", "шипован", "литые диск", "штамповк", "акб", "аккумулятор",
    "форсунк", "инжектор", "карбюратор", "дроссель", "коленвал",
    "распредвал", "гбц", "поршн", "прокладк", "сальник", "помп",
    "термостат", "лямбд", "полуос", "картер", "муфт", "гбо", "лебедк",
    "автохими", "автокосметик", "автоэмаль", "автокресл",
)

#: бренды машин (границы слова)
_AUTO_BRANDS = (
    "ваз|лада|калина|гранта|приора|веста|нива|шевроле|камаз|маз|зил|уаз|"
    "газел|газон|собол|буханк|тойота|ниссан|мицубиси|хонда|мазда|субару|"
    "сузуки|хендай|хёндай|хюндай|киа|форд|фольксваген|ауди|шкода|бмв|"
    "мерседес|бенц|опель|рено|пежо|ситроен|вольво|лексус|инфинити|джип|"
    "порше|фиат|чери|джили|хавал|эксеед|чанган|дэу|дастер|логан|сандеро|"
    "солярис|крета|камри|королла|лансер|прадо|патрол|теана|церато|"
    "спортейдж|тигуан|джетта|пасат|lada|kalina|granta|priora|vesta|niva|"
    "chevrolet|kamaz|zil|uaz|gazelle|toyota|nissan|mitsubishi|honda|"
    "mazda|subaru|suzuki|hyundai|kia|ford|volkswagen|vw|audi|skoda|bmw|"
    "mercedes|benz|opel|renault|peugeot|citroen|volvo|lexus|infiniti|"
    "jeep|porsche|fiat|chery|geely|haval|exeed|changan|daewoo|duster|"
    "logan|sandero|solaris|creta|camry|corolla|lancer|prado|patrol|"
    "teana|cerato|sportage|tiguan|jetta|passat"
)
AUTO_BRAND_RE = re.compile(
    r"(?:^|[^а-яёa-z])(" + _AUTO_BRANDS + r")(?:$|[^а-яёa-z])",
    re.IGNORECASE)
_AUTO_URL_RE = re.compile(
    r"/(" + "|".join(AUTO_URL_SLUGS) + r")/", re.IGNORECASE)
#: маркеры матчатся только в НАЧАЛЕ слова (см. докстринг модуля)
_AUTO_TITLE_RE = re.compile(
    r"(?:^|[^а-яёa-z])(" + "|".join(
        re.escape(_m) for _m in AUTO_TITLE_MARKERS) + r")",
    re.IGNORECASE)


def looks_like_auto(title: str, url: str = "", category: str = "") -> bool:
    """Похоже ли объявление на автотранспорт/запчасти/водительскую вакансию."""
    t = (title or "").lower()
    u = (url or "").lower()
    c = (category or "").lower()
    if _AUTO_URL_RE.search(u):
        return True
    for slug in AUTO_URL_SLUGS:
        if slug in c:
            return True
    if _AUTO_TITLE_RE.search(t):
        return True
    if AUTO_BRAND_RE.search(t):
        return True
    return False
