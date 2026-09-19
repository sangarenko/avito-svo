#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_db.py — модуль базы данных (SQLite): ads / sellers / runs.

Схема толерантная: базовые таблицы CREATE IF NOT EXISTS, недостающие
колонки добавляются ALTER'ом с игнором «duplicate column» — тот же код
работает и с легаси-базой сервера, и с чистой. drop_auto() вычищает
авто-мусор, случайно накопившийся до внедрения фильтра.
"""

from __future__ import annotations

import sqlite3

from avito_common import log, now_iso
from avito_filter import looks_like_auto

#: доп. колонки таблицы ads (мигрируются ALTER'ом)
ADS_EXTRA = [
    ("phone", "TEXT"),
    ("phone_status", "TEXT"),
    ("phone_checked_at", "TEXT"),
    ("seller_name", "TEXT"),
    ("seller_type", "TEXT"),
    ("seller_url", "TEXT"),
    ("seller_rating", "TEXT"),
    ("seller_reviews", "INTEGER"),
    ("seller_avito_ads", "INTEGER"),
    ("is_mass", "INTEGER DEFAULT 0"),
    # сниппет карточки SERP (серые строки условий) — «Описание» в экселе
    ("description", "TEXT"),
]


class DB:
    """Tolerant SQLite wrapper around the `ads` / `sellers` / `runs` tables.

    The schema self-migrates: base tables are CREATE IF NOT EXISTS, extra
    columns are added with ALTER TABLE and "duplicate column name" errors
    are silently ignored, so the same code works against the legacy server
    DB and a fresh one.
    """

    #: колонки базовой таблицы ads, добавляемые ALTER'ом при отсутствии
    _ADS_BASE = [
        ("title", "TEXT"),
        ("price", "TEXT"),
        ("url", "TEXT"),
        ("region", "TEXT"),
        ("category", "TEXT DEFAULT 'svo'"),
        ("first_seen", "TEXT"),
        ("last_seen", "TEXT"),
    ]

    def __init__(self, path: str):
        """Connect (creating if needed) and ensure the schema is current.

        check_same_thread=False so tg_bot can reuse one connection from
        handler threads; sqlite timeout=30 tolerates concurrent readers.
        """
        self.path = path
        self.con = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            # WAL: чтение эксель-отчёта не блокируется записью парсера
            self.con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.ensure_schema()
        self.cols = self._introspect("ads")

    # -- schema ---------------------------------------------------------------

    def _introspect(self, table: str) -> list:
        """Return the list of column names of `table` (empty on error)."""
        try:
            cur = self.con.execute("PRAGMA table_info(%s)" % table)
            return [r["name"] for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    def _alter_tolerant(self, table: str, col: str, decl: str) -> None:
        """ALTER TABLE ... ADD COLUMN, ignoring duplicate-column errors."""
        try:
            self.con.execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg:
                return
            raise

    def ensure_schema(self) -> None:
        """Create tables if missing and add missing columns tolerantly."""
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY,
                title TEXT,
                price TEXT,
                url TEXT,
                region TEXT,
                category TEXT DEFAULT 'svo',
                first_seen TEXT,
                last_seen TEXT
            )""")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS sellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                type TEXT,
                url TEXT,
                rating TEXT,
                reviews INTEGER,
                avito_ads INTEGER,
                ads_in_db INTEGER,
                is_mass INTEGER DEFAULT 0,
                first_seen TEXT
            )""")
        # legacy server DB has an old sellers schema (seller_key /
        # seller_name / items_in_db ...) which can't host the v4 aggregate;
        # the table is fully rebuilt from ads by refresh_sellers(), so
        # dropping it is safe (no source data is lost).
        s_cols = self._introspect("sellers")
        if s_cols and "name" not in s_cols:
            try:
                log("sellers: legacy schema (%s) — rebuilding from ads"
                    % ",".join(s_cols[:5]))
                self.con.execute("DROP TABLE sellers")
                self.con.execute(
                    """CREATE TABLE sellers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        type TEXT,
                        url TEXT,
                        rating TEXT,
                        reviews INTEGER,
                        avito_ads INTEGER,
                        ads_in_db INTEGER,
                        is_mass INTEGER DEFAULT 0,
                        first_seen TEXT
                    )""")
            except sqlite3.Error as e:
                log("sellers rebuild failed: %s" % e)
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                pages INTEGER DEFAULT 0,
                found INTEGER,
                new INTEGER,
                cards INTEGER,
                phones_ok INTEGER,
                phones_login_required INTEGER,
                no_button INTEGER,
                captchas_solved INTEGER,
                stop_reason TEXT,
                login_state INTEGER
            )""")
        # tolerate legacy DBs missing even some base columns
        for col, decl in self._ADS_BASE:
            if col not in self._introspect("ads"):
                self._alter_tolerant("ads", col, decl)
        for col, decl in ADS_EXTRA:
            if col not in self._introspect("ads"):
                self._alter_tolerant("ads", col, decl)
        self.con.commit()

    # -- ads ------------------------------------------------------------------

    def upsert_ad(self, d: dict) -> str:
        """Insert or refresh an ad row; returns 'new' or 'old'.

        On conflict the ad keeps its original first_seen and gets
        last_seen/price/title refreshed.
        """
        ad_id = d["id"]
        existed = self.con.execute(
            "SELECT 1 FROM ads WHERE id=?", (ad_id,)).fetchone() is not None
        ts = now_iso()
        self.con.execute(
            """INSERT INTO ads(id, title, price, url, region, category,
                               first_seen, last_seen)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   last_seen = excluded.last_seen,
                   price = COALESCE(excluded.price, price),
                   title = COALESCE(excluded.title, title)""",
            (ad_id, d.get("title"), d.get("price"), d.get("url"),
             d.get("region"), d.get("category", "svo"), ts, ts))
        self.con.commit()
        return "old" if existed else "new"

    def update_card(self, ad_id: int, fields: dict) -> None:
        """Update card-scraped columns of one ad.

        Only keys that actually exist as `ads` columns are written (the
        schema is tolerant, so a column may legitimately be absent).
        phone_checked_at is stamped automatically whenever phone_status
        is present in `fields`.
        """
        sets, vals = [], []
        for k, v in fields.items():
            if k in self.cols:
                sets.append("%s=?" % k)
                vals.append(v)
        if "phone_status" in fields and "phone_checked_at" in self.cols \
                and "phone_checked_at" not in fields:
            sets.append("phone_checked_at=?")
            vals.append(now_iso())
        if not sets:
            return
        vals.append(ad_id)
        self.con.execute(
            "UPDATE ads SET %s WHERE id=?" % ", ".join(sets), vals)
        self.con.commit()

    def get_ads(self, only_without_phone: bool = True,
                retry_auth: bool = False, limit: int | None = None) -> list:
        """Select ads for card scraping.

        only_without_phone - only rows with empty phone;
        retry_auth         - additionally require phone_status='login_required';
        limit              - optional LIMIT.
        Rows come back as dicts, newest first (ORDER BY first_seen DESC).
        """
        q = "SELECT * FROM ads"
        conds = []
        if only_without_phone:
            conds.append("(phone IS NULL OR phone='')")
        if retry_auth:
            conds.append("phone_status='login_required'")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        # id DESC as tie-breaker: ads inserted in the same second (same
        # first_seen) still come out in a deterministic newest-first order
        q += " ORDER BY first_seen DESC, id DESC"
        if limit:
            q += " LIMIT %d" % int(limit)
        return [dict(r) for r in self.con.execute(q).fetchall()]

    def drop_auto(self) -> int:
        """Удалить из `ads` транспортный мусор; вернуть число удалённых.

        Чистит объявления, похожие на машины/запчасти/водительские вакансии
        (looks_like_auto) — защиту «даже если в БД осталось» от мусора,
        накопившегося до внедрения фильтра. Если колонки category в старой
        схеме нет — читаем без неё. Ничего не добавляет, только удаляет.
        """
        try:
            if "category" in self.cols:
                rows = self.con.execute(
                    "SELECT id, title, url, category FROM ads").fetchall()
            else:  # старая схема без колонки category
                rows = self.con.execute(
                    "SELECT id, title, url FROM ads").fetchall()
        except sqlite3.Error as e:
            log("drop_auto: select failed: %s" % e)
            return 0
        victims = []
        for r in rows:
            d = dict(r)
            if looks_like_auto(d.get("title") or "", d.get("url") or "",
                               d.get("category") or ""):
                victims.append(d["id"])
        if not victims:
            return 0
        self.con.executemany("DELETE FROM ads WHERE id=?",
                             [(v,) for v in victims])
        self.con.commit()
        return len(victims)

    # -- stats / sellers / runs ------------------------------------------------

    def stats(self) -> dict:
        """Aggregated counters used by reports and the telegram bot."""
        def one(sql: str):
            return self.con.execute(sql).fetchone()[0]

        return {
            "total": one("SELECT COUNT(*) FROM ads"),
            "with_phone": one(
                "SELECT COUNT(*) FROM ads WHERE phone IS NOT NULL AND phone<>''"),
            "login_required": one(
                "SELECT COUNT(*) FROM ads WHERE phone_status='login_required'"),
            "no_button": one(
                "SELECT COUNT(*) FROM ads WHERE phone_status='no_button'"),
            "pending": one(
                "SELECT COUNT(*) FROM ads WHERE "
                "(phone_status IS NULL OR phone_status='') "
                "AND (phone IS NULL OR phone='')"),
            "sellers": one(
                "SELECT COUNT(DISTINCT seller_name) FROM ads "
                "WHERE seller_name IS NOT NULL AND seller_name<>''"),
            "mass": one("SELECT COUNT(*) FROM ads WHERE is_mass=1"),
        }

    def refresh_sellers(self) -> None:
        """Rebuild the `sellers` aggregate table from ads.seller_* columns.

        A seller is marked is_mass when they have >= 5 ads in the DB;
        the same flag is then written back onto the ads rows.
        Атомарно: при ошибке любого шага — rollback, чтобы частичный
        DELETE («sellers пустые») никогда не коммитился наружу.
        """
        try:
            self.con.execute("DELETE FROM sellers")
            self.con.execute(
                """INSERT INTO sellers(name, type, url, rating, reviews,
                                       avito_ads, ads_in_db, is_mass, first_seen)
                   SELECT seller_name,
                          MAX(seller_type),
                          MAX(seller_url),
                          MAX(seller_rating),
                          MAX(seller_reviews),
                          MAX(seller_avito_ads),
                          COUNT(*)          AS ads_in_db,
                          CASE WHEN COUNT(*) >= 5 THEN 1 ELSE 0 END,
                          MIN(first_seen)
                   FROM ads
                   WHERE seller_name IS NOT NULL AND seller_name<>''
                   GROUP BY seller_name""")
            self.con.execute(
                """UPDATE ads SET is_mass =
                    CASE WHEN (SELECT s.ads_in_db FROM sellers s
                               WHERE s.name = ads.seller_name) >= 5
                         THEN 1 ELSE 0 END
                    WHERE seller_name IS NOT NULL AND seller_name<>''""")
            self.con.commit()
        except Exception:
            try:
                self.con.rollback()
            except Exception:
                pass
            raise

    def record_run(self, s: dict) -> None:
        """Insert one row into `runs` (keys filtered to existing columns)."""
        cols = self._introspect("runs")
        s = dict(s)
        s.setdefault("ts", now_iso())
        keys = [k for k in s if k in cols]
        if not keys:
            return
        self.con.execute(
            "INSERT INTO runs(%s) VALUES(%s)"
            % (",".join(keys), ",".join(["?"] * len(keys))),
            [s[k] for k in keys])
        self.con.commit()

    def close(self) -> None:
        """Commit and close the connection."""
        try:
            self.con.commit()
        finally:
            self.con.close()
