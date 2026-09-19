#!/usr/bin/env node
/* ff_browser_daemon.js — резидентный браузер Авито.
 *
 * Браузер живёт ПОСТОЯННО (systemd avito-svo-browser, DISPLAY=:1 —
 * вкладку видно в noVNC) с открытой вкладкой поиска «сво по
 * контракту». Прогоны ff_collect подключаются к этому браузеру
 * (resident_endpoint() читает browser.ws) и закрывают только свои
 * вкладки — сам браузер и вкладка-демон живут дальше. Постоянно
 * открытый браузер вместо «новый Firefox на каждый прогон» —
 * меньше блокировок «Доступ ограничен» и капч со стороны Авито.
 *
 * Что делает демон:
 *   - launchServer (порт 9334, прокси socks5 Москва, антидетект-префы),
 *     ws-эндпоинт -> browser.ws, heartbeat -> browser.heartbeat (45 c);
 *   - вкладка-«печка»: поиск Авито, лёгкий скролл ~5 мин, мягкий
 *     reload ~20 мин (пока не идёт сбор — pgrep ff_collect),
 *     попапы «Хорошо/Понятно/Принять/Всё верно» закрываются;
 *   - куки логина из avito_session.json при старте + при изменении
 *     файла (свежий логин подхватывается без рестарта);
 *   - storage_state -> avito_session.json ~10 мин (в простое);
 *   - пересоздание контекста ежедневно ~04:30 МСК (гигиена памяти),
 *     сам браузер-процесс НЕ перезапускается.
 * Фолбэк в ff_collect остаётся: демон умер -> свой persistent-профиль.
 */
'use strict';

const DRIVER_PKG =
  '/usr/local/lib/python3.12/dist-packages/playwright/driver/package';
const fs = require('fs');
const net = require('net');
const path = require('path');
const child = require('child_process');
const { firefox } = require(DRIVER_PKG);

const BASE = '/root/avito-svo';
const WS_FILE = path.join(BASE, 'browser.ws');
const HEARTBEAT = path.join(BASE, 'browser.heartbeat');
const SESSION_FILE = path.join(BASE, 'avito_session.json');
const LOG_FILE = path.join(BASE, 'browser_daemon.log');
const PORT = 9334;

const UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) '
            + 'Gecko/20100101 Firefox/128.0');
const PROXY = 'socks5://127.0.0.1:10808';
const SEARCH_QUERY = 'сво по контракту';
const SEARCH_URL = ('https://www.avito.ru/moskva_i_mo?q='
                    + encodeURIComponent(SEARCH_QUERY));
const HOME_URL = 'https://www.avito.ru/';

const LOGIN_COOKIES = ['auth', 'sessid', 'sessionid', 'avito_user',
                       'userid', 'login', 'sess', 'avito_user_id'];

const FF_PREFS = {
  'general.platform.override': 'Win32',
  'privacy.trackingprotection.enabled': false,
  'browser.safebrowsing.malware.enabled': false,
  'browser.safebrowsing.phishing.enabled': false,
  'toolkit.telemetry.enabled': false,
  'datareporting.healthreport.uploadEnabled': false,
  'media.peerconnection.enabled': false,
  'browser.shell.checkDefaultBrowser': false,
};

const INIT_JS = (
  "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
  + "Object.defineProperty(navigator,'platform',{get:()=>'Win32'});");

function log(msg) {
  const line = '[' + new Date().toLocaleString('ru-RU') + '] ' + msg;
  console.log(line);
  try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch (e) {}
}

function touch(file, data) {
  try {
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, data === undefined ? String(Date.now()) : data);
    fs.renameSync(tmp, file);
  } catch (e) {}
}

function crawlActive() {
  /* идёт ли сейчас прогон ff_collect (демон в это время вкладку
     не трогает — рулит прогон). pgrep себя не матчит. */
  try {
    const out = child.execSync("pgrep -f 'ff_collec[t]\\.py'",
                               { encoding: 'utf8', timeout: 5000 });
    return out.trim().length > 0;
  } catch (e) {
    return false;  // rc=1 — процессов нет
  }
}

function proxyReady() {
  return new Promise((resolve) => {
    const s = net.connect(10808, '127.0.0.1');
    s.setTimeout(4000, () => { s.destroy(); resolve(false); });
    s.on('connect', () => { s.destroy(); resolve(true); });
    s.on('error', () => resolve(false));
  });
}

async function addSessionCookies(ctx) {
  let state;
  try {
    state = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
  } catch (e) {
    log('avito_session.json не читается: ' + e.message);
    return false;
  }
  const cookies = state.cookies || [];
  const names = new Set(cookies.map((c) => (c.name || '').toLowerCase()));
  const hasLogin = LOGIN_COOKIES.some((n) => names.has(n));
  let ok = 0;
  for (const c of cookies) {
    const cc = Object.assign({}, c);
    if (typeof cc.expires === 'number' && cc.expires > 1e11) {
      cc.expires = cc.expires / 1000;
    }
    if (!['Strict', 'Lax', 'None'].includes(cc.sameSite)) delete cc.sameSite;
    if (cc.sameSite === 'None' && !cc.secure) delete cc.sameSite;
    if (!cc.path) cc.path = '/';
    try { await ctx.addCookies([cc]); ok++; } catch (e) {}
  }
  log('куки сессии: добавлено ' + ok
      + (hasLogin ? ' (логин есть)' : ' (БЕЗ кук логина)'));
  return hasLogin;
}

async function dismissPopups(page) {
  for (const t of ['Хорошо', 'Понятно', 'Принять', 'Всё верно']) {
    try {
      const loc = page.locator('button', { hasText: t });
      const n = await loc.count();
      for (let i = 0; i < Math.min(n, 3); i++) {
        const el = loc.nth(i);
        if (await el.isVisible()) {
          await el.click({ timeout: 2000 });
          log('закрыл попап «' + t + '»');
          await page.waitForTimeout(300);
          return;
        }
      }
    } catch (e) { /* нет попапа — норм */ }
  }
}

async function gotoSafe(page, url, tries) {
  tries = tries || 3;
  for (let i = 0; i < tries; i++) {
    try {
      await page.goto(url, { timeout: 60000, waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2500);
      return true;
    } catch (e) {
      log('goto fail (' + (i + 1) + '/' + tries + '): '
          + String(e.message || e).split('\n')[0].slice(0, 120));
    }
  }
  return false;
}

/* ---------------- состояние демона ---------------- */
let srv = null;
let browser = null;
let ctx = null;
let page = null;
let sessionLoadedAt = 0;
let lastReload = 0;
let lastScroll = 0;
let lastSave = 0;
let lastRecycleDay = '';

async function createContext() {
  ctx = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    screen: { width: 1280, height: 900 },
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    userAgent: UA,
  });
  await ctx.addInitScript(INIT_JS);
  await addSessionCookies(ctx);
  sessionLoadedAt = Date.now();
  page = await ctx.newPage();
  if (!(await gotoSafe(page, HOME_URL))) {
    log('!!! главная Авито не открылась — ещё попытка позже (tick)');
  }
  if (!(await gotoSafe(page, SEARCH_URL))) {
    log('!!! поиск не открылся — вкладка на ' + (page.url() || '?'));
  }
  await dismissPopups(page);
  let title = '';
  try { title = await page.title(); } catch (e) {}
  log('вкладка Авито открыта: ' + (page.url() || '?').slice(0, 90)
      + ' · title=' + title.slice(0, 60));
}

async function saveSession(why) {
  if (!ctx) return;
  try {
    await ctx.storageState({ path: SESSION_FILE });
    log('сессия сохранена -> ' + path.basename(SESSION_FILE)
        + (why ? ' (' + why + ')' : ''));
  } catch (e) {
    log('storage_state fail: ' + String(e.message || e).slice(0, 100));
  }
}

async function tick() {
  touch(HEARTBEAT);

  /* живы ли браузер/вкладка? */
  let alive = false;
  try {
    if (page && !page.isClosed()) {
      await page.evaluate('1');
      alive = true;
    }
  } catch (e) {}
  if (!alive) {
    log('вкладка/контекст умерли — пересоздаю');
    try { if (ctx) await ctx.close(); } catch (e) {}
    await createContext();
    lastReload = lastScroll = lastSave = Date.now();
    return;
  }

  const active = crawlActive();
  const now = Date.now();

  /* свежие куки (юзер перелогинился / ff_collect сохранил ротацию) */
  try {
    const st = fs.statSync(SESSION_FILE);
    if (st.mtimeMs > sessionLoadedAt + 30000 && !active) {
      await addSessionCookies(ctx);
      sessionLoadedAt = st.mtimeMs;
    }
  } catch (e) {}

  if (!active) {
    /* лёгкий скролл ~раз в 5 мин — вкладка «живая» */
    if (now - lastScroll > 5 * 60 * 1000) {
      try {
        await page.evaluate('window.scrollBy(0, 600)');
        await page.waitForTimeout(400);
        await page.evaluate('window.scrollTo(0, 0)');
      } catch (e) {}
      lastScroll = now;
    }
    /* мягкий reload ~раз в 20 мин + возврат на выдачу поиска */
    if (now - lastReload > 20 * 60 * 1000) {
      try {
        await page.reload({ timeout: 60000, waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(2000);
        await dismissPopups(page);
        if (!(page.url() || '').includes('avito.ru')) {
          await gotoSafe(page, HOME_URL, 1);
        }
        if (!(page.url() || '').includes('q=')) {
          await gotoSafe(page, SEARCH_URL, 1);
        }
        log('вкладка обновлена: ' + (page.url() || '?').slice(0, 90));
      } catch (e) {
        log('reload fail: ' + String(e.message || e).slice(0, 100));
      }
      lastReload = now;
    }
    /* сессия в файл ~раз в 10 мин */
    if (now - lastSave > 10 * 60 * 1000) {
      await saveSession('план');
      lastSave = now;
    }
  }

  /* гигиена памяти: пересоздать контекст ~04:30 МСК (браузер живёт) */
  const d = new Date();
  const dayKey = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
  if (d.getHours() === 4 && d.getMinutes() >= 25 && lastRecycleDay !== dayKey) {
    log('04:30 МСК — пересоздаю контекст (гигиена памяти, браузер живёт)');
    try { if (ctx) await ctx.close(); } catch (e) {}
    await createContext();
    lastRecycleDay = dayKey;
    lastReload = lastScroll = lastSave = Date.now();
  }
}

async function main() {
  log('старт: жду прокси ' + PROXY);
  for (let i = 0; i < 30; i++) {
    if (await proxyReady()) break;
    log('прокси недоступен, попытка ' + (i + 1) + '/30 — жду 10 c');
    await new Promise((r) => setTimeout(r, 10000));
    if (i === 29) {
      log('!!! прокси так и не поднялся — выхожу (systemd перезапустит)');
      process.exit(1);
    }
  }
  log('прокси ок — поднимаю резидентный браузер (порт ' + PORT + ')');
  try {
    srv = await firefox.launchServer({
      headless: false,
      port: PORT,
      firefoxUserPrefs: FF_PREFS,
      proxy: { server: PROXY },
    });
  } catch (e) {
    log('!!! launchServer fail: ' + String(e.message || e).slice(0, 200));
    process.exit(1);
  }
  const ep = srv.wsEndpoint();
  touch(WS_FILE, ep);
  log('браузер-сервер поднят: ' + ep);

  browser = await firefox.connect(ep);
  await createContext();
  lastReload = lastScroll = lastSave = Date.now();
  touch(HEARTBEAT);

  /* цикл: tick каждые 45 c, без перекрытий */
  let busy = false;
  const schedule = () => setTimeout(async () => {
    if (!busy) {
      busy = true;
      try {
        await tick();
      } catch (e) {
        log('tick: ' + String(e.message || e).slice(0, 150));
        /* фатально? проверим на следующем такте (вкладка пересоздастся),
           а если умер сам сервер — выходим, systemd поднимет заново */
        try {
          if (srv) { await srv.wsEndpoint(); }  // throws если сервер мёртв
        } catch (e2) {
          log('браузер-сервер мёртв — выхожу на рестарт systemd');
          process.exit(1);
        }
      }
      busy = false;
    }
    schedule();
  }, 45000);
  schedule();
  log('демон работает: вкладка Авито постоянна, heartbeat каждые 45 c');
}

process.on('SIGTERM', async () => {
  log('SIGTERM — сохраняю сессию и аккуратно выхожу');
  try { if (ctx) await ctx.storageState({ path: SESSION_FILE }); } catch (e) {}
  try { fs.unlinkSync(WS_FILE); } catch (e) {}
  try { fs.unlinkSync(HEARTBEAT); } catch (e) {}
  try { if (srv) await srv.close(); } catch (e) {}
  process.exit(0);
});

main().catch((e) => {
  log('ФАТАЛ: ' + String(e && e.stack || e).slice(0, 400));
  process.exit(1);
});
