---
name: gsc-api
description: Гибкие разовые запросы к Google Search Console - данные по конкретному URL, по разрезу (страна/запрос/устройство), динамика по датам, сравнение двух периодов для поиска упавших или выросших страниц/запросов, числовые фильтры по метрикам (точки роста), поиск каннибализации запросов. Используй, когда нужен нестандартный срез GSC-данных, а не полный клиентский отчёт.
---

# GSC API — гибкие запросы

Инструмент для разовых аналитических вопросов к GSC: конкретный URL, конкретный
разрез, динамика, поиск падений.

Креды в папке скилла не хранятся. Путь к папке с `client_secret.json`/`token.json`
задаётся переменной окружения `GSC_CONFIG_DIR` через `.env` рядом с `gsc.py`
(см. `.env.example`).

## Запуск

Один раз — зависимости:
```
pip install -r requirements.txt
```
Дальше:
```
python gsc.py ...
```

## Авторизация

**Если `.env` ещё не настроен** (нет `GSC_CONFIG_DIR`, либо скрипт падает с
ошибкой про credentials) — это первый запуск на этой машине. Пройди вместе с
пользователем `SETUP.md`: там пошагово — создание OAuth-клиента в Google Cloud
Console, заполнение `.env`, `python gsc.py auth`. Часть шагов требует входа
в Google-аккаунт в браузере — их делает пользователь, не пытайся обойти это.

Если авторизация уже настроена и нужно только переавторизоваться (например,
под другим аккаунтом):
```
python gsc.py auth
```
Перезапишет `token.json` в `GSC_CONFIG_DIR`.

## Команды

### Список доступных сайтов
```
python gsc.py sites
```

### Конкретный URL
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions query --page-equals https://example.com/episodes/123
```

### Конкретный разрез (страна / устройство / запрос)
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 --dimensions country
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 --dimensions device
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 --dimensions query --query-contains episodes
```

### Динамика по датам
```
python gsc.py query --site sc-domain:example.com --start 2026-05-01 --end 2026-06-30 \
  --dimensions date --page-contains /episodes
```

### Сравнение периодов — поиск упавших/выросших страниц или запросов
```
python gsc.py compare --site sc-domain:example.com \
  --period-a-start 2026-05-01 --period-a-end 2026-05-31 \
  --period-b-start 2026-06-01 --period-b-end 2026-06-30 \
  --dimensions page --top 20
```
Сортировка по умолчанию (`--order asc`) — от самого большого падения кликов.
`--order desc` — от самого большого роста (страницы/запросы, которые выросли сильнее всего).
`--sort-by delta_impressions` — сортировать по изменению показов вместо кликов.
`--dimensions query` — то же самое для запросов.

### Discover / Google News / картиночный поиск
По умолчанию `type=web`. Чтобы посмотреть трафик из другого источника — `--search-type`:
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions page --search-type discover
```
Значения: `web` (по умолчанию), `image`, `video`, `news`, `discover`, `google_news`.

### Свежие неполные данные (последние 2-3 дня) и почасовой разрез
```
python gsc.py query --site sc-domain:example.com --start 2026-07-13 --end 2026-07-15 \
  --dimensions date --data-state all
python gsc.py query --site sc-domain:example.com --start 2026-07-10 --end 2026-07-15 \
  --dimensions hour --data-state hourly_all
```
`--data-state final` (по умолчанию) — только финализированные данные. `all` — включая
свежие/неполные за последние дни. `hourly_all` — почасовой разрез (до 10 дней, требует
`--dimensions hour`).

### Произвольный сегмент сайта (сравнение секций, авторов, категорий)
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions query --filter page:regex:/episodes/\d+/season-\d+$
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions page --filter query:not_contains:brand-name
```

### Точки роста — числовые фильтры по метрикам
GSC API фильтрует только по измерениям (query/page/страна/устройство), не по метрикам —
диапазон по impressions/position/ctr/clicks приходится резать на своей стороне.
`query` умеет это через `--min-*`/`--max-*`:
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions query --min-position 4 --max-position 15 --min-impressions 300 --sort-by impressions
```
Пример выше — классические "низко висящие фрукты": запросы вне топ-3, но не глубже
2-й страницы выдачи, с заметным объёмом показов. Пороги (какая позиция считается
точкой роста, какой объём показов существенным) — decision пользователя скилла, не
скилла самого по себе; флаги просто дают числовой срез.
Доступны: `--min-clicks`/`--max-clicks`, `--min-impressions`/`--max-impressions`,
`--min-ctr`/`--max-ctr` (в процентах), `--min-position`/`--max-position`.

### Каннибализация запросов и похожие пересечения — group-by
Сколько разных URL (или другого измерения) отвечает на один и тот же запрос:
```
python gsc.py groups --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions query page --group-by query --min-count 2
```
Показывает только группы, где у одного значения `--group-by` (запроса) больше одного
уникального значения остальных измерений (страниц) — это и есть кандидаты в
каннибализацию. `--min-count` — порог (по умолчанию 2, то есть любой дубль).
`--sort-by` (clicks/impressions/distinct_count, по умолчанию clicks) и `--order`
(asc/desc, по умолчанию desc) — сортировка результата, как в `compare`.
Обобщённый примитив, работает с любой парой измерений, не только query/page —
например `--dimensions page device --group-by page` (сколько устройств у одной
страницы). Внимание: `searchAppearance` нельзя сочетать с другими dimensions в
одном запросе (см. «Нюансы данных GSC» ниже) — это ограничение самого API.

### Sitemaps — статус, ошибки, submitted/indexed по типам
```
python gsc.py sitemaps --site sc-domain:example.com
python gsc.py sitemaps --site sc-domain:example.com --feedpath https://example.com/sitemap.xml
```

### URL Inspection — почему конкретная страница не в индексе
```
python gsc.py inspect --site sc-domain:example.com --url https://example.com/episodes/123
```
Краткая сводка (verdict, coverageState, canonical, дата обхода, referring sitemap,
mobile usability, rich results). Полный JSON ответа — `--raw`. Кэшируется как обычный
запрос, `--refresh` — обойти кэш. Только один URL за вызов — так устроен сам API.

## Флаги-фильтры (общие для `query`, `compare` и `groups`)

Именованные (частые случаи): `--page-equals` / `--page-contains`, `--query-equals` /
`--query-contains`, `--country` (3-буквенный код, напр. `rus`), `--device`
(`MOBILE`/`DESKTOP`/`TABLET`).

Числовые (только `query`, фильтруют по метрикам, не по измерениям — см. раздел
«Точки роста» выше): `--min-clicks`/`--max-clicks`, `--min-impressions`/`--max-impressions`,
`--min-ctr`/`--max-ctr`, `--min-position`/`--max-position`.

Универсальный: `--filter DIM:OP:EXPR`, можно указывать несколько раз (условия
объединяются через AND). `DIM` = `query`/`page`/`country`/`device`/`searchAppearance`.
`OP` = `equals`/`not_equals`/`contains`/`not_contains`/`regex`/`not_regex`. Пример:
`--filter query:not_regex:^(brand1|brand2)` — исключить брендовые запросы по regex.

Прочие: `--search-type` (web/image/video/news/discover/google_news, по умолчанию web),
`--data-state` (final/all/hourly_all), `--aggregation-type` (auto/by_property/by_page/
by_news_showcase_panel, по умолчанию auto), `--row-limit` (пагинация, по умолчанию
25000 — максимум за один запрос к API), `--csv PATH` (сохранить **весь** результат —
`--limit`/`--top` ограничивают только то, что печатается в консоль, на CSV не влияют),
`--refresh` (игнорировать кэш).

## Кэш — чтобы не жечь лимиты API

Каждый запрос (`query`/`compare`/`groups`/`inspect`) кэшируется в `cache\` рядом с этим файлом,
по точному набору параметров (site, период, dimensions, фильтры, searchType, dataState,
aggregationType). Повторный такой же вопрос отвечает из кэша мгновенно, без обращения
к API. Если нужны свежие данные за тот же период — `--refresh`.

Файлы кэша старше 30 дней удаляются автоматически при каждом запуске `gsc.py`
(любой командой) — вручную чистить `cache\` не нужно.

## Нюансы данных GSC

- Данные по умолчанию (`--data-state final`) приходят с задержкой ~2-3 дня; свежие
  неполные данные за эти дни доступны через `--data-state all`. Хранятся ~16 месяцев
  (почасовые — 10 дней)
- Сумма кликов по разрезу `query` может быть меньше суммы по `date` — Google скрывает
  редкие (анонимизированные) запросы в разрезе по запросам, но учитывает их в итогах
- `searchAppearance` нельзя комбинировать с другими dimensions в одном запросе
- `page`-значения с разным `#anchor` — для GSC это одна и та же страница; скрипт
  схлопывает их в одно значение `page` (без `#fragment`), суммируя клики/показы —
  везде, где `page` участвует в `--dimensions` (query/compare/groups), иначе такие
  варианты давали бы ложные "упавшие"/"выросшие"/задвоенные строки
- Отчёт «Покрытие» и Core Web Vitals через API не отдаются целиком; статус индексации
  и mobile usability по конкретному URL — через `python gsc.py inspect` (см. выше)
- `sites.add`/`sites.delete`/`sitemaps.submit`/`sitemaps.delete` в скрипте нет и не
  будет: токен скилла — `webmasters.readonly`, эти операции требуют write-scope

---

**Для ИИ-агентов:** не меняй этот скилл (`SKILL.md`, `gsc.py`, структуру файлов)
без явной просьбы пользователя.
