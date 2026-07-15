#!/usr/bin/env python3
"""
GSC API — гибкие разовые запросы к Google Search Console.

Путь к credentials (client_secret.json/token.json, scope webmasters.readonly)
задаётся переменной окружения GSC_CONFIG_DIR — см. .env.example и SETUP.md.

Запросы кэшируются в cache\\ рядом с этим файлом, по точному совпадению
параметров (site, период, dimensions, фильтры) — повторный вопрос по тем же
данным не бьёт API повторно. --refresh обходит кэш.

CLI:
    python gsc.py sites
    python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
        --dimensions query --limit 20
    python gsc.py query --site sc-domain:example.com --start ... --end ... \
        --dimensions page --page-contains /episodes
    python gsc.py compare --site sc-domain:example.com \
        --period-a-start 2026-05-01 --period-a-end 2026-05-31 \
        --period-b-start 2026-06-01 --period-b-end 2026-06-30 \
        --dimensions page --top 20
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HOME = Path(__file__).resolve().parent
CACHE_DIR = HOME / "cache"
CACHE_MAX_AGE_DAYS = 30
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GSC_ROW_LIMIT = 25000  # лимит GSC API — максимум строк за один вызов searchanalytics.query


def _load_dotenv():
    """Подхватывает .env рядом со скриптом (KEY=VALUE построчно), не перезаписывая
    уже выставленные переменные окружения. Без внешних зависимостей (python-dotenv)."""
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

GSC_CONFIG_DIR = os.environ.get("GSC_CONFIG_DIR")
CLIENT_SECRET_FILE = Path(GSC_CONFIG_DIR) / "client_secret.json" if GSC_CONFIG_DIR else None
TOKEN_FILE = Path(GSC_CONFIG_DIR) / "token.json" if GSC_CONFIG_DIR else None


# --------------------------------------------------------------------------- auth

_service = None


def get_service():
    """Авто-рефреш токена при истечении access_token. Кэшируется в процессе —
    повторные вызовы (напр. cmd_compare с двумя run_query) не пересоздают сервис заново."""
    global _service
    if _service is not None:
        return _service

    if not GSC_CONFIG_DIR:
        raise SystemExit(
            "GSC_CONFIG_DIR не задан. Скопируй .env.example в .env рядом с gsc.py "
            "и укажи путь к папке с client_secret.json/token.json — см. SETUP.md."
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_FILE.exists():
                raise FileNotFoundError(f"OAuth-клиент не найден: {CLIENT_SECRET_FILE}. См. SETUP.md.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    _service = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
    return _service


def list_sites(service):
    return service.sites().list().execute().get("siteEntry", [])


# --------------------------------------------------------------------------- cache

def _cache_key(**params):
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_read(key, field="records"):
    path = CACHE_DIR / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))[field]
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _cache_write(key, params, data, field="records"):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps({"params": params, field: data}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _cache_or_fetch(key, refresh, fetch_fn, cache_params, field="records"):
    """Общий кэш-паттерн (используют run_query и cmd_inspect): читает кэш, если не
    --refresh, иначе вызывает fetch_fn и пишет результат в кэш. Возвращает (данные, из_кэша)."""
    if not refresh:
        cached = _cache_read(key, field=field)
        if cached is not None:
            return cached, True
    result = fetch_fn()
    _cache_write(key, cache_params, result, field=field)
    return result, False


def _prune_cache(max_age_days=CACHE_MAX_AGE_DAYS):
    """Удаляет файлы кэша, не обновлявшиеся дольше max_age_days. Вызывается при каждом запуске."""
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"[cache] удалено {removed} файлов старше {max_age_days} дн.", file=sys.stderr)


# --------------------------------------------------------------------------- filters

_OPERATOR_ALIASES = {
    "equals": "equals",
    "notequals": "notEquals",
    "contains": "contains",
    "notcontains": "notContains",
    "regex": "includingRegex",
    "includingregex": "includingRegex",
    "notregex": "excludingRegex",
    "excludingregex": "excludingRegex",
}
_DIMENSION_ALIASES = {
    # date/hour сюда не входят: GSC API принимает их только в --dimensions (группировка),
    # но не как dimensionFilterGroups[].filters[].dimension — это ограничение самого API.
    "query": "query",
    "page": "page",
    "country": "country",
    "device": "device",
    "searchappearance": "searchAppearance",
}
_VALID_DIMENSIONS = sorted(set(_DIMENSION_ALIASES.values()) | {"date", "hour"})
_DEVICE_VALUES = ("MOBILE", "DESKTOP", "TABLET")

# (dimension, operator, argparse-dest, help) — единый источник и для регистрации
# флагов в _add_filter_args, и для сборки фильтров в build_filter_groups.
_NAMED_FILTERS = [
    ("page", "equals", "page_equals", "Точное совпадение URL страницы"),
    ("page", "contains", "page_contains", "URL страницы содержит подстроку"),
    ("query", "equals", "query_equals", "Точное совпадение поискового запроса"),
    ("query", "contains", "query_contains", "Запрос содержит подстроку"),
    ("country", "equals", "country", "3-буквенный код страны, напр. rus"),
    ("device", "equals", "device", "MOBILE / DESKTOP / TABLET"),
]


def _normalize_key(s):
    return s.strip().lower().replace("_", "").replace("-", "")


def _make_filter(dimension, operator, expression):
    """Единая точка сборки ApiDimensionFilter dict — используется и именованными
    шорткатами (--page-equals и т.п.), и --filter."""
    if dimension == "device":
        expression = expression.upper()
        if expression not in _DEVICE_VALUES:
            raise SystemExit(f"--device: недопустимое значение {expression!r}. Допустимо: {', '.join(_DEVICE_VALUES)}")
    return {"dimension": dimension, "operator": operator, "expression": expression}


def _parse_generic_filter(raw):
    """'dimension:operator:expression' -> ApiDimensionFilter dict. Expression may contain ':' (URLs)."""
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise SystemExit(f"--filter должен быть в формате dimension:operator:expression, получено: {raw!r}")
    dim_raw, op_raw, expression = parts
    dimension = _DIMENSION_ALIASES.get(_normalize_key(dim_raw))
    operator = _OPERATOR_ALIASES.get(_normalize_key(op_raw))
    if not dimension:
        raise SystemExit(f"--filter: неизвестное измерение {dim_raw!r}. Допустимо: {', '.join(sorted(set(_DIMENSION_ALIASES.values())))}")
    if not operator:
        raise SystemExit(f"--filter: неизвестный оператор {op_raw!r}. Допустимо: equals, not_equals, contains, not_contains, regex, not_regex")
    return _make_filter(dimension, operator, expression)


def build_filter_groups(args):
    """CLI-флаги фильтров -> dimensionFilterGroups для searchanalytics.query."""
    filters = []
    for dimension, operator, attr, _help in _NAMED_FILTERS:
        value = getattr(args, attr, None)
        if value:
            filters.append(_make_filter(dimension, operator, value))
    for raw in getattr(args, "filter", None) or []:
        filters.append(_parse_generic_filter(raw))
    return [{"filters": filters}] if filters else None


def build_extra_body(args):
    """Необязательные поля searchanalytics.query: dimensionFilterGroups / type / dataState / aggregationType."""
    body = {}
    filter_groups = build_filter_groups(args)
    if filter_groups:
        body["dimensionFilterGroups"] = filter_groups
    if getattr(args, "search_type", None):
        body["type"] = args.search_type.upper()
    if getattr(args, "data_state", None):
        body["dataState"] = args.data_state.upper()
    if getattr(args, "aggregation_type", None):
        body["aggregationType"] = args.aggregation_type.upper()
    return body or None


# --------------------------------------------------------------------------- query

def _normalize_page(value):
    """GSC репортит #anchor-варианты одной страницы как разные значения page —
    схлопываем их в одно (без #fragment), иначе они дают ложные срабатывания и в
    поиске каннибализации (groups), и в сравнении периодов (compare)."""
    return value.split("#", 1)[0]


def _merge_page_duplicates(records, dimensions):
    """Схлопывает записи, совпадающие по всем dimensions после нормализации page,
    суммируя clicks/impressions и пересчитывая ctr/position (impressions-взвешенно)."""
    if "page" not in dimensions:
        return records
    merged = {}
    for r in records:
        r = {**r, "page": _normalize_page(r["page"])}
        key = tuple(r[d] for d in dimensions)
        m = merged.get(key)
        if m is None:
            merged[key] = r
            continue
        total_impressions = m["impressions"] + r["impressions"]
        if total_impressions:
            m["position"] = round(
                (m["position"] * m["impressions"] + r["position"] * r["impressions"]) / total_impressions, 1,
            )
        m["clicks"] += r["clicks"]
        m["impressions"] = total_impressions
        m["ctr"] = round(m["clicks"] / total_impressions * 100, 2) if total_impressions else 0
    return list(merged.values())


def fetch_search_analytics(service, site_url, start_date, end_date, dimensions,
                            row_limit=GSC_ROW_LIMIT, max_rows=200000, extra_body=None):
    """searchanalytics.query с автоматической пагинацией (лимит GSC — 25k строк за вызов)."""
    all_rows = []
    start_row = 0
    while True:
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": row_limit,
            "startRow": start_row,
        }
        if extra_body:
            body.update(extra_body)

        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = response.get("rows", [])
        if not rows:
            break

        all_rows.extend(rows)
        start_row += len(rows)
        if len(rows) < row_limit:
            break
        if start_row >= max_rows:
            print(f"[warn] достигнут предел {max_rows} строк — результат может быть неполным, "
                  f"сузьте период или добавьте фильтры", file=sys.stderr)
            break

    records = []
    for row in all_rows:
        record = dict(zip(dimensions, row["keys"]))
        record.update({
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0) * 100, 2),
            "position": round(row.get("position", 0), 1),
        })
        records.append(record)
    return _merge_page_duplicates(records, dimensions)


def run_query(site, start, end, dimensions, extra_body=None, row_limit=GSC_ROW_LIMIT, refresh=False):
    """Список dict-записей за период. Кэшируется по точному набору параметров
    (включая фильтры, searchType, dataState, aggregationType). row_limit — размер
    страницы пагинации, на итоговый набор данных не влияет, поэтому в ключ кэша
    не входит."""
    identity = {"site": site, "start": start, "end": end, "dimensions": dimensions, "extra": extra_body}
    key = _cache_key(**identity)

    records, from_cache = _cache_or_fetch(
        key, refresh,
        lambda: fetch_search_analytics(get_service(), site, start, end, dimensions,
                                        row_limit=row_limit, extra_body=extra_body),
        identity,
    )
    tag, verb = ("cache", "строк из кэша") if from_cache else ("api", "строк получено и закэшировано")
    print(f"[{tag}] {len(records)} {verb} ({key})", file=sys.stderr)
    return records


# --------------------------------------------------------------------------- output

def print_table(records, csv_path=None, limit=None):
    """CSV всегда сохраняет ПОЛНЫЙ набор записей; limit обрезает только вывод в консоль."""
    import pandas as pd

    if not records:
        print("Нет данных.")
        return
    df = pd.DataFrame(records)
    if csv_path:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"[OK] Сохранено: {csv_path} ({len(df)} строк)")
    if limit is not None:
        df = df.head(limit)
    print(df.to_string(index=False))


# --------------------------------------------------------------------------- commands

def cmd_auth(args):
    get_service()
    print(f"OK — токен сохранён: {TOKEN_FILE}")


def cmd_sites(args):
    service = get_service()
    sites = list_sites(service)
    if not sites:
        print("Нет доступных property.")
        return
    width = max(len(s["siteUrl"]) for s in sites)
    for s in sorted(sites, key=lambda x: x["siteUrl"]):
        print(f"{s['siteUrl']:<{width}}  {s['permissionLevel']}")
    print(f"\n{len(sites)} property.")


_NUMERIC_FIELDS = ["clicks", "impressions", "ctr", "position"]
_NUMERIC_FIELD_HELP = {
    "clicks": ("Оставить строки с clicks >= значения", "Оставить строки с clicks <= значения"),
    "impressions": ("Оставить строки с impressions >= значения", "Оставить строки с impressions <= значения"),
    "ctr": ("CTR в процентах (напр. 2.5) — оставить строки с ctr >= значения",
            "CTR в процентах — оставить строки с ctr <= значения"),
    "position": ("Оставить строки с position >= значения (число больше — позиция хуже)",
                 "Оставить строки с position <= значения. Напр. --min-position 4 --max-position 15 "
                 "— запросы вне топ-3, но не глубже 2-й страницы выдачи"),
}


def apply_numeric_filters(records, args):
    """Клиентская фильтрация по метрикам (--min-*/--max-*). GSC API фильтрует
    только по измерениям (dimensionFilterGroups), метрики приходится резать
    после получения данных — это ограничение самого API, не только скрипта."""
    bounds = []
    for field in _NUMERIC_FIELDS:
        min_v = getattr(args, f"min_{field}", None)
        max_v = getattr(args, f"max_{field}", None)
        if min_v is not None:
            bounds.append((field, "min", min_v))
        if max_v is not None:
            bounds.append((field, "max", max_v))
    if not bounds:
        return records
    return [
        r for r in records
        if all(r[f] >= v if kind == "min" else r[f] <= v for f, kind, v in bounds)
    ]


def _add_numeric_filter_args(p):
    for field in _NUMERIC_FIELDS:
        min_help, max_help = _NUMERIC_FIELD_HELP[field]
        p.add_argument(f"--min-{field}", type=float, help=min_help)
        p.add_argument(f"--max-{field}", type=float, help=max_help)


def cmd_query(args):
    extra_body = build_extra_body(args)
    records = run_query(args.site, args.start, args.end, args.dimensions,
                         extra_body=extra_body, row_limit=args.row_limit,
                         refresh=args.refresh)
    records = apply_numeric_filters(records, args)
    records.sort(key=lambda r: r.get(args.sort_by, 0), reverse=not args.sort_asc)
    print_table(records, csv_path=args.csv, limit=args.limit)


_ZERO_ROW = {"clicks": 0, "impressions": 0, "position": 0}


def cmd_compare(args):
    extra_body = build_extra_body(args)
    dims = args.dimensions

    a = run_query(args.site, args.period_a_start, args.period_a_end, dims,
                  extra_body=extra_body, row_limit=args.row_limit, refresh=args.refresh)
    b = run_query(args.site, args.period_b_start, args.period_b_end, dims,
                  extra_body=extra_body, row_limit=args.row_limit, refresh=args.refresh)

    a_by_key = {tuple(r[d] for d in dims): r for r in a}
    b_by_key = {tuple(r[d] for d in dims): r for r in b}

    rows = []
    for k in sorted(set(a_by_key) | set(b_by_key)):
        ra = a_by_key.get(k, _ZERO_ROW)
        rb = b_by_key.get(k, _ZERO_ROW)
        row = dict(zip(dims, k))
        row.update({
            "clicks_a": ra["clicks"], "clicks_b": rb["clicks"],
            "delta_clicks": rb["clicks"] - ra["clicks"],
            "impressions_a": ra["impressions"], "impressions_b": rb["impressions"],
            "delta_impressions": rb["impressions"] - ra["impressions"],
            "position_a": ra["position"], "position_b": rb["position"],
        })
        rows.append(row)

    rows.sort(key=lambda r: r[args.sort_by], reverse=(args.order == "desc"))
    print_table(rows, csv_path=args.csv, limit=args.top)


def cmd_groups(args):
    """Группировка по одному измерению с подсчётом уникальных комбинаций остальных —
    обобщённый примитив для задач вида "один query -> несколько page" (каннибализация)
    и любых похожих (напр. один page -> несколько searchAppearance)."""
    extra_body = build_extra_body(args)
    dims = args.dimensions
    if args.group_by not in dims:
        raise SystemExit(f"--group-by {args.group_by!r} должен быть одним из --dimensions {dims}")
    other_dims = [d for d in dims if d != args.group_by]
    if not other_dims:
        raise SystemExit("--dimensions должен содержать хотя бы одно измерение, кроме --group-by")

    records = run_query(args.site, args.start, args.end, dims,
                         extra_body=extra_body, row_limit=args.row_limit, refresh=args.refresh)

    groups = defaultdict(lambda: {"clicks": 0, "impressions": 0, "members": set()})
    for r in records:
        g = groups[r[args.group_by]]
        g["clicks"] += r["clicks"]
        g["impressions"] += r["impressions"]
        g["members"].add(tuple(r[d] for d in other_dims))

    rows = [
        {
            args.group_by: key,
            "distinct_count": len(g["members"]),
            "clicks": g["clicks"],
            "impressions": g["impressions"],
            "members": "; ".join(" / ".join(m) for m in sorted(g["members"])),
        }
        for key, g in groups.items() if len(g["members"]) >= args.min_count
    ]
    rows.sort(key=lambda r: r[args.sort_by], reverse=(args.order == "desc"))
    print_table(rows, csv_path=args.csv, limit=args.limit)


def cmd_sitemaps(args):
    service = get_service()
    if args.feedpath:
        sitemap = service.sitemaps().get(siteUrl=args.site, feedpath=args.feedpath).execute()
        print(json.dumps(sitemap, indent=2, ensure_ascii=False))
        return

    kwargs = {"siteUrl": args.site}
    if args.sitemap_index:
        kwargs["sitemapIndex"] = args.sitemap_index
    entries = service.sitemaps().list(**kwargs).execute().get("sitemap", [])
    if not entries:
        print("Sitemaps не найдены.")
        return

    rows = []
    for s in entries:
        base = {
            "path": s.get("path"),
            "type": s.get("type"),
            "isPending": s.get("isPending"),
            "isSitemapsIndex": s.get("isSitemapsIndex"),
            "lastSubmitted": s.get("lastSubmitted"),
            "lastDownloaded": s.get("lastDownloaded"),
            "warnings": s.get("warnings"),
            "errors": s.get("errors"),
        }
        for c in s.get("contents") or [{}]:
            rows.append({**base, "content_type": c.get("type"),
                         "submitted": c.get("submitted"), "indexed": c.get("indexed")})
    print_table(rows, csv_path=args.csv)


def cmd_inspect(args):
    """urlInspection.index.inspect — статус индексации одного URL (по одному URL за вызов)."""
    key = _cache_key(command="inspect", site=args.site, url=args.url, language=args.language)

    def fetch():
        body = {"inspectionUrl": args.url, "siteUrl": args.site, "languageCode": args.language}
        return get_service().urlInspection().index().inspect(body=body).execute()

    result, from_cache = _cache_or_fetch(
        key, args.refresh, fetch, {"site": args.site, "url": args.url}, field="result",
    )
    print(f"[{'cache' if from_cache else 'api'}] {'' if from_cache else 'закэшировано '}({key})", file=sys.stderr)

    if args.raw:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    ir = result.get("inspectionResult", {})
    idx = ir.get("indexStatusResult", {})
    summary = {
        "verdict": idx.get("verdict"),
        "coverageState": idx.get("coverageState"),
        "robotsTxtState": idx.get("robotsTxtState"),
        "indexingState": idx.get("indexingState"),
        "pageFetchState": idx.get("pageFetchState"),
        "lastCrawlTime": idx.get("lastCrawlTime"),
        "crawledAs": idx.get("crawledAs"),
        "googleCanonical": idx.get("googleCanonical"),
        "userCanonical": idx.get("userCanonical"),
        "sitemap": idx.get("sitemap"),
        "referringUrls": idx.get("referringUrls"),
    }
    width = max(len(k) for k in summary)
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")
    if "mobileUsabilityResult" in ir:
        print(f"\nmobileUsability: {ir['mobileUsabilityResult'].get('verdict')}")
    if "richResultsResult" in ir:
        print(f"richResults: {ir['richResultsResult'].get('verdict')}")
    if "ampResult" in ir:
        print(f"amp: {ir['ampResult'].get('verdict')}")
    print("\n(полный ответ API — добавь --raw)")


# --------------------------------------------------------------------------- CLI

def _row_limit(value):
    v = int(value)
    if not 1 <= v <= GSC_ROW_LIMIT:
        raise argparse.ArgumentTypeError(f"должен быть от 1 до {GSC_ROW_LIMIT}")
    return v


def _add_filter_args(p):
    for _, _, attr, help_text in _NAMED_FILTERS:
        p.add_argument(f"--{attr.replace('_', '-')}", help=help_text)
    p.add_argument("--filter", action="append", metavar="DIM:OP:EXPR",
                    help="Произвольный фильтр, можно указывать несколько раз. "
                         "DIM = query|page|country|device|searchAppearance. "
                         "OP = equals|not_equals|contains|not_contains|regex|not_regex "
                         "(regex/not_regex — RE2, includingRegex/excludingRegex в терминах API). "
                         "Пример: --filter query:not_contains:brand-name "
                         "--filter page:regex:/episodes/\\d+$")
    p.add_argument("--search-type", choices=["web", "image", "video", "news", "discover", "google_news"],
                    help="Тип поиска (по умолчанию web). discover/google_news — трафик из Google Discover / Google News")
    p.add_argument("--data-state", choices=["final", "all", "hourly_all"],
                    help="final — только финализированные данные (по умолчанию, задержка ~2-3 дня); "
                         "all — включая свежие неполные данные за последние дни; "
                         "hourly_all — почасовые данные за последние 10 дней (нужно --dimensions hour)")
    p.add_argument("--aggregation-type", choices=["auto", "by_property", "by_page", "by_news_showcase_panel"],
                    help="Как агрегируются клики/позиция (по умолчанию auto)")
    p.add_argument("--row-limit", type=_row_limit, default=GSC_ROW_LIMIT, help="Лимит строк на запрос к API (пагинация, 1-25000)")
    p.add_argument("--refresh", action="store_true", help="Игнорировать кэш, запросить заново")
    p.add_argument("--csv", help="Сохранить ПОЛНЫЙ результат в CSV (--limit/--top ограничивает только вывод в консоль)")


def main():
    parser = argparse.ArgumentParser(description="GSC API — гибкие запросы к Google Search Console")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("sites", help="Список доступных property").set_defaults(func=cmd_sites)
    sub.add_parser("auth", help="Разовая авторизация в браузере (если нет token.json)").set_defaults(func=cmd_auth)

    q = sub.add_parser("query", help="Гибкий запрос: разрез + фильтры + период")
    q.set_defaults(func=cmd_query)
    q.add_argument("--site", required=True, help="sc-domain:example.com или https://example.com/")
    q.add_argument("--start", required=True)
    q.add_argument("--end", required=True)
    q.add_argument("--dimensions", nargs="+", required=True, choices=_VALID_DIMENSIONS,
                   help="query, page, country, device, date, searchAppearance, "
                        "hour (только с --data-state hourly_all)")
    q.add_argument("--limit", type=int, default=50,
                   help="Сколько строк показать в консоли после сортировки (не влияет на --csv — там всегда полный результат)")
    q.add_argument("--sort-by", choices=_NUMERIC_FIELDS, default="clicks",
                   help="Поле сортировки (по умолчанию clicks)")
    q.add_argument("--sort-asc", action="store_true", help="Сортировать по возрастанию (по умолчанию — убывание)")
    _add_numeric_filter_args(q)
    _add_filter_args(q)

    c = sub.add_parser("compare", help="Сравнение двух периодов — поиск упавших/выросших")
    c.set_defaults(func=cmd_compare)
    c.add_argument("--site", required=True)
    c.add_argument("--period-a-start", required=True)
    c.add_argument("--period-a-end", required=True)
    c.add_argument("--period-b-start", required=True)
    c.add_argument("--period-b-end", required=True)
    c.add_argument("--dimensions", nargs="+", default=["page"], choices=_VALID_DIMENSIONS)
    c.add_argument("--top", type=int, default=20,
                   help="Сколько строк показать в консоли (не влияет на --csv — там всегда полный результат)")
    c.add_argument("--sort-by", choices=["delta_clicks", "delta_impressions"], default="delta_clicks",
                   help="Поле сортировки (по умолчанию delta_clicks)")
    c.add_argument("--order", choices=["asc", "desc"], default="asc",
                   help="asc — сначала самые упавшие (по умолчанию), desc — сначала самые выросшие")
    _add_filter_args(c)

    gr = sub.add_parser("groups", help="Группировка по измерению с подсчётом уникальных значений остальных "
                                        "(напр. каннибализация: query -> несколько page)")
    gr.set_defaults(func=cmd_groups)
    gr.add_argument("--site", required=True)
    gr.add_argument("--start", required=True)
    gr.add_argument("--end", required=True)
    gr.add_argument("--dimensions", nargs="+", required=True, choices=_VALID_DIMENSIONS,
                     help="Минимум 2 измерения, напр. query page")
    gr.add_argument("--group-by", required=True, help="Одно из --dimensions, по которому группировать")
    gr.add_argument("--min-count", type=int, default=2,
                     help="Показать только группы, где уникальных значений остальных измерений >= min-count "
                          "(по умолчанию 2 — то есть только реальные дубли)")
    gr.add_argument("--limit", type=int, default=50, help="Сколько групп показать в консоли")
    gr.add_argument("--sort-by", choices=["clicks", "impressions", "distinct_count"], default="clicks",
                     help="Поле сортировки (по умолчанию clicks)")
    gr.add_argument("--order", choices=["asc", "desc"], default="desc",
                     help="desc — сначала самые большие значения (по умолчанию), asc — сначала самые маленькие")
    _add_filter_args(gr)

    sm = sub.add_parser("sitemaps", help="Список sitemap'ов сайта (или детали одного через --feedpath)")
    sm.set_defaults(func=cmd_sitemaps)
    sm.add_argument("--site", required=True)
    sm.add_argument("--feedpath", help="Точный URL конкретного sitemap — вернуть детали только по нему")
    sm.add_argument("--sitemap-index", help="Ограничить список дочерними sitemap этого sitemap-индекса")
    sm.add_argument("--csv", help="Сохранить результат в CSV")

    insp = sub.add_parser("inspect", help="URL Inspection — статус индексации одного URL")
    insp.set_defaults(func=cmd_inspect)
    insp.add_argument("--site", required=True, help="Property как в Search Console (sc-domain:... или https://...)")
    insp.add_argument("--url", required=True, help="Полный URL страницы для проверки")
    insp.add_argument("--language", default="ru", help="Язык сообщений об ошибках (IETF BCP-47), по умолчанию ru")
    insp.add_argument("--raw", action="store_true", help="Вывести полный JSON-ответ вместо краткой сводки")
    insp.add_argument("--refresh", action="store_true", help="Игнорировать кэш, запросить заново")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    _prune_cache()
    args.func(args)


if __name__ == "__main__":
    main()
