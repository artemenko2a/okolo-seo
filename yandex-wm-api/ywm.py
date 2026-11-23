#!/usr/bin/env python3
"""
yandex-wm-api — Yandex Webmaster API v4 from the command line, for AI agents.

Token: YANDEX_WEBMASTER_TOKEN in .env next to this file (see .env.example, SETUP.md).
Every read request is cached in cache/ for the current day (the API has no date
parameter for some methods and its windows roll daily). --refresh bypasses the cache.

    python ywm.py hosts
    python ywm.py freshness --host example.com
    python ywm.py qa --host example.com --group url --csv pages.csv
    python ywm.py raw GET H/summary --host example.com

All commands and caveats: SKILL.md. Endpoint catalogue: reference.md.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HOME = Path(__file__).resolve().parent
CACHE_DIR = HOME / "cache"
PRO_TASKS = CACHE_DIR / "_pro_tasks.json"
CACHE_MAX_AGE_DAYS = 2  # entries are keyed by day, older ones are never hit again
API_ROOT = "https://api.webmaster.yandex.net/"
QA_PAGE = 500       # query-analytics/list: max limit per call
SAMPLES_PAGE = 100  # */samples: max limit per call
POPULAR_PAGE = 500
QA_CAP = 150000     # observed ceiling of unique queries per 14-day window
DEVICES = ["ALL", "DESKTOP", "MOBILE_AND_TABLET", "MOBILE", "TABLET"]
LOCATIONS = ["WEB_LOCATION", "ALL_LOCATIONS", "ALL_LOCATIONS_ORGANIC",
             "DYNAMIC_LOCATION_ALL", "DYNAMIC_LOCATION_BASIC", "DYNAMIC_LOCATION_ADDITIONAL"]
QUERY_INDICATORS = ["TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION", "AVG_CLICK_POSITION"]

HINTS = {
    401: "токен не задан, неверный или истёк (живёт 6 месяцев) — см. SETUP.md",
    "INVALID_OAUTH_TOKEN": "токен неверный или истёк — получи новый по SETUP.md",
    "INVALID_USER_ID": "user_id не совпадает с владельцем токена — убери YANDEX_WEBMASTER_USER_ID из .env",
    "ACCESS_FORBIDDEN": "у токена нет прав на этот метод (нужен доступ webmaster:hostinfo, для записи может понадобиться webmaster:verify)",
    "HOST_NOT_FOUND": "сайта нет в этом аккаунте — проверь `python ywm.py hosts`",
    "HOST_NOT_VERIFIED": "права на сайт в этом аккаунте не подтверждены",
    "HOST_NOT_INDEXED": "Яндекс ещё не проиндексировал сайт — данных нет",
    "HOST_NOT_LOADED": "данные по сайту ещё не загружены",
    "QUERY_ID_NOT_FOUND": "query_id устарел — возьми свежий через `popular`",
    "TOO_MANY_REQUESTS_ERROR": "лимит query-analytics — 10 000 запросов в час на домен; скачанные страницы в кэше, повтори позже тем же вызовом",
    "QUOTA_EXCEEDED": "суточная квота метода исчерпана (время сброса — в exceeded_until)",
    "LIMITS_EXCEEDED": "лимит PRO-выгрузки на сегодня исчерпан, сброс в 00:00 МСК",
    "HOST_RECRAWLS_QUOTA_EXCEEDED": "лимит приоритетного переобхода Sitemap — 10 в месяц",
    "RECRAWL_IN_PROCESS": "этот URL уже стоит в очереди на переобход",
    "RESTRICTIONS_VIOLATED": "даты фильтра должны лежать внутри текущего окна query-analytics — см. `freshness`",
}


def _load_dotenv():
    """KEY=VALUE lines from .env next to this file; already set env vars win."""
    env_path = HOME / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- http + cache

class ApiError(Exception):
    def __init__(self, status, data, method, path):
        self.status, self.data, self.method, self.path = status, data, method, path
        code = data.get("error_code") or data.get("code") or ""
        msg = data.get("error_message") or data.get("message") or data.get("_text", "")
        super().__init__(f"HTTP {status} {code}: {msg}".strip())
        self.code = code


_local = threading.local()
FORCE_REFRESH = False  # set from --refresh for every call of the command, helpers included


def _session():
    token = os.environ.get("YANDEX_WEBMASTER_TOKEN")
    if not token:
        raise SystemExit("YANDEX_WEBMASTER_TOKEN не задан. Скопируй .env.example в .env рядом с ywm.py "
                         "и впиши токен — пошагово в SETUP.md.")
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"Authorization": f"OAuth {token}", "Accept": "application/json"})
        _local.s = s
    return _local.s


def _cache_path(method, path, params, body):
    raw = json.dumps([method, path, params, body, date.today().isoformat()], sort_keys=True, ensure_ascii=False)
    return CACHE_DIR / f"{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}.json"


def api(method, path, params=None, body=None, cache=True, refresh=False, attempts=6):
    """One API call. path is relative to the API root ('v4/user/...'). Read calls are cached
    for the current day; errors raise ApiError; network errors, 429 and 5xx are retried.
    Calls that spend quota or change data must pass attempts=1: a retry after a timeout
    could submit them twice."""
    cfile = _cache_path(method, path, params, body) if cache else None
    if cfile and not (refresh or FORCE_REFRESH) and cfile.exists():
        try:
            return json.loads(cfile.read_text(encoding="utf-8"))["data"]
        except (json.JSONDecodeError, KeyError):
            pass
    last = None
    for attempt in range(attempts):
        try:
            r = _session().request(method, API_ROOT + path, params=params, json=body, timeout=(15, 180))
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {str(e)[:150]}"
            if attempt < attempts - 1:
                time.sleep(min(60, 3 * 2 ** attempt))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code} {r.text[:200]}"
            if attempt < attempts - 1:
                time.sleep(60 if r.status_code == 429 else min(60, 3 * 2 ** attempt))
                continue
        break
    else:
        raise SystemExit(f"API не ответил за {attempts} попыток: {method} {path} — {last}" +
                         ("\nЗапрос мог дойти до Яндекса: прежде чем повторять, проверь результат (очередь, лимит)."
                          if attempts == 1 else ""))
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {"_text": r.text[:500]}
    if r.status_code >= 400:
        raise ApiError(r.status_code, data if isinstance(data, dict) else {"_text": str(data)}, method, path)
    if cfile:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cfile.with_suffix(".tmp")
        tmp.write_text(json.dumps({"request": {"method": method, "path": path, "params": params, "body": body},
                                   "fetched_at": datetime.now().isoformat(timespec="seconds"), "data": data},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(cfile)
    return data


def _prune_cache():
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - CACHE_MAX_AGE_DAYS * 86400
    for f in CACHE_DIR.glob("*.json"):
        if not f.name.startswith("_") and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


# --------------------------------------------------------------------------- user, hosts, paths

_uid = None


def user_id():
    global _uid
    if _uid is None:
        _uid = os.environ.get("YANDEX_WEBMASTER_USER_ID") or str(api("GET", "v4/user")["user_id"])
    return _uid


def list_hosts(refresh=False):
    return api("GET", f"v4/user/{user_id()}/hosts", refresh=refresh).get("hosts", [])


def _hostname(url):
    try:
        return (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return (urlsplit(url).hostname or "").lower()


def resolve_host(value):
    """host_id as is; a domain or URL -> host_id of the main mirror (https preferred)."""
    if re.fullmatch(r"https?:[^:/\s]+:\d+", value):
        return value
    s = value.strip()
    scheme = s.split("://", 1)[0].lower() if "://" in s else None
    name = _hostname(s if "://" in s else "https://" + s)
    hosts = list_hosts()

    def match(strip_www):
        n = name[4:] if strip_www and name.startswith("www.") else name
        out = []
        for h in hosts:
            hn = _hostname(h.get("ascii_host_url", ""))
            if (hn[4:] if strip_www and hn.startswith("www.") else hn) == n:
                out.append(h)
        return out

    cands = match(False) or match(True)
    if not cands:
        similar = [h["host_id"] for h in hosts if name.split(".")[0] in h["host_id"]][:10]
        raise SystemExit(f"Сайт {value} не найден в аккаунте. Похожие: {similar or '—'}. Список: python ywm.py hosts")
    cands.sort(key=lambda h: (h.get("main_mirror") is not None,
                              (h["host_id"].split(":")[0] != scheme) if scheme else h["host_id"].split(":")[0] != "https"))
    chosen = cands[0]
    if chosen.get("main_mirror"):
        log(f"[host] {chosen['host_id']} — не главное зеркало, главное: {chosen['main_mirror']['host_id']}")
    return chosen["host_id"]


def hp(host, suffix, ver="v4"):
    return f"{ver}/user/{user_id()}/hosts/{host}/{suffix}"


def host_base(host_id):
    scheme, name, port = host_id.split(":")
    default = (scheme == "https" and port == "443") or (scheme == "http" and port == "80")
    return f"{scheme}://{name}" + ("" if default else f":{port}")


def unmangle(value):
    """Git Bash turns an argument '/x' into 'C:/Program Files/Git/x' before Python sees it; undo that."""
    v = value.strip()
    m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git([/\\].*)$", v)
    if m:
        log(f"[path] {v} похоже на путь, подменённый Git Bash; беру {m.group(1)}")
        v = m.group(1).replace("\\", "/")
    return v


def to_path(value):
    """Full URL or path (with or without the leading slash) -> '/path?query'."""
    v = unmangle(value)
    if "://" in v:
        p = urlsplit(v)
        return (p.path or "/") + (f"?{p.query}" if p.query else "")
    return v if v.startswith("/") else "/" + v


def read_lines(path):
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8-sig").splitlines() if ln.strip()]


def d10(s):
    return (s or "")[:10]


def dt16(s):
    """API datetime -> 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' when the time is not midnight."""
    s = s or ""
    t = s[11:16]
    return s[:10] if t in ("", "00:00") else f"{s[:10]} {t}"


def date_range(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


# --------------------------------------------------------------------------- output

def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v % 1 else str(int(v))
    return str(v)


def write_csv(rows, columns, csv_path):
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in columns])
    log(f"[csv] {len(rows)} строк -> {csv_path}")


def emit(rows, columns, csv_path=None, limit=30):
    """Print up to `limit` rows (0 — all) as an aligned table; write ALL rows to CSV (utf-8-sig)."""
    if csv_path:
        write_csv(rows, columns, csv_path)
    shown = rows if not limit or limit < 0 else rows[:limit]
    if not shown:
        print("(нет строк)")
        return
    cells = [[_fmt(r.get(c))[:80] for c in columns] for r in shown]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
    for row in cells:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))
    if len(rows) > len(shown):
        print(f"... показано {len(shown)} из {len(rows)} (все строки — через --csv)")


def sort_rows(rows, key, asc=False):
    none_last = [r for r in rows if r.get(key) is None]
    vals = sorted((r for r in rows if r.get(key) is not None), key=lambda r: r[key], reverse=not asc)
    return vals + none_last


def apply_numeric(rows, args, fields):
    for f in fields:
        lo, hi = getattr(args, f"min_{f}", None), getattr(args, f"max_{f}", None)
        if lo is not None:
            rows = [r for r in rows if r.get(f) is not None and r[f] >= lo]
        if hi is not None:
            rows = [r for r in rows if r.get(f) is not None and r[f] <= hi]
    return rows


# --------------------------------------------------------------------------- paginated GET

def fetch_offset_pages(path, item_key, params=None, max_rows=0, workers=4, refresh=False, page=SAMPLES_PAGE):
    """offset/limit pagination (samples, popular): first page gives count, the rest in parallel."""
    params = list(params or [])

    def get(offset, lim=page):
        return api("GET", path, params=params + [("offset", offset), ("limit", lim)], refresh=refresh)

    first = get(0)
    count = int(first.get("count") or 0)
    items = list(first.get(item_key, []))
    target = count if not max_rows else min(count, max_rows)
    offsets = list(range(len(items), target, page)) if items else []
    note = None
    if offsets:
        log(f"[pages] {count} всего, качаю {target} ({len(offsets) + 1} вызовов)")
        with ThreadPoolExecutor(max(1, workers)) as ex:
            try:
                for i, res in enumerate(ex.map(get, offsets)):
                    items.extend(res.get(item_key, []))
                    if (i + 1) % 20 == 0:
                        log(f"[pages] {len(items)}/{target}")
            except ApiError as e:
                note = f"пагинация остановлена на {len(items)} строках: {e}"
    if not note and len(items) < target:
        note = f"API отдал {len(items)} строк при заявленных {count}: count у списков бывает завышен, дальше offset пустой"
    return count, items[:target] if target else items, note, first


# --------------------------------------------------------------------------- commands: general

def cmd_hosts(args):
    rows = []
    for h in list_hosts(refresh=args.refresh):
        if h.get("main_mirror") and not args.all:
            continue
        rows.append({"host_id": h["host_id"], "url": h.get("unicode_host_url"), "verified": h.get("verified"),
                     "main_mirror": (h.get("main_mirror") or {}).get("host_id", "")})
    print(f"user_id {user_id()}, сайтов: {len(rows)}" + ("" if args.all else " (только главные зеркала; все — --all)"))
    emit(rows, ["host_id", "url", "verified", "main_mirror"], args.csv, limit=0)


def cmd_summary(args):
    host = resolve_host(args.host)
    info = api("GET", hp(host, "").rstrip("/"), refresh=args.refresh)
    summ = api("GET", hp(host, "summary"), refresh=args.refresh)
    print(f"host_id: {host}")
    for k in ("unicode_host_url", "host_display_name", "verified", "host_data_status"):
        print(f"{k}: {info.get(k)}")
    print(f"main_mirror: {(info.get('main_mirror') or {}).get('host_id', '—')}")
    print(f"ИКС (sqi): {summ.get('sqi')}")
    print(f"страниц в поиске: {summ.get('searchable_pages_count')}")
    print(f"исключено страниц: {summ.get('excluded_pages_count')}")
    print(f"проблемы по важности: {summ.get('site_problems') or '—'}  (подробно — diagnostics)")


def _last_point(points, key="value"):
    pts = [p for p in points if p.get(key) not in (None,)]
    return pts[-1] if pts else None


def _popular_count(host, day):
    d = api("GET", hp(host, "search-queries/popular"),
            params=[("order_by", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_SHOWS"),
                    ("date_from", day), ("date_to", day), ("limit", 1)])
    return int(d.get("count") or 0)


def cmd_freshness(args):
    host = resolve_host(args.host)
    today = date.today()
    rows = []

    def add(source, command, last, note=""):
        lag = (today - date.fromisoformat(last[:10])).days if last else None
        rows.append({"source": source, "command": command, "last": last or "—", "lag_days": lag, "note": note})

    qa = api("POST", hp(host, "query-analytics/list"),
             body={"offset": 0, "limit": 5, "text_indicator": "QUERY", "device_type_indicator": "ALL"})
    days = sorted({s["date"] for it in qa.get("text_indicator_to_statistics", []) for s in it.get("statistics", [])})
    add("query-analytics (запрос × URL)", "qa, pairs", days[-1] if days else None,
        f"окно {days[0]}..{days[-1]}, запросов {qa.get('count')}" if days else "пусто")

    tot = api("GET", hp(host, "search-queries/all/history"),
              params=[("query_indicator", "TOTAL_SHOWS"), ("date_from", (today - timedelta(days=45)).isoformat()),
                      ("date_to", today.isoformat())])
    pts = [p for p in tot.get("indicators", {}).get("TOTAL_SHOWS", []) if p.get("value")]
    totals_last = d10(pts[-1]["date"]) if pts else None
    add("итоги сайта по дням", "totals", totals_last, "дни после — нули без данных")

    # popular: newest day with data, searching back from the totals' last day
    start = date.fromisoformat(totals_last) if totals_last else today
    hi_zero, found = None, None
    for back in (0, 7, 14, 28, 56, 112):
        dd = start - timedelta(days=back)
        if _popular_count(host, dd.isoformat()):
            found = dd
            break
        hi_zero = dd
    if found and hi_zero:
        lo, hi = found, hi_zero  # lo has data, hi has none
        while (hi - lo).days > 1:
            mid = lo + timedelta(days=(hi - lo).days // 2)
            if _popular_count(host, mid.isoformat()):
                lo = mid
            else:
                hi = mid
        found = lo
    add("топ запросов по дням", "popular, query-history", found.isoformat() if found else None,
        "глубина ~85 дней до этой даты")

    for source, command, suffix, extract in (
        ("страниц в поиске (история)", "history --kind in-search", "search-urls/in-search/history",
         lambda d: d.get("history", [])),
        ("появились/выпали (история)", "history --kind events", "search-urls/events/history",
         lambda d: d.get("indicators", {}).get("APPEARED_IN_SEARCH", []) + d.get("indicators", {}).get("REMOVED_FROM_SEARCH", [])),
        ("обход по HTTP-кодам", "history --kind crawl", "indexing/history",
         lambda d: [p for v in d.get("indicators", {}).values() for p in v]),
        ("ИКС", "history --kind sqi", "sqi-history", lambda d: d.get("points", [])),
    ):
        try:
            d = api("GET", hp(host, suffix), params={"date_from": (today - timedelta(days=120)).isoformat(),
                                                     "date_to": today.isoformat()})
            p = sorted(extract(d), key=lambda x: x.get("date", ""))
            add(source, command, dt16(p[-1]["date"]) if p else None)
        except ApiError as e:
            add(source, command, None, str(e))

    try:
        dates = sorted(api("GET", hp(host, "pro/serp/dates")).get("dates", []))
        add("PRO-выгрузка (запрос × URL × регион)", "pro-export", dates[-1] if dates else None,
            f"{len(dates)} дат с {dates[0]}" if dates else "")
    except ApiError as e:
        add("PRO-выгрузка", "pro-export", None, str(e))

    print(f"{host}, сегодня {today.isoformat()}")
    emit(rows, ["source", "command", "last", "lag_days", "note"], args.csv, limit=0)


# --------------------------------------------------------------------------- commands: search queries

def _indicators_by_date(indicators):
    by = defaultdict(dict)
    for ind, pts in (indicators or {}).items():
        for p in pts:
            by[d10(p["date"])][ind] = p.get("value")
    return by


def _query_row(m):
    shows, clicks = m.get("TOTAL_SHOWS") or 0, m.get("TOTAL_CLICKS") or 0
    return {"shows": shows, "clicks": clicks, "ctr": round(clicks / shows * 100, 2) if shows else None,
            "avg_show_position": m.get("AVG_SHOW_POSITION"), "avg_click_position": m.get("AVG_CLICK_POSITION")}


def fetch_totals(host, start, end, device="ALL", refresh=False):
    """search-queries/all/history in chunks of <=365 days (long ranges drop the connection)."""
    by = {}
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    while a <= b:
        e = min(b, a + timedelta(days=364))
        d = api("GET", hp(host, "search-queries/all/history"),
                params=[("query_indicator", q) for q in QUERY_INDICATORS] +
                       [("device_type_indicator", device), ("date_from", a.isoformat()), ("date_to", e.isoformat())],
                refresh=refresh)
        by.update(_indicators_by_date(d.get("indicators")))
        a = e + timedelta(days=1)
    return by


def _bucket(day, by):
    if by == "week":
        d = date.fromisoformat(day)
        return (d - timedelta(days=d.weekday())).isoformat()
    return day[:7] if by == "month" else day


def cmd_totals(args):
    host = resolve_host(args.host)
    by = fetch_totals(host, args.start, args.end, args.device, args.refresh)
    days = sorted(by)
    while days and not by[days[-1]].get("TOTAL_SHOWS") and not by[days[-1]].get("TOTAL_CLICKS"):
        days.pop()
    dropped = len(by) - len(days)
    holes = [d for d in days if not by[d].get("TOTAL_SHOWS")]
    groups = defaultdict(lambda: {"shows": 0, "clicks": 0, "sp": 0.0, "sw": 0, "cp": 0.0, "cw": 0, "days": 0})
    for d in days:
        m, g = by[d], groups[_bucket(d, args.by)]
        s, c = m.get("TOTAL_SHOWS") or 0, m.get("TOTAL_CLICKS") or 0
        g["shows"] += s
        g["clicks"] += c
        g["days"] += 1
        if m.get("AVG_SHOW_POSITION") is not None and s:
            g["sp"] += m["AVG_SHOW_POSITION"] * s
            g["sw"] += s
        if m.get("AVG_CLICK_POSITION") is not None and c:
            g["cp"] += m["AVG_CLICK_POSITION"] * c
            g["cw"] += c
    rows = [{"period": k, "days": g["days"], "shows": g["shows"], "clicks": g["clicks"],
             "ctr": round(g["clicks"] / g["shows"] * 100, 2) if g["shows"] else None,
             "avg_show_position": round(g["sp"] / g["sw"], 2) if g["sw"] else None,
             "avg_click_position": round(g["cp"] / g["cw"], 2) if g["cw"] else None}
            for k, g in sorted(groups.items())]
    ts, tc = sum(r["shows"] for r in rows), sum(r["clicks"] for r in rows)
    print(f"{host}, {args.start}..{days[-1] if days else args.end}, устройство {args.device}: "
          f"показы {ts:.0f}, клики {tc:.0f}, CTR {tc / ts * 100 if ts else 0:.2f}%")
    if dropped:
        print(f"Последние {dropped} дн. отброшены: API отдаёт за них нули, данных ещё нет.")
    if holes:
        print(f"Внимание: {len(holes)} дн. внутри периода с нулём показов (провал данных?): "
              f"{', '.join(holes[:10])}{' ...' if len(holes) > 10 else ''}. Сравнивая периоды, учти их.")
    emit(rows, ["period", "days", "shows", "clicks", "ctr", "avg_show_position", "avg_click_position"],
         args.csv, args.limit)


def cmd_popular(args):
    host = resolve_host(args.host)
    params = [("order_by", "TOTAL_CLICKS" if args.order_by == "clicks" else "TOTAL_SHOWS"),
              ("device_type_indicator", args.device), ("date_from", args.start), ("date_to", args.end)]
    params += [("query_indicator", q) for q in QUERY_INDICATORS]
    count, items, note, first = fetch_offset_pages(hp(host, "search-queries/popular"), "queries", params,
                                                   args.max_rows, args.workers, args.refresh, page=POPULAR_PAGE)
    rows = [dict(query=q.get("query_text"), query_id=q.get("query_id"), **_query_row(q.get("indicators", {})))
            for q in items]
    print(f"{host}, период по ответу API {first.get('date_from')}..{first.get('date_to')}, "
          f"запросов {count} (топ до 3000), устройство {args.device}")
    if not count:
        print("Пусто. Часто это не «нет показов», а нет данных за эти дни — проверь `freshness`.")
    if note:
        print(note)
    emit(rows, ["query", "shows", "clicks", "ctr", "avg_show_position", "avg_click_position", "query_id"],
         args.csv, args.limit)


def cmd_query_history(args):
    host = resolve_host(args.host)
    qid, text = args.query_id, args.query
    if not qid:
        params = [("order_by", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_SHOWS"),
                  ("date_from", args.start), ("date_to", args.end)]
        _, items, _, _ = fetch_offset_pages(hp(host, "search-queries/popular"), "queries", params, 0, 4,
                                            args.refresh, page=POPULAR_PAGE)
        hit = [q for q in items if (q.get("query_text") or "").strip().lower() == text.strip().lower()]
        if not hit:
            raise SystemExit(f"Запроса «{text}» нет в топ-3000 popular за {args.start}..{args.end}. "
                             "Для хвостовых запросов — `qa --query-equals` (последние 14 дней).")
        qid = hit[0]["query_id"]
    d = api("GET", hp(host, f"search-queries/{qid}/history"),
            params=[("query_indicator", q) for q in QUERY_INDICATORS] +
                   [("device_type_indicator", args.device), ("date_from", args.start), ("date_to", args.end)],
            refresh=args.refresh)
    q = d if "indicators" in d else (d.get("queries") or [{}])[0]  # docs show a list, the API returns one object
    by = _indicators_by_date(q.get("indicators"))
    rows = [dict(date=k, **_query_row(by[k])) for k in sorted(by)]
    print(f"«{q.get('query_text', text)}» (query_id {qid}), {host}. Дни без данных API заполняет нулями.")
    emit(rows, ["date", "shows", "clicks", "ctr", "avg_show_position", "avg_click_position"], args.csv, args.limit)


# --------------------------------------------------------------------------- commands: query-analytics

_OPS = {"lt": "LESS_THAN", "gt": "GREATER_THAN", "le": "LESS_EQUAL", "ge": "GREATER_EQUAL", "eq": "EQUAL"}


def qa_window(host):
    """(first, last) day of the current query-analytics window, from one tiny call."""
    d = api("POST", hp(host, "query-analytics/list"), body={"offset": 0, "limit": 1, "text_indicator": "QUERY"})
    return window_of(d.get("text_indicator_to_statistics", []))


def qa_body(args, group, window=None):
    tf = []
    for attr, ind, op in (("query_contains", "QUERY", "TEXT_CONTAINS"), ("query_not_contains", "QUERY", "TEXT_DOES_NOT_CONTAIN"),
                          ("query_equals", "QUERY", "TEXT_MATCH"), ("url_contains", "URL", "TEXT_CONTAINS"),
                          ("url_not_contains", "URL", "TEXT_DOES_NOT_CONTAIN"), ("url_equals", "URL", "TEXT_MATCH")):
        for v in getattr(args, attr, None) or []:
            if ind == "URL":
                v = to_path(v) if (op == "TEXT_MATCH" or "://" in v) else unmangle(v)
            tf.append({"text_indicator": ind, "operation": op, "value": v})
    sf = []
    if getattr(args, "day", None):
        sf.append({"statistic_field": "IMPRESSIONS", "operation": "GREATER_THAN", "value": "0",
                   "from": args.day, "to": args.day})
    for raw in getattr(args, "stat_filter", None) or []:
        parts = raw.split(":")
        if len(parts) not in (3, 5):
            raise SystemExit(f"--stat-filter {raw}: нужно FIELD:OP:VALUE или FIELD:OP:VALUE:FROM:TO")
        f = {"statistic_field": parts[0].upper(), "operation": _OPS.get(parts[1].lower(), parts[1].upper()),
             "value": parts[2]}
        # the API requires from/to inside the current window; default to the whole window
        f.update({"from": parts[3], "to": parts[4]} if len(parts) == 5 else {"from": window[0], "to": window[1]})
        sf.append(f)
    body = {"text_indicator": group.upper(), "device_type_indicator": args.device}
    if args.location:
        body["search_location"] = args.location
    if args.region:
        body["region_ids"] = [int(r) for r in args.region]
    if tf or sf:
        body["filters"] = {}
        if tf:
            body["filters"]["text_filters"] = tf
        if sf:
            body["filters"]["statistic_filters"] = sf
    return body, bool(tf or sf or args.region)


def qa_fetch(host, body, max_rows=0, min_impressions=None, workers=4, refresh=False, quiet=False):
    """All pages of query-analytics/list. The API sorts by 14-day impressions, so with
    min_impressions fetching stops once a whole batch of pages falls below the threshold."""
    path = hp(host, "query-analytics/list")

    def page(offset):
        return api("POST", path, body=dict(body, offset=offset, limit=QA_PAGE), refresh=refresh)

    first = page(0)
    count = int(first.get("count") or 0)
    items = list(first.get("text_indicator_to_statistics", []))
    target = count if not max_rows else min(count, max_rows)
    offsets = list(range(QA_PAGE, target, QA_PAGE))
    stopped = False
    if offsets and not quiet:
        log(f"[qa] {count} строк в окне, качаю до {target} ({len(offsets) + 1} вызовов, потоков {workers})")
    w = max(1, workers)
    with ThreadPoolExecutor(w) as ex:
        for i in range(0, len(offsets), w):
            if min_impressions is not None and items and max(
                    qa_aggregate(it)["impressions"] for it in items[-QA_PAGE:]) < min_impressions:
                stopped = True
                break
            for res in ex.map(page, offsets[i:i + w]):
                items.extend(res.get("text_indicator_to_statistics", []))
            if not quiet and (i // w) % 5 == 4:
                log(f"[qa] {len(items)}/{target}")
    return count, items[:target] if target else items, stopped


def qa_aggregate(item):
    """Daily statistics -> window totals; position weighted by each day's impressions, CTR from sums."""
    by = defaultdict(dict)
    for s in item.get("statistics", []):
        by[s["date"]][s["field"]] = s.get("value")
    imp = sum((m.get("IMPRESSIONS") or 0) for m in by.values())
    clk = sum((m.get("CLICKS") or 0) for m in by.values())
    dem = sum((m.get("DEMAND") or 0) for m in by.values())
    pw = sum(m["POSITION"] * (m.get("IMPRESSIONS") or 0) for m in by.values() if m.get("POSITION") is not None)
    w = sum((m.get("IMPRESSIONS") or 0) for m in by.values() if m.get("POSITION") is not None)
    return {"impressions": imp, "clicks": clk, "ctr": round(clk / imp * 100, 2) if imp else None,
            "position": round(pw / w, 2) if w else None, "demand": dem,
            "days": sum(1 for m in by.values() if m.get("IMPRESSIONS")), "_by": by}


def qa_rows(host, items, group, by_date=False):
    base = host_base(host)
    out = []
    for it in items:
        key = (it.get("text_indicator") or {}).get("value")
        partner = (it.get("popular_complementary_indicator") or {}).get("value")
        a = qa_aggregate(it)
        common = {group: key}
        if group == "url":
            common["full_url"] = base + key if key and key.startswith("/") else key
        if by_date:
            for d, m in sorted(a["_by"].items()):
                imp, clk = m.get("IMPRESSIONS") or 0, m.get("CLICKS") or 0
                if not (imp or clk or m.get("DEMAND")):
                    continue
                out.append(dict(common, date=d, impressions=imp, clicks=clk,
                                ctr=round(clk / imp * 100, 2) if imp else None,
                                position=m.get("POSITION"), demand=m.get("DEMAND")))
        else:
            a.pop("_by")
            out.append(dict(common, **a, top_partner=partner))
    return out


def window_of(items):
    days = sorted({s["date"] for it in items for s in it.get("statistics", [])})
    return (days[0], days[-1]) if days else (None, None)


def cmd_qa(args):
    host = resolve_host(args.host)
    need_window = any(len(s.split(":")) == 3 for s in (args.stat_filter or []))
    body, filtered = qa_body(args, args.group, qa_window(host) if need_window else None)
    count, items, stopped = qa_fetch(host, body, args.max_rows, args.min_impressions if not args.by_date else None,
                                     args.workers, args.refresh)
    w0, w1 = window_of(items)
    rows = qa_rows(host, items, args.group, args.by_date)
    if args.day and args.by_date:
        rows = [r for r in rows if r["date"] == args.day]
    print(f"{host}, query-analytics по {args.group}, " + (f"окно {w0}..{w1} (14 дней, свежее API не отдаёт)" if w0 else "нет строк") +
          f", всего строк в окне {count}, скачано {len(items)}" + (", остановлено по --min-impressions" if stopped else ""))
    if not count and any(not to_path(u).endswith("/") for u in (args.url_equals or [])):
        print("Точный адрес не нашёлся: путь сравнивается побуквенно, проверь слеш на конце (`/page/` и `/page` — разные).")
    if args.group == "query" and count >= QA_CAP * 0.99:
        print(f"Список упёрся в потолок API (~{QA_CAP} запросов на окно): хвоста нет ни в одном разрезе, "
              "долю видно по строке «Покрытие» ниже; полный хвост страницы — только pro-export.")
    if not filtered and w0 and not args.by_date:
        tot = fetch_totals(host, w0, w1, args.device)
        ts = sum((m.get("TOTAL_SHOWS") or 0) for m in tot.values())
        tc = sum((m.get("TOTAL_CLICKS") or 0) for m in tot.values())
        si, sc = sum(r["impressions"] for r in rows), sum(r["clicks"] for r in rows)
        print(f"Покрытие: в выборке {si:.0f} показов и {sc:.0f} кликов — {si / ts * 100 if ts else 0:.1f}% и "
              f"{sc / tc * 100 if tc else 0:.1f}% итогов сайта за окно (totals: {ts:.0f} / {tc:.0f}).")
    # DEMAND is a query-level metric: for pages the API returns 0
    key_cols = [args.group] + (["full_url"] if args.group == "url" else [])
    metrics = ["impressions", "clicks", "ctr", "position"] + (["demand"] if args.group == "query" else [])
    if args.by_date:
        cols = key_cols + ["date"] + metrics
        rows.sort(key=lambda r: (r["date"], -(r["impressions"] or 0)))
    else:
        rows = apply_numeric(rows, args, ["impressions", "clicks", "ctr", "position", "demand"])
        rows = sort_rows(rows, args.sort_by, args.sort_asc)
        cols = key_cols + metrics + ["days", "top_partner"]
    emit(rows, cols, args.csv, args.limit)


def cmd_pairs(args):
    host = resolve_host(args.host)

    def ns(**filters):
        return argparse.Namespace(device=args.device, location=args.location, region=args.region, day=args.day, **filters)

    pairs = []
    page = to_path(args.page) if args.page else None
    if page:
        # who competes with one page: its top queries, each split over all pages of the site
        body, _ = qa_body(ns(url_equals=[page]), "query")
        _, items, _ = qa_fetch(host, body, args.top_queries, None, args.workers, args.refresh, quiet=True)
        demand = {r["query"]: r["demand"] for r in qa_rows(host, items, "query")}
        log(f"[pairs] запросов страницы: {len(demand)}, вызов на запрос")

        def one_query(q):
            b, _ = qa_body(ns(query_equals=[q]), "url")
            return q, qa_fetch(host, b, 0, None, 1, args.refresh, quiet=True)[1]

        with ThreadPoolExecutor(max(1, args.workers)) as ex:
            for q, its in ex.map(one_query, list(demand)):
                for r in qa_rows(host, its, "url"):
                    pairs.append({"url": r["url"], "query": q, "impressions": r["impressions"], "clicks": r["clicks"],
                                  "ctr": r["ctr"], "position": r["position"], "demand": demand[q], "days": r["days"]})
        paths = sorted({p["url"] for p in pairs})
    else:
        paths = [to_path(u) for u in (args.url or [])] + ([to_path(u) for u in read_lines(args.urls_file)] if args.urls_file else [])
        if not paths:
            body, _ = qa_body(ns(), "url")
            _, items, _ = qa_fetch(host, body, args.top_urls, None, args.workers, args.refresh, quiet=True)
            paths = [(it.get("text_indicator") or {}).get("value") for it in items]
        log(f"[pairs] страниц: {len(paths)}, вызов на страницу (+ пагинация по 500)")

        def one(path):
            b, _ = qa_body(ns(url_equals=[path]), "query")
            return path, qa_fetch(host, b, 0, None, 1, args.refresh, quiet=True)[1]

        with ThreadPoolExecutor(max(1, args.workers)) as ex:
            for i, (path, items) in enumerate(ex.map(one, paths)):
                for r in qa_rows(host, items, "query"):
                    r.pop("top_partner", None)
                    pairs.append(dict(url=path, **r))
                if (i + 1) % 25 == 0:
                    log(f"[pairs] {i + 1}/{len(paths)}")
    groups = defaultdict(list)
    for p in pairs:
        groups[p["query"]].append(p)
    cann = []
    for q, ps in groups.items():
        if len(ps) < args.min_urls:
            continue
        ps.sort(key=lambda p: -p["impressions"])
        ti = sum(p["impressions"] for p in ps)
        g = {"query": q, "urls": len(ps), "impressions": ti, "clicks": sum(p["clicks"] for p in ps),
             "top_url": ps[0]["url"], "top_share": round(ps[0]["impressions"] / ti * 100, 1) if ti else None,
             "all_urls": " | ".join(f"{p['url']} ({p['impressions']:.0f}, поз. {_fmt(p['position'])})" for p in ps)}
        if page:
            mine = next((p for p in ps if p["url"] == page), None)
            g["page_share"] = round(mine["impressions"] / ti * 100, 1) if mine and ti else 0
        cann.append(g)
    cann = sort_rows(cann, "impressions")
    print(f"{host}: пар «запрос × страница» {len(pairs)} по {len(paths)} страницам, окно query-analytics (14 дней). "
          f"Запросов с ≥{args.min_urls} страницами: {len(cann)}" +
          (f" (из {args.top_queries} главных запросов {page})" if page else ""))
    if args.csv:
        write_csv(pairs, ["url", "query", "impressions", "clicks", "ctr", "position", "demand", "days"], args.csv)
    emit(cann, ["query", "urls", "impressions", "clicks", "top_url", "top_share"] + (["page_share"] if page else []) +
         ["all_urls"], args.groups_csv, args.limit)


# --------------------------------------------------------------------------- commands: indexing, links

HISTORY_KINDS = {
    "in-search": ("search-urls/in-search/history", True),
    "events": ("search-urls/events/history", True),
    "crawl": ("indexing/history", True),
    "sqi": ("sqi-history", True),
    "broken-links": ("links/internal/broken/history", True),
    "external-links": ("links/external/history", False),
}


def cmd_history(args):
    host = resolve_host(args.host)
    suffix, dated = HISTORY_KINDS[args.kind]
    params = [("date_from", args.start), ("date_to", args.end)] if dated else []
    if args.kind == "external-links":
        params = [("indicator", "LINKS_TOTAL_COUNT")]
    d = api("GET", hp(host, suffix), params=params, refresh=args.refresh)
    if "history" in d:
        series = {"pages_in_search": d["history"]}
    elif "points" in d:
        series = {"sqi": d["points"]}
    else:
        series = d.get("indicators", {})
    by = defaultdict(dict)
    for name, pts in series.items():
        for p in pts:
            by[dt16(p["date"])][name] = p.get("value")
    cols = ["date"] + sorted(series)
    rows = [dict(date=k, **v) for k, v in sorted(by.items())]
    if args.kind == "external-links" and args.start:
        rows = [r for r in rows if args.start <= r["date"][:10] <= (args.end or "9999")]
    print(f"{host}, history {args.kind}: точек {len(rows)}" + (f", последняя {rows[-1]['date']}" if rows else ""))
    emit(rows, cols, args.csv, args.limit)


SAMPLE_KINDS = {
    "in-search": ("search-urls/in-search/samples", "samples", ["url", "title", "last_access"]),
    "events": ("search-urls/events/samples", "samples",
               ["event_date", "event", "url", "excluded_url_status", "bad_http_status", "target_url", "last_access", "title"]),
    "crawl": ("indexing/samples", "samples", ["url", "status", "http_code", "access_date"]),
    "external-links": ("links/external/samples", "links", ["source_url", "destination_url", "discovery_date", "source_last_access_date"]),
    "broken-links": ("links/internal/broken/samples", "links", ["source_url", "destination_url", "discovery_date", "source_last_access_date"]),
}


def cmd_samples(args):
    host = resolve_host(args.host)
    suffix, key, cols = SAMPLE_KINDS[args.kind]
    params = [("indicator", i) for i in (args.indicator or [])] if args.kind == "broken-links" else []
    count, items, note, _ = fetch_offset_pages(hp(host, suffix), key, params, args.max_rows, args.workers, args.refresh)
    rows = []
    for it in items:
        r = {c: it.get(c) for c in cols}
        for c in ("last_access", "event_date", "access_date", "discovery_date", "source_last_access_date"):
            if r.get(c):
                r[c] = d10(r[c])
        rows.append(r)
    if args.url_contains:
        sub_ = unmangle(args.url_contains)
        rows = [r for r in rows if any(sub_ in (r.get(c) or "") for c in ("url", "source_url", "destination_url", "target_url"))]
    if args.event:
        want = "APPEARED_IN_SEARCH" if args.event == "appeared" else "REMOVED_FROM_SEARCH"
        rows = [r for r in rows if r.get("event") == want]
    if args.reason:
        rows = [r for r in rows if (r.get("excluded_url_status") or "") == args.reason.upper()]
    if args.since:
        dcol = {"events": "event_date", "in-search": "last_access", "crawl": "access_date"}.get(args.kind, "discovery_date")
        rows = [r for r in rows if (r.get(dcol) or "") >= args.since]
    print(f"{host}, samples {args.kind}: в API {count} (выборка, не полный список; потолок 50 000), "
          f"скачано {len(items)}, после фильтров {len(rows)}")
    if note:
        print(note)
    if args.kind == "events" and rows:
        c = Counter((r["event"], r.get("excluded_url_status") or "") for r in rows)
        print("Сводка: " + "; ".join(f"{e} {s or ''} — {n}".replace("  ", " ") for (e, s), n in c.most_common()))
    emit(rows, cols, args.csv, args.limit)


def cmd_important_urls(args):
    host = resolve_host(args.host)
    if args.url:
        d = api("GET", hp(host, "important-urls/history"), params={"url": args.url}, refresh=args.refresh)
        items = d.get("history", [])
    else:
        items = api("GET", hp(host, "important-urls"), refresh=args.refresh).get("urls", [])
    rows = []
    for it in items:
        ix, ss = it.get("indexing_status") or {}, it.get("search_status") or {}
        rows.append({"url": it.get("url", args.url), "update_date": d10(it.get("update_date")),
                     "changes": ",".join(it.get("change_indicators") or []),
                     "http_code": ix.get("http_code"), "crawled": d10(ix.get("access_date")),
                     "searchable": ss.get("searchable"), "excluded_status": ss.get("excluded_url_status"),
                     "target_url": ss.get("target_url"), "title": ss.get("title")})
    print(f"{host}, мониторинг важных страниц: {len(rows)} (набор задаётся только в интерфейсе Вебмастера)")
    emit(rows, ["url", "update_date", "changes", "http_code", "crawled", "searchable", "excluded_status", "target_url", "title"],
         args.csv, args.limit)


def cmd_diagnostics(args):
    host = resolve_host(args.host)
    probs = api("GET", hp(host, "diagnostics"), refresh=args.refresh).get("problems", {})
    order = {"FATAL": 0, "CRITICAL": 1, "POSSIBLE_PROBLEM": 2, "RECOMMENDATION": 3}
    rows = [{"problem": k, "severity": v.get("severity"), "state": v.get("state"), "updated": d10(v.get("last_state_update"))}
            for k, v in probs.items() if args.all or v.get("state") == "PRESENT"]
    rows.sort(key=lambda r: (order.get(r["severity"], 9), r["problem"]))
    print(f"{host}, диагностика: " + (f"все проверки ({len(rows)})" if args.all else f"найдено проблем {len(rows)} из {len(probs)} проверок"))
    emit(rows, ["severity", "problem", "state", "updated"], args.csv, limit=0)


def cmd_sitemaps(args):
    host = resolve_host(args.host)
    rows = []
    if args.user_added:
        d = api("GET", hp(host, "user-added-sitemaps"), params={"limit": 100}, refresh=args.refresh)
        for s in d.get("sitemaps", []):
            rows.append({"sitemap_id": s.get("sitemap_id"), "sitemap_url": s.get("sitemap_url"), "added": d10(s.get("added_date"))})
        emit(rows, ["sitemap_id", "sitemap_url", "added"], args.csv, limit=0)
        return
    cursor = None
    while True:
        params = [("limit", 100)] + ([("parent_id", args.parent)] if args.parent else []) + ([("from", cursor)] if cursor else [])
        page = api("GET", hp(host, "sitemaps"), params=params, refresh=args.refresh).get("sitemaps", [])
        for s in page:
            rows.append({"sitemap_id": s.get("sitemap_id"), "sitemap_url": s.get("sitemap_url"), "type": s.get("sitemap_type"),
                         "sources": ",".join(s.get("sources") or []), "urls": s.get("urls_count"),
                         "errors": s.get("errors_count"), "children": s.get("children_count"),
                         "last_access": d10(s.get("last_access_date"))})
        if len(page) < 100:
            break
        cursor = page[-1]["sitemap_id"]
    print(f"{host}, sitemap'ы, найденные роботом" + (f" (дочерние {args.parent})" if args.parent else "") +
          f": {len(rows)}. Дочерние индексного — --parent SITEMAP_ID")
    emit(rows, ["sitemap_id", "sitemap_url", "type", "sources", "urls", "errors", "children", "last_access"], args.csv, limit=0)


def _download(url, out):
    r = requests.get(url, timeout=(15, 600))
    r.raise_for_status()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(r.content)
    return len(r.content)


def cmd_archive(args):
    host = resolve_host(args.host)
    task = args.task_id or api("POST", hp(host, "indexing/archive"), cache=False).get("task_id")
    log(f"[archive] task_id {task}")
    deadline = time.time() + args.wait * 60
    while True:
        st = api("GET", hp(host, f"indexing/archive/{task}"), cache=False)
        if st.get("state") != "IN_PROGRESS" or time.time() > deadline:
            break
        time.sleep(10)
    print(f"{host}, архив всех страниц: {st.get('state')} (task_id {task})")
    if st.get("state") == "DONE" and st.get("download_url"):
        out = Path(args.out)
        raw = out.with_name(out.stem + "_raw.csv.gz")
        _download(st["download_url"], raw)
        blob = raw.read_bytes()
        text = gzip.decompress(blob).decode("utf-8") if blob[:2] == b"\x1f\x8b" else blob.decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        cols = list(rows[0].keys()) if rows else []
        emit(rows, cols, out, limit=args.limit)
        print(f"Страниц {len(rows)}; сырой файл — {raw}")
        for col in ("status", "httpCode"):
            if col in cols:
                print(f"{col}: " + ", ".join(f"{k} {n}" for k, n in Counter(r[col] for r in rows).most_common()))
    elif st.get("state") == "IN_PROGRESS":
        print(f"Ещё готовится. Повтори: python ywm.py archive --host {args.host} --task-id {task} --out {args.out}")


# --------------------------------------------------------------------------- commands: recrawl (spend quota)

def cmd_recrawl(args):
    host = resolve_host(args.host)
    urls = (args.url or []) + (read_lines(args.urls_file) if args.urls_file else [])
    q = api("GET", hp(host, "recrawl/quota"), cache=False)
    print(f"{host}: квота переобхода {q.get('quota_remainder')} из {q.get('daily_quota')} на сегодня")
    if not urls:
        tasks = api("GET", hp(host, "recrawl/queue"), params={"limit": 100}, cache=False).get("tasks", [])
        emit([{"added": dt16(t.get("added_time")), "state": t.get("state"), "url": t.get("url"), "task_id": t.get("task_id")}
              for t in tasks], ["added", "state", "url", "task_id"], args.csv, args.limit)
        return
    base = host_base(host)
    urls = [u if "://" in u else base + to_path(u) for u in urls]
    if not args.yes:
        print(f"Сухой прогон: отправлю {len(urls)} URL, потрачу {len(urls)} из {q.get('quota_remainder')}. "
              "Запуск — тот же вызов с --yes, только по просьбе пользователя.")
        for u in urls[:20]:
            print("  " + u)
        return
    rows = []
    for u in urls:
        try:
            d = api("POST", hp(host, "recrawl/queue"), body={"url": u}, cache=False, attempts=1)
            rows.append({"url": u, "result": "OK", "task_id": d.get("task_id"), "quota_left": d.get("quota_remainder")})
        except ApiError as e:
            rows.append({"url": u, "result": str(e)})
            if e.status == 429:
                break
    emit(rows, ["url", "result", "task_id", "quota_left"], args.csv, limit=0)


def cmd_sitemap_recrawl(args):
    host = resolve_host(args.host)
    info = api("GET", hp(host, "sitemaps/recrawl", ver="v4.1"), cache=False)
    lim = info.get("host_sitemaps_recrawl_limit_info") or info
    print(f"{host}: приоритетный переобход Sitemap — использовано {lim.get('requests_count')} из "
          f"{lim.get('monthly_limit_requests')} в месяц, ближайший доступный день {d10(lim.get('nearest_allowed_day'))}")
    if not args.id:
        return
    if not args.yes:
        print(f"Сухой прогон: отправлю sitemap {args.id}. Запуск — с --yes, только по просьбе пользователя.")
        return
    d = api("POST", hp(host, f"sitemaps/{args.id}/recrawl", ver="v4.1"),
            params={"parent_id": args.parent} if args.parent else None, cache=False, attempts=1)
    print(json.dumps(d, ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------- commands: PRO export

PRO_COLS = ["date", "host", "path", "query", "region_id", "region", "clicks", "impressions", "position"]
_PRO_HEAD = re.compile(r'^"(\d{4}-\d{2}-\d{2})","([^"]*)",(.*)$')
_PRO_TAIL = re.compile(r'^(.*),"(-?\d*)","([^"]*)","([^"]*)","([^"]*)","([^"]*)"$')


def parse_pro_csv(text):
    """PRO export rows. Not a standard CSV: quotes inside a field are escaped as \\" and a query
    may end with a literal backslash, so parse by the fixed outer fields and keep the middle as is."""
    rows, bad = [], 0
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        h, t = _PRO_HEAD.match(line), None
        if h:
            t = _PRO_TAIL.match(h.group(3))
        if not t:
            bad += 1
            continue
        mid = t.group(1)  # "path","query"
        sep = mid.find('","')
        if not (mid.startswith('"') and mid.endswith('"')) or sep < 0:
            bad += 1
            continue
        path, query = mid[1:sep], mid[sep + 3:-1].replace('\\"', '"')
        rows.append({"date": h.group(1), "host": h.group(2), "path": path, "query": query,
                     "region_id": t.group(2), "region": t.group(3), "clicks": float(t.group(4) or 0),
                     "impressions": float(t.group(5) or 0), "position": float(t.group(6)) if t.group(6) else None})
    return rows, bad


def _pro_tasks():
    try:
        return json.loads(PRO_TASKS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _pro_limits(host):
    lims = api("GET", hp(host, "pro/limits"), cache=False).get("limits", [])
    return next((l for l in lims if l.get("feature") == "PRO_SERP"), lims[0] if lims else {})


def cmd_pro_info(args):
    host = resolve_host(args.host)
    for l in api("GET", hp(host, "pro/limits"), cache=False).get("limits", []):
        print(f"лимит {l.get('feature')}: осталось {l.get('remaining')} из {l.get('limit')} "
              f"({l.get('period_start')}..{l.get('period_end')}, владелец {l.get('owner')}, тариф {l.get('tariff_id') or 'базовый'})")
    dates = sorted(api("GET", hp(host, "pro/serp/dates"), refresh=args.refresh).get("dates", []))
    print(f"доступных дат: {len(dates)}" + (f", {dates[0]}..{dates[-1]}" if dates else ""))
    if args.regions is not None:
        d = api("GET", hp(host, "pro/regions"), params={"filter": args.regions} if args.regions else None, refresh=args.refresh)
        emit(d.get("regions", []), ["id", "name"], args.csv, limit=0)


def cmd_pro_export(args):
    host = resolve_host(args.host)
    paths = [to_path(p) for p in (args.path or [])] + ([to_path(p) for p in read_lines(args.paths_file)] if args.paths_file else [])
    dates = list(args.date or []) + (date_range(args.start, args.end) if args.start and args.end else [])
    if not paths or not dates:
        raise SystemExit("Нужны --path (или --paths-file) и --date (или --start/--end)")
    avail = set(api("GET", hp(host, "pro/serp/dates")).get("dates", []))
    missing = [d for d in dates if d not in avail]
    dates = sorted(set(dates) & avail)
    if missing:
        print(f"Нет в доступных датах, пропускаю: {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")
    cost = len(paths) * len(dates)
    lim = _pro_limits(host)
    # one task: paths x dates <= 100 and paths + dates <= 100
    batches = []
    for i in range(0, len(paths), 99):
        ps = paths[i:i + 99]
        step = max(1, min(100 // len(ps), 100 - len(ps)))
        for j in range(0, len(dates), step):
            batches.append((ps, dates[j:j + step]))
    print(f"{host}: {len(paths)} URL × {len(dates)} дн. = {cost} URL-дней, задач {len(batches)}. "
          f"Бесплатно осталось сегодня {lim.get('remaining')} из {lim.get('limit')} (сброс 00:00 МСК).")
    if cost > (lim.get("remaining") or 0) and not args.paid:
        raise SystemExit("Не хватает бесплатного лимита. Уменьши набор или разбей по дням; платный тариф — --paid "
                         "и только если пользователь сам это сказал.")
    if not args.yes:
        print("Сухой прогон. Запуск — тот же вызов с --yes, после согласия пользователя на расход лимита.")
        return
    tasks = _pro_tasks()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ps, ds in batches:
        d = api("POST", hp(host, "pro/serp/queries/download/"), cache=False, attempts=1,
                body={"dates": ds, "paths": ps, "region_ids": [int(r) for r in (args.region or [])],
                      "use_pro_tariff": "true" if args.paid else "false"})
        tasks.append({"host": host, "task_id": d.get("task_id"), "paths": ps, "dates": ds,
                      "created": datetime.now().isoformat(timespec="seconds")})
        # save after every task: a failure in a later batch must not lose ids already paid for
        PRO_TASKS.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"task {d.get('task_id')}: {len(ps)} URL × {len(ds)} дн., free_remaining {d.get('free_quota_remaining')}")
    print(f"Готовится офлайн (20 мин – 2 ч, бывает до суток). Статус и скачивание: "
          f"python ywm.py pro-status --host {args.host} --out FILE.csv")


def cmd_pro_status(args):
    host = resolve_host(args.host)
    ids = args.task_id or [t["task_id"] for t in _pro_tasks() if t["host"] == host and not t.get("downloaded")]
    if not ids:
        raise SystemExit("Нет задач: передай --task-id или запусти pro-export")
    deadline = time.time() + args.wait * 60
    while True:
        st = {tid: api("GET", hp(host, f"pro/serp/queries/download/{tid}"), cache=False) for tid in ids}
        pending = [t for t, s in st.items() if s.get("download_status") == "IN_PROGRESS"]
        if not pending or time.time() > deadline:
            break
        log(f"[pro] готовятся {len(pending)} из {len(ids)}, жду")
        time.sleep(60)
    for tid, s in st.items():
        print(f"{tid}: {s.get('download_status')} {s.get('error_code') or ''} {s.get('error_message') or ''}".rstrip())
    if not args.out:
        return
    done = [t for t, s in st.items() if s.get("download_status") == "SUCCESS"]
    rows, bad = [], 0
    out = Path(args.out)
    for tid in done:
        raw = out.with_name(f"{out.stem}_{tid[:8]}.csv.gz")
        _download(st[tid]["url"], raw)  # keep the raw file: the link lives only 24 hours
        blob = raw.read_bytes()
        try:
            text = gzip.decompress(blob).decode("utf-8")
        except OSError:
            text = blob.decode("utf-8")
        r, b = parse_pro_csv(text)
        rows += r
        bad += b
    tasks = _pro_tasks()
    for t in tasks:
        if t["task_id"] in done:
            t["downloaded"] = True
    if tasks:
        PRO_TASKS.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Строк {len(rows)} (строка = запрос × регион × день × URL), нераспознанных {bad}; сырые .csv.gz рядом с {out}")
    emit(rows, PRO_COLS, args.out, args.limit)


# --------------------------------------------------------------------------- raw

def cmd_raw(args):
    method = args.method.upper()
    p = unmangle(args.path).lstrip("/")
    if p.startswith("H/"):
        if not args.host:
            raise SystemExit("Путь H/... требует --host")
        p = hp(resolve_host(args.host), p[2:])
    elif p.startswith("U/"):
        p = f"v4/user/{user_id()}/{p[2:]}"
    elif not re.match(r"v\d", p):
        p = "v4/" + p
    params = [tuple(kv.split("=", 1)) for kv in (args.param or [])]
    body = None
    if args.body:
        body = json.loads(Path(args.body[1:]).read_text(encoding="utf-8") if args.body.startswith("@") else args.body)
    read_post = method == "POST" and p.rstrip("/").endswith("query-analytics/list")
    if method != "GET" and not read_post and not args.yes:
        raise SystemExit(f"{method} {p} меняет данные или тратит квоту. Повтори с --yes, только если пользователь об этом просил.")
    data = api(method, p, params=params or None, body=body, cache=(method == "GET" or read_post), refresh=args.refresh,
               attempts=6 if (method == "GET" or read_post) else 1)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{method} {p}: ответ -> {args.out} ({len(text)} символов)")
    else:
        print(text)


# --------------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Yandex Webmaster API v4 для агентов. Подробно — SKILL.md")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def P(name, help_, func, host=True, csv_=True, limit=None):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)
        if host:
            p.add_argument("--host", required=True, help="example.com, https://example.com/ или host_id https:example.com:443")
        if csv_:
            p.add_argument("--csv", help="Сохранить ВСЕ строки в CSV (консоль показывает --limit)")
        if limit is not None:
            p.add_argument("--limit", type=int, default=limit, help=f"Строк в консоли (по умолчанию {limit}, 0 — все)")
        p.add_argument("--refresh", action="store_true", help="Мимо кэша текущего дня")
        return p

    def period(p, required=True):
        p.add_argument("--start", required=required, help="YYYY-MM-DD")
        p.add_argument("--end", required=required, help="YYYY-MM-DD")

    def numeric(p, fields):
        for f in fields:
            p.add_argument(f"--min-{f}", type=float)
            p.add_argument(f"--max-{f}", type=float)

    p = P("hosts", "Сайты аккаунта", cmd_hosts, host=False)
    p.add_argument("--all", action="store_true", help="Включая неглавные зеркала")
    P("summary", "ИКС, страниц в поиске/исключено, число проблем", cmd_summary, csv_=False)
    P("freshness", "До какой даты есть данные в каждом источнике", cmd_freshness)

    p = P("totals", "Итоги сайта по дням: показы, клики, CTR, позиции (с 2020)", cmd_totals, limit=60)
    period(p)
    p.add_argument("--device", choices=DEVICES, default="ALL")
    p.add_argument("--by", choices=["day", "week", "month"], default="day")

    p = P("popular", "Топ запросов (до 3000) за любой период в ~85 днях до последнего дня с данными", cmd_popular, limit=30)
    period(p)
    p.add_argument("--order-by", choices=["shows", "clicks"], default="shows")
    p.add_argument("--device", choices=DEVICES, default="ALL")
    p.add_argument("--max-rows", type=int, default=0, help="0 — все")
    p.add_argument("--workers", type=int, default=4)

    p = P("query-history", "Динамика одного запроса по дням", cmd_query_history, limit=60)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--query-id")
    g.add_argument("--query", help="Точный текст запроса (ищется в popular за период)")
    period(p)
    p.add_argument("--device", choices=DEVICES, default="ALL")

    def qa_common(p):
        p.add_argument("--device", choices=DEVICES, default="ALL")
        p.add_argument("--location", choices=LOCATIONS, help="Тип размещения (по умолчанию API — WEB_LOCATION)")
        p.add_argument("--region", action="append", help="ID региона Яндекса (1 — Москва и обл., 225 — Россия); можно несколько")
        p.add_argument("--day", help="Только строки с показами в этот день (YYYY-MM-DD); цифры всё равно за все 14 дней")
        p.add_argument("--workers", type=int, default=4, help="Параллельных вызовов (лимит 10 000/час на домен)")

    p = P("qa", "query-analytics: запросы или страницы за 14 дней, со спросом", cmd_qa, limit=30)
    p.add_argument("--group", choices=["query", "url"], default="query")
    for a, h in (("query-contains", "текст запроса содержит"), ("query-not-contains", "не содержит"),
                 ("query-equals", "точно равен"), ("url-contains", "адрес содержит"),
                 ("url-not-contains", "адрес не содержит"), ("url-equals", "точный адрес (URL или путь)")):
        p.add_argument(f"--{a}", action="append", help=h + "; можно несколько (AND)")
    p.add_argument("--stat-filter", action="append", metavar="FIELD:OP:VALUE[:FROM:TO]",
                   help="Сырой statistic_filter: условие на КАЖДЫЙ день диапазона, не на сумму")
    qa_common(p)
    p.add_argument("--by-date", action="store_true", help="Строка на ключ × день вместо суммы за окно")
    p.add_argument("--max-rows", type=int, default=5000, help="Сколько строк качать (0 — все, до 150 000)")
    numeric(p, ["impressions", "clicks", "ctr", "position", "demand"])
    p.add_argument("--sort-by", choices=["impressions", "clicks", "ctr", "position", "demand"], default="impressions")
    p.add_argument("--sort-asc", action="store_true")

    p = P("pairs", "Матрица запрос × страница и каннибализация (вызов на страницу)", cmd_pairs, limit=30)
    p.add_argument("--page", help="С кем конкурирует эта страница: её топ-запросы по всем страницам сайта")
    p.add_argument("--top-queries", type=int, default=100, help="С --page: сколько главных запросов страницы разложить")
    p.add_argument("--url", action="append", help="Страница (URL или путь); можно несколько")
    p.add_argument("--urls-file", help="Файл со страницами, по одной в строке")
    p.add_argument("--top-urls", type=int, default=50, help="Без --url: топ-N страниц по показам")
    p.add_argument("--min-urls", type=int, default=2, help="Порог страниц на запрос для списка каннибализации")
    p.add_argument("--groups-csv", help="CSV со списком каннибализации (--csv — все пары)")
    qa_common(p)

    p = P("history", "Динамика: страницы в поиске, события, обход, ИКС, ссылки", cmd_history, limit=60)
    p.add_argument("--kind", required=True, choices=list(HISTORY_KINDS))
    period(p, required=False)

    p = P("samples", "Списки: страницы в поиске, события с причинами, обход, ссылки", cmd_samples, limit=30)
    p.add_argument("--kind", required=True, choices=list(SAMPLE_KINDS))
    p.add_argument("--max-rows", type=int, default=0, help="0 — все (до 50 000)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--indicator", action="append", choices=["SITE_ERROR", "DISALLOWED_BY_USER", "UNSUPPORTED_BY_ROBOT"],
                   help="broken-links: тип битой ссылки")
    p.add_argument("--url-contains", help="Фильтр по адресу на нашей стороне")
    p.add_argument("--event", choices=["appeared", "removed"], help="events: только появившиеся или выпавшие")
    p.add_argument("--reason", help="events: причина исключения, напр. NOT_CANONICAL, DUPLICATE, LOW_QUALITY")
    p.add_argument("--since", help="Не раньше даты (YYYY-MM-DD) по главной дате строки")

    p = P("important-urls", "Мониторинг важных страниц или история одной (--url)", cmd_important_urls, limit=50)
    p.add_argument("--url", help="Полный URL из списка важных — вернуть его историю")

    p = P("diagnostics", "Проблемы сайта из диагностики", cmd_diagnostics)
    p.add_argument("--all", action="store_true", help="Все проверки, включая ABSENT/UNDEFINED")

    p = P("sitemaps", "Sitemap'ы: найденные роботом, дочерние, добавленные вручную", cmd_sitemaps)
    p.add_argument("--parent", help="ID индексного sitemap — показать дочерние")
    p.add_argument("--user-added", action="store_true", help="Добавленные пользователем")

    p = P("archive", "Все известные страницы: код, статус в поиске, canonical, title — одним CSV", cmd_archive,
          csv_=False, limit=10)
    p.add_argument("--out", required=True, help="CSV для результата (рядом сохранится сырой .csv.gz)")
    p.add_argument("--task-id", help="Продолжить уже запущенную задачу")
    p.add_argument("--wait", type=float, default=5, help="Ждать готовности, минут")

    p = P("recrawl", "Квота и очередь переобхода; отправка URL (--url ... --yes)", cmd_recrawl, limit=30)
    p.add_argument("--url", action="append", help="URL или путь; можно несколько")
    p.add_argument("--urls-file")
    p.add_argument("--yes", action="store_true", help="Реально отправить (тратит суточную квоту)")

    p = P("sitemap-recrawl", "Лимит приоритетного переобхода Sitemap; отправка (--id --yes)", cmd_sitemap_recrawl, csv_=False)
    p.add_argument("--id", help="sitemap_id из `sitemaps`")
    p.add_argument("--parent", help="ID родительского sitemap")
    p.add_argument("--yes", action="store_true", help="Реально отправить (10 раз в месяц на сайт)")

    p = P("pro-info", "PRO-выгрузка: лимиты, доступные даты, регионы (--regions)", cmd_pro_info)
    p.add_argument("--regions", nargs="?", const="", help="Справочник регионов; можно подстроку названия")

    p = P("pro-export", "PRO-выгрузка запрос × URL × регион за 550 дней (тратит лимит URL-дней)", cmd_pro_export, csv_=False)
    p.add_argument("--path", action="append", help="Страница (URL или путь); можно несколько")
    p.add_argument("--paths-file")
    p.add_argument("--date", action="append", help="YYYY-MM-DD; можно несколько")
    period(p, required=False)
    p.add_argument("--region", action="append", help="ID региона (без — все регионы)")
    p.add_argument("--paid", action="store_true", help="Расширенный платный тариф — только по прямому указанию пользователя")
    p.add_argument("--yes", action="store_true", help="Реально запустить")

    p = P("pro-status", "Статус PRO-задач и скачивание результата в CSV", cmd_pro_status, csv_=False, limit=20)
    p.add_argument("--task-id", action="append")
    p.add_argument("--out", help="CSV для результата (рядом сохранятся сырые .csv.gz)")
    p.add_argument("--wait", type=float, default=0, help="Ждать готовности, минут (опрос раз в минуту)")

    p = sub.add_parser("raw", help="Любой метод API: raw GET H/summary --host example.com")
    p.set_defaults(func=cmd_raw)
    p.add_argument("method", choices=["GET", "POST", "DELETE", "get", "post", "delete"])
    p.add_argument("path", help="H/... — путь сайта, U/... — путь пользователя, иначе от корня (v4/..., v4.1/...)")
    p.add_argument("--host")
    p.add_argument("--param", action="append", metavar="KEY=VALUE", help="Query-параметр; повторяемый ключ — несколько раз")
    p.add_argument("--body", help="JSON-тело или @файл.json")
    p.add_argument("--out", help="Сохранить ответ в файл")
    p.add_argument("--yes", action="store_true", help="Разрешить POST/DELETE, которые меняют данные")
    p.add_argument("--refresh", action="store_true")

    args = ap.parse_args()
    global FORCE_REFRESH
    FORCE_REFRESH = getattr(args, "refresh", False)
    _prune_cache()
    try:
        args.func(args)
    except ApiError as e:
        hint = HINTS.get(e.code) or HINTS.get(e.status) or ""
        if e.status >= 500 and e.method != "GET":
            hint = ("сбой на стороне Яндекса, повтора не было. Прежде чем запускать снова, проверь, не потрачено ли "
                    "что-то (pro-info, recrawl), и повтори позже")
        extra = {k: v for k, v in e.data.items() if k not in ("error_code", "error_message", "code", "message")}
        raise SystemExit(f"{e.method} {e.path}\n{e}" + (f"\n{json.dumps(extra, ensure_ascii=False)}" if extra else "") +
                         (f"\nПодсказка: {hint}" if hint else ""))


if __name__ == "__main__":
    main()
