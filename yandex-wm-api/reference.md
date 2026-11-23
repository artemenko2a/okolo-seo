# API Яндекс Вебмастера v4 — справочник

Выжимка официальной документации (https://yandex.ru/dev/webmaster/doc/ru/, версия 4.1)
плюс то, что проверено на живом API. Читай, когда нужен метод без отдельной команды
(`python ywm.py raw ...`) или точный формат ответа. Готовые команды — в [SKILL.md](SKILL.md).

Содержание: [Основы](#основы) · [Карта методов](#карта-методов) · [Запросы](#поисковые-запросы) ·
[query-analytics](#query-analytics) · [PRO-выгрузка](#pro-выгрузка) · [Индексирование](#индексирование) ·
[Ссылки](#ссылки) · [Переобход и Sitemap](#переобход-и-sitemap) · [Сайты и права](#сайты-и-права) ·
[Фиды](#фиды) · [Enum'ы](#enumы) · [Причины исключения](#причины-исключения) · [Ошибки и лимиты](#ошибки-и-лимиты)

## Основы

- Корень: `https://api.webmaster.yandex.net/`, версия в пути: `v4/` (= 4.1). Приоритетный
  переобход Sitemap есть только под `v4.1/`.
- Заголовки: `Authorization: OAuth <токен>`, `Accept: application/json`; для тела —
  `Content-Type: application/json`. XML — через `Accept: application/xml`, не нужен.
- Токен: приложение на oauth.yandex.ru с доступом `webmaster:hostinfo` (чтение, ссылки) и
  `webmaster:verify` (добавление сайтов, подтверждение прав). Живёт 6 месяцев, refresh в
  документированном способе нет.
- `user-id` нужен в каждом пути: `GET v4/user` → `{"user_id": 123}`.
- `host-id` — не URL, а `протокол:домен:порт`: `https:example.com:443`, `http:xn--d1acpjx3f.xn--p1ai:80`.
  Брать из `GET v4/user/{u}/hosts`, не собирать руками.
- Даты в ответах — `2026-01-01T00:00:00.000+03:00` (местами с запятой: `,000+0300`). В
  `query-analytics` и PRO — просто `YYYY-MM-DD`. Параметры `date_from/date_to` принимают `YYYY-MM-DD`.
- `limit` по умолчанию мелкий (20 у query-analytics, 10 у ссылок): всегда задавай явно.

## Карта методов

Все пути, кроме первых двух, — под `v4/user/{user-id}/hosts/{host-id}/` (`H/` в `raw`).

| Путь | GET | POST | DELETE | Команда |
|---|---|---|---|---|
| `v4/user` | user_id | | | — |
| `v4/user/{u}/hosts` | список сайтов | добавить сайт `{"host_url"}` | | `hosts` |
| `` (сам хост) | инфо о сайте | | удалить сайт | `summary` |
| `summary` | ИКС, страниц в поиске, исключено, проблемы | | | `summary` |
| `sqi-history` | история ИКС | | | `history --kind sqi` |
| `owners` | кто подтвердил права | | | `raw` |
| `verification` | статус подтверждения | запустить проверку `?verification_type=` | | `raw` |
| `diagnostics` | все проверки с состоянием | | | `diagnostics` |
| `search-queries/popular` | топ запросов | | | `popular` |
| `search-queries/all/history` | итоги по дням | | | `totals` |
| `search-queries/{query-id}/history` | динамика запроса | | | `query-history` |
| `query-analytics/list` | | мониторинг запросов | | `qa`, `pairs` |
| `indexing/history` | обход по HTTP-кодам | | | `history --kind crawl` |
| `indexing/samples` | примеры загруженных страниц | | | `samples --kind crawl` |
| `indexing/archive` · `indexing/archive/{task-id}` | статус | сформировать архив | | `archive` |
| `search-urls/in-search/history` · `.../samples` | динамика · примеры | | | `history`/`samples --kind in-search` |
| `search-urls/events/history` · `.../samples` | динамика · примеры с причинами | | | `history`/`samples --kind events` |
| `important-urls` · `important-urls/history?url=` | важные страницы · история одной | | | `important-urls` |
| `links/external/samples` · `.../history?indicator=LINKS_TOTAL_COUNT` | внешние ссылки | | | `samples`/`history --kind external-links` |
| `links/internal/broken/samples` · `.../history` | битые внутренние | | | `samples`/`history --kind broken-links` |
| `recrawl/queue` · `recrawl/queue/{task-id}` · `recrawl/quota` | очередь, задача, квота | отправить `{"url"}` | | `recrawl` |
| `sitemaps` · `sitemaps/{id}` | найденные роботом | | | `sitemaps` |
| `user-added-sitemaps` · `user-added-sitemaps/{id}` | добавленные вручную | добавить `{"url"}` | удалить | `sitemaps --user-added`, `raw` |
| `v4.1/.../sitemaps/recrawl` · `v4.1/.../sitemaps/{id}/recrawl?parent_id=` | лимит | приоритетный переобход | | `sitemap-recrawl` |
| `pro/limits` · `pro/serp/dates` · `pro/regions?filter=&limit=` | лимиты, даты, регионы | | | `pro-info` |
| `pro/serp/queries/download/` · `.../{task-id}` | статус и ссылка | создать выгрузку | | `pro-export`, `pro-status` |
| `feeds/...` | см. [Фиды](#фиды) | | | `raw` |

Удалены из API: `original-texts` (оригинальные тексты), `tic-history`.

## Поисковые запросы

**`search-queries/popular`** — топ до 3000 запросов за период, 500 за вызов.
Параметры: `order_by` (обязателен: `TOTAL_SHOWS` | `TOTAL_CLICKS`), `query_indicator` (повторяемый:
`TOTAL_SHOWS`, `TOTAL_CLICKS`, `AVG_SHOW_POSITION`, `AVG_CLICK_POSITION`), `device_type_indicator`,
`date_from`, `date_to`, `offset`, `limit`. Без дат — последняя неделя.
Ответ: `{"queries":[{"query_id","query_text","indicators":{...}}], "date_from","date_to","count"}`.
`date_from/date_to` в ответе могут сузиться до дней, где есть данные. Индикатор пропадает,
если значения нет. На деле принимает любой период в ~85 днях до последнего дня с данными, включая один день;
цифры за день совпадают с query-analytics.

**`search-queries/all/history`** — те же индикаторы по дням для всего сайта, без разбивки.
Ответ: `{"indicators":{"TOTAL_SHOWS":[{"date","value"}],...}}`. История по дням с 2020;
диапазон в несколько лет рвёт соединение, поэтому бери куски по году. За дни без данных — нули.

**`search-queries/{query-id}/history`** — то же для одного запроса, `query-id` из popular
(протухает → `404 QUERY_ID_NOT_FOUND`). По документации ответ — список `queries`, на деле один
объект `{"query_id","query_text","indicators"}`.

## query-analytics

`POST H/query-analytics/list` — единственный бесплатный метод со связкой «запрос × страница × день»
и спросом. Данные только за последние 14 дней (дат в параметрах нет).

```json
{
  "offset": 0, "limit": 500,
  "text_indicator": "QUERY",
  "device_type_indicator": "ALL",
  "search_location": "WEB_LOCATION",
  "region_ids": [1, 10174],
  "filters": {
    "text_filters": [{"text_indicator": "URL", "operation": "TEXT_MATCH", "value": "/blog/post/"}],
    "statistic_filters": [{"statistic_field": "IMPRESSIONS", "operation": "GREATER_THAN",
                           "value": "0", "from": "2026-11-10", "to": "2026-11-10"}]
  },
  "sort_by_date": {"date": "2026-11-10", "statistic_field": "IMPRESSIONS", "by": "DESC"}
}
```

| Поле | Значения |
|---|---|
| `text_indicator` | по чему группировать: `QUERY` (по умолчанию) или `URL` |
| `limit` | 1–500, по умолчанию 20 |
| `device_type_indicator` | `ALL`, `DESKTOP`, `MOBILE_AND_TABLET`, `MOBILE`, `TABLET` |
| `search_location` | `WEB_LOCATION` (по умолчанию, органика), `ALL_LOCATIONS` (органика + динамические места Директа), `ALL_LOCATIONS_ORGANIC` (без дополнительных кликов), `DYNAMIC_LOCATION_ALL`, `DYNAMIC_LOCATION_BASIC`, `DYNAMIC_LOCATION_ADDITIONAL` |
| `region_ids` | ID регионов Яндекса; пусто — все |
| `text_filters[].operation` | `TEXT_CONTAINS`, `TEXT_MATCH` (точное), `TEXT_DOES_NOT_CONTAIN` |
| `statistic_filters[]` | `statistic_field` + `operation` (`LESS_THAN`, `GREATER_THAN`, `LESS_EQUAL`, `GREATER_EQUAL`, `EQUAL`) + `value` (строка) + `from`/`to` — обязательны и внутри окна, иначе `400 RESTRICTIONS_VIOLATED` (в сообщении — границы окна). Условие на **каждый** день диапазона |
| `sort_by_date` | сортировка по метрике одного дня; без неё — по показам за окно, по убыванию |

Ответ:
```json
{"count": 5175, "text_indicator_to_statistics": [{
  "text_indicator": {"type": "URL", "value": "/blog/post/"},
  "popular_complementary_indicator": {"type": "QUERY", "value": "самый частый запрос"},
  "statistics": [{"date": "2026-11-10", "field": "IMPRESSIONS", "value": 12.0}, ...]}]}
```
Поля статистики: `IMPRESSIONS`, `CLICKS`, `CTR` (в %), `POSITION` (может отсутствовать), `DEMAND`
(спрос на запрос в целом). Адрес — путь. Срез «запрос × страница» получается фильтром: группировка
`QUERY` + `text_filters` URL `TEXT_MATCH` путь даёт статистику по этой странице; обратное направление
(`URL` + фильтр по запросу) раскладывает запрос по страницам. Фильтр URL совместим с `region_ids`.

Проверено на живых сайтах: потолок ~150 000 запросов на окно; `count` — уникальные строки за окно;
`statistic_filters` с `from = to = D` и `IMPRESSIONS > 0` отдаёт ровно ключи с показами в день D
(статистика всё равно за 14 дней); сумма по URL совпадает с `all/history` по кликам, по показам — с
расхождением в единицы в день. ~0,3 вызова в секунду в один поток, 8 потоков ≈ 10 000 в час = лимит.

## PRO-выгрузка

Расширенная выгрузка запросов по URL (β), то же в интерфейсе: Эффективность → Детальный анализ.

- `GET pro/limits` → `{"limits":[{"owner":"example.com","feature":"PRO_SERP","limit":100,"used":0,"remaining":100,"period_start","period_end","is_active","tariff_id"}]}`. Лимит на домен.
- `GET pro/serp/dates` → `{"dates":[...]}` — 550 дат, свежайшая отстаёт на ~2 дня.
- `GET pro/regions?filter=&limit=` → `{"regions":[{"id","name"}]}` — 500+ записей вперемешку: страны,
  округа, субъекты, группы, города. `filter` — подстрока названия.
- `POST pro/serp/queries/download/` с `{"dates":[...],"paths":["/blog/"],"region_ids":[],"use_pro_tariff":"false"}` →
  `{"task_id","free_quota_used","pro_quota_used","total_quota_used","free_quota_remaining","pro_quota_remaining"}`.
  `paths` начинаются с `/`, `use_pro_tariff` — строка. 1 URL × 1 день = 1 запрос квоты; в одной задаче
  дат и URL не больше 100 (`413 PAYLOAD_TOO_LARGE`).
- `GET pro/serp/queries/download/{task-id}` → `{"download_status":"IN_PROGRESS"}` |
  `{"download_status":"SUCCESS","url":"..."}` (ссылка живёт 24 ч) | `{"download_status":"FAILED","error_code","error_message"}`.
- Файл: CSV.gz, колонки `date,host,path,query,regionId,regionName,clicks,impressions,position`, все поля
  в кавычках, строка = запрос × регион. Кавычки внутри поля экранированы `\"`, а запрос может
  кончаться на `\`: стандартный CSV-парсер ломается (`ywm.py` разбирает сам).
- Базовый доступ: 100 запросов в сутки, сброс 00:00 МСК, готовится 20 мин – 2 ч (до 24 ч).
  Расширенный: +500 000 в месяц, 30 000 ₽/мес или 250 000 ₽/год, блок +500 000 — 10 000 ₽.
- Ошибки: `400 WRONG_REGIONS`, `EMPTY_PATHS`, `EMPTY_DATES`, `SOME_DATES_ARE_UNAVAILABLE` (+`unavailable_dates`),
  `URLS_ARE_CORRUPTED`, `403 LIMITS_EXCEEDED`, `413 PAYLOAD_TOO_LARGE`. Тело ошибок PRO — `{"code","message"}`.

## Индексирование

- `indexing/history?date_from&date_to` → `{"indicators":{"HTTP_2XX":[...],"HTTP_3XX","HTTP_4XX","HTTP_5XX","OTHER"}}`;
  точки с часами, не по одной в день. По умолчанию — текущий день.
- `indexing/samples?offset&limit` (limit ≤ 100, до 50 000) → `{"count","samples":[{"status","http_code","url","access_date"}]}`.
- `search-urls/in-search/history` → `{"history":[{"date","value"}]}`; точки на обновлениях базы.
- `search-urls/in-search/samples` → `{"count","samples":[{"url","last_access","title"}]}`.
- `search-urls/events/history` → `{"indicators":{"APPEARED_IN_SEARCH":[...],"REMOVED_FROM_SEARCH":[...]}}`.
- `search-urls/events/samples` → `samples[]`: `url`, `title`, `event_date`, `last_access` (последний обход
  до события), `event`, `excluded_url_status`, `bad_http_status`, `target_url` (куда редирект, canonical
  или дубль).
- `important-urls` → `urls[]`: `url`, `update_date`, `change_indicators` (`INDEXING_HTTP_CODE`, `SEARCH_STATUS`,
  `TITLE`, `DESCRIPTION`), `indexing_status{status,http_code,access_date}`, `search_status{title,description,
  last_access,excluded_url_status,bad_http_status,searchable,target_url}`. `important-urls/history?url=` —
  то же в `history[]`. Набор страниц задаётся только в интерфейсе.
- `POST indexing/archive/` → `{"task_id"}`; `GET indexing/archive/{task-id}` → `{"state":"IN_PROGRESS|DONE|FAILED","download_url"}`
  (10 с – 3 мин). Файл — CSV.gz в UTF-8: `url,httpCode,status,lastAccess,redirTarget,relCanonical,title,metaDescription`.
- `summary` → `{"sqi","excluded_pages_count","searchable_pages_count","site_problems":{"FATAL":1,...}}`.
- `sqi-history?date_from&date_to` → `{"points":[{"date","value"}]}`, по дням с 2018.
- `diagnostics` → `{"problems":{"NO_SITEMAPS":{"severity","state","last_state_update"}}}` — все проверки,
  `state`: `PRESENT`, `ABSENT`, `UNDEFINED`.

## Ссылки

Нужен доступ `webmaster:hostinfo`, иначе 403. Все `samples` — выборки до 50 000, limit ≤ 100.

- `links/external/samples` → `{"count","links":[{"source_url","destination_url","discovery_date","source_last_access_date"}]}`.
- `links/external/history?indicator=LINKS_TOTAL_COUNT` (обязателен, дат нет) → `indicators.LINKS_TOTAL_COUNT[]`.
- `links/internal/broken/samples?indicator=` (повторяемый: `SITE_ERROR`, `DISALLOWED_BY_USER`, `UNSUPPORTED_BY_ROBOT`) —
  формат как у внешних.
- `links/internal/broken/history?date_from&date_to` → `indicators` по тем же типам.

## Переобход и Sitemap

- `POST recrawl/queue` `{"url":"https://example.com/page"}` → `{"task_id","quota_remainder"}`;
  `409 RECRAWL_IN_PROCESS` — уже в очереди. `GET recrawl/queue?offset&limit&date_from&date_to` → `tasks[]`
  (`task_id`, `url`, `added_time`, `state`: `IN_PROGRESS|DONE|FAILED`). `GET recrawl/quota` →
  `{"daily_quota","quota_remainder"}`; квота у каждого сайта своя.
- `GET sitemaps?parent_id&limit&from` — курсорная пагинация (`from` = последний `sitemap_id`, limit ≤ 100) →
  `sitemaps[]`: `sitemap_id`, `sitemap_url`, `last_access_date`, `errors_count`, `urls_count`, `children_count`,
  `sources` (`ROBOTS_TXT`, `WEBMASTER`, `INDEX_SITEMAP`), `sitemap_type` (`SITEMAP`, `INDEX_SITEMAP`).
- `user-added-sitemaps`: GET список (`offset`, `limit`), POST `{"url"}` → `{"sitemap_id"}`, DELETE `/{id}`.
  Ошибки `409 SITEMAP_ALREADY_ADDED`, `404 SITEMAP_NOT_FOUND`, `404 SITEMAP_NOT_ADDED`.
- `v4.1`: `GET .../sitemaps/recrawl` — лимит; `POST .../sitemaps/{id}/recrawl?parent_id=` →
  `{"sitemap_recrawl_info":{"last_request_time","pending","allowed"},"host_sitemaps_recrawl_limit_info":{"monthly_limit_requests":10,"requests_count","nearest_allowed_day"}}`;
  `429 HOST_RECRAWLS_QUOTA_EXCEEDED`.

## Сайты и права

- `GET v4/user/{u}/hosts` → `hosts[]`: `host_id`, `ascii_host_url`, `unicode_host_url`, `verified`, `main_mirror{...}`
  (null у главного зеркала).
- `GET` хоста → плюс `host_data_status` (`NOT_INDEXED`, `NOT_LOADED`, `OK`), `host_display_name`.
- `POST v4/user/{u}/hosts` `{"host_url":"https://example.com"}` → `{"host_id"}`; `409 HOST_ALREADY_ADDED`,
  `403 HOSTS_LIMIT_EXCEEDED`. `DELETE` хоста — удалить.
- `GET verification` → `verification_uin`, `verification_state` (`NONE`, `VERIFIED`, `IN_PROGRESS`,
  `VERIFICATION_FAILED`, `INTERNAL_ERROR`), `verification_type`, `applicable_verifiers`.
  `POST verification?verification_type=DNS|HTML_FILE|META_TAG` — запустить проверку.
- `GET owners` → `users[]`: `user_login`, `verification_uin`, `verification_type`, `verification_date`.

## Фиды

Товарные и сервисные YML-фиды (только HTTPS-сайты): `feeds/list`, `feeds/status/list`, `feeds/info`, `feeds/summary`,
`feeds/problems`, `feeds/host-problems`, `feeds/moderation/rejected-offers`, `POST feeds/moderation/recheck`,
`POST feeds/add/start` + `GET feeds/add/info`, `POST feeds/batch/add` (до 50), `feeds/batch/remove`,
`feeds/regions`, `feeds/region/change`. Параметры — на страницах `reference/feeds-*` официальной документации.

## Enum'ы

- Устройства: `ALL`, `DESKTOP`, `MOBILE_AND_TABLET`, `MOBILE`, `TABLET`.
- `SiteProblemSeverityEnum`: `FATAL` (сайт или страницы могут выпасть), `CRITICAL`, `POSSIBLE_PROBLEM`, `RECOMMENDATION`.
- Проблемы диагностики: FATAL — `CONNECT_FAILED`, `DISALLOWED_IN_ROBOTS`, `DNS_ERROR`, `MAIN_PAGE_ERROR`, `THREATS`;
  CRITICAL — `INSIGNIFICANT_CGI_PARAMETER`, `SLOW_AVG_RESPONSE_TIME`, `SSL_CERTIFICATE_ERROR`, `URL_ALERT_4XX`,
  `URL_ALERT_5XX`; POSSIBLE_PROBLEM — `DISALLOWED_URLS_ALERT`, `DOCUMENTS_MISSING_DESCRIPTION`,
  `DOCUMENTS_MISSING_TITLE`, `DUPLICATE_CONTENT_ATTRS`, `DUPLICATE_PAGES`, `ERROR_IN_ROBOTS_TXT`,
  `ERRORS_IN_SITEMAPS`, `FAVICON_ERROR`, `MAIN_MIRROR_IS_NOT_HTTPS`, `MAIN_PAGE_REDIRECTS`,
  `NO_METRIKA_COUNTER_BINDING`, `NO_METRIKA_COUNTER_CRAWL_ENABLED`, `NO_ROBOTS_TXT`, `NO_SITEMAPS`,
  `NO_SITEMAP_MODIFICATIONS`, `NON_WORKING_VIDEO`, `SOFT_404`, `TOO_MANY_DOMAINS_ON_SEARCH`, `VIDEOHOST_OFFER_*`;
  RECOMMENDATION — `BIG_FAVICON_ABSENT`, `FAVICON_PROBLEM`, `NO_METRIKA_COUNTER`, `NO_REGIONS`, `NOT_IN_SPRAV`,
  `NOT_MOBILE_FRIENDLY`, `VYGODA_POSSIBLE_ACTIVATION`.
- Регионы query-analytics: 225 — Россия, 1 — Москва и Московская область, 10174 — Санкт-Петербург и
  Ленинградская область, остальные субъекты — по справочнику `pro/regions`. Уровни (страна, округ,
  субъект) не пересекаются внутри уровня.

## Причины исключения

`excluded_url_status` в events/samples и important-urls (в архиве `indexing/archive` названия иногда
другие — указаны в скобках):

| Код | Причина |
|---|---|
| `NOTHING_FOUND` | страница неизвестна роботу или долго была недоступна |
| `HOST_ERROR` | не удалось соединиться с сервером |
| `REDIRECT_NOTSEARCHABLE` | редирект, в поиске цель (`target_url`) |
| `HTTP_ERROR` | HTTP-ошибка, код в `bad_http_status` |
| `NOT_CANONICAL` | в поиске канонический адрес из `rel="canonical"` |
| `NOT_MAIN_MIRROR` | неглавное зеркало |
| `PARSER_ERROR` | не удалось получить содержимое |
| `ROBOTS_HOST_ERROR` / `ROBOTS_URL_ERROR` | запрет в robots.txt для сайта / страницы |
| `DUPLICATE` | дубль страницы, которая уже в поиске |
| `LOW_QUALITY` (`BAD_QUALITY`) | исключена алгоритмом, вернётся сама, если станет релевантной |
| `CLEAN_PARAMS` | директива Clean-param |
| `NO_INDEX` (`META_NO_INDEX`) | meta robots noindex |
| `OTHER` | нет актуальных данных |

## Ошибки и лимиты

Тело ошибки: `{"error_code","error_message"}` (PRO — `{"code","message"}`).

| HTTP | Коды |
|---|---|
| 400 | `ENTITY_VALIDATION_ERROR`, `FIELD_VALIDATION_ERROR`, `INVALID_URL`, `NO_CHANGES`, PRO-коды выше |
| 401 | токена нет или он невалиден (тело пустое) |
| 403 | `ACCESS_FORBIDDEN`, `INVALID_OAUTH_TOKEN`, `INVALID_USER_ID`, `HOSTS_LIMIT_EXCEEDED`, `LIMITS_EXCEEDED`, `FEEDS_*` |
| 404 | `RESOURCE_NOT_FOUND`, `HOST_NOT_FOUND`, `HOST_NOT_VERIFIED`, `HOST_NOT_INDEXED`, `HOST_NOT_LOADED`, `QUERY_ID_NOT_FOUND`, `SITEMAP_NOT_FOUND`, `TASK_NOT_FOUND`, `FEED_NOT_FOUND` |
| 409 | `HOST_ALREADY_ADDED`, `VERIFICATION_ALREADY_IN_PROGRESS`, `SITEMAP_ALREADY_ADDED`, `RECRAWL_IN_PROCESS` |
| 413 | `REQUEST_ENTITY_TOO_LARGE`, `PAYLOAD_TOO_LARGE` |
| 415 | `CONTENT_TYPE_UNSUPPORTED` |
| 429 | `QUOTA_EXCEEDED` (+`daily_quota`, `exceeded_until`), `TOO_MANY_REQUESTS_ERROR`, `HOST_RECRAWLS_QUOTA_EXCEEDED` |

| Что | Лимит |
|---|---|
| query-analytics | 10 000 вызовов в час на домен, дальше временная блокировка (429) |
| PRO-выгрузка | 100 «URL × день» в сутки на домен, сброс 00:00 МСК; глубина 550 дней; ссылка 24 ч |
| Переобход страниц | суточная квота сайта (`recrawl/quota`) |
| Приоритетный переобход Sitemap | 10 в месяц на сайт |
| popular | топ 3000, 500 за вызов |
| `*/samples` | до 50 000 строк, 100 за вызов |
| Фиды | до 50 в batch или 50 параллельных загрузок |
