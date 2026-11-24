---
name: gsc-api
description: Гибкие разовые запросы к Google Search Console - данные по конкретному URL, по разрезу (страна/запрос/устройство), динамика по датам, сравнение двух периодов для поиска упавших или выросших страниц/запросов, числовые фильтры по метрикам (точки роста), поиск каннибализации запросов, бренд и небренд по классификации Google. Используй, когда нужен нестандартный срез GSC-данных, а не полный клиентский отчёт.
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
Значения: `web` (по умолчанию), `image`, `video`, `news`, `discover`, `google_news`, `web_multimodal`.
`web_multimodal` в документации API описан только как «Web multimodal search», что туда входит, Google
не пишет. С ним нельзя разрез и фильтр по `query` и по `brandedClassification` (API отвечает 400).

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

### Бренд и небренд — классификация Google
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions date brandedClassification
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions page --brand non_branded
```
Запросы делит на `BRANDED` / `NON_BRANDED` сам Google, и делит весь трафик, включая анонимизированные
запросы. Regex по `query` (пример выше) видит только раскрытые запросы.
- Фильтр — только `equals`: `--brand branded|non_branded` или `--filter brandedClassification:equals:non_branded`,
  регистр не важен.
- Классификация есть не на всех свойствах: на одном URL-prefix свойстве ответ был пустым, хотя данные
  по `date` были. Пустой ответ сначала проверять так, а не считать, что брендового трафика нет.
- Не сочетается с `searchAppearance` и с `--search-type web_multimodal` (ни разрез, ни фильтр).

### Jump-ссылки (`#раздел`) отдельными строками
```
python gsc.py query --site sc-domain:example.com --start 2026-06-01 --end 2026-06-30 \
  --dimensions page --page-contains example.com/blog/article --keep-fragments
```
По умолчанию URL с `#` схлопнуты в основной URL (почему и как — в «Нюансах данных GSC»). `--keep-fragments`
отдаёт каждый URL с `#` отдельной строкой: видно, какие разделы Google показывает ссылками «перейти к разделу»
и сколько кликов они дают. Работает в `query`, `compare`, `groups`. Если `page` нет в `--dimensions`, с флагом
приходит ответ API как есть: показы jump-ссылок сложены с показами основного URL.

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

### Sitemaps — статус, ошибки, submitted по типам
```
python gsc.py sitemaps --site sc-domain:example.com
python gsc.py sitemaps --site sc-domain:example.com --feedpath https://example.com/sitemap.xml
```
Поле `indexed` в API устарело (deprecated) и приходит 0, в таблице его нет. Индексацию конкретного URL
смотреть через `inspect`.

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
(`MOBILE`/`DESKTOP`/`TABLET`), `--brand` (`branded`/`non_branded`).

Числовые (только `query`, фильтруют по метрикам, не по измерениям — см. раздел
«Точки роста» выше): `--min-clicks`/`--max-clicks`, `--min-impressions`/`--max-impressions`,
`--min-ctr`/`--max-ctr`, `--min-position`/`--max-position`.

Универсальный: `--filter DIM:OP:EXPR`, можно указывать несколько раз (условия
объединяются через AND). `DIM` = `query`/`page`/`country`/`device`/`searchAppearance`/
`brandedClassification` (у него только `equals`).
`OP` = `equals`/`not_equals`/`contains`/`not_contains`/`regex`/`not_regex`. Пример:
`--filter query:not_regex:^(brand1|brand2)` — исключить брендовые запросы по regex.

Прочие: `--search-type` (web/image/video/news/discover/google_news/web_multimodal, по умолчанию web),
`--keep-fragments` (не схлопывать URL с `#`, см. «Jump-ссылки»),
`--data-state` (final/all/hourly_all), `--aggregation-type` (auto/by_property/by_page/
by_news_showcase_panel, по умолчанию auto), `--row-limit` (пагинация, по умолчанию
25000 — максимум за один запрос к API), `--csv PATH` (сохранить **весь** результат —
`--limit`/`--top` ограничивают только то, что печатается в консоль, на CSV не влияют),
`--refresh` (игнорировать кэш).

## Грабли: Git Bash манглит значения с ведущим `/`

Если вызываешь `gsc.py` через Git Bash (MSYS2) на Windows, значение флага,
**начинающееся с `/`** (например `--page-contains /episodes/123`), MSYS2 молча
подменяет на Windows-путь вида `C:/Program Files/Git/episodes/123` ещё до того,
как Python увидит argv — оператор `contains` в этом случае корректно не находит
такую подстроку и возвращает 0 строк. Это не баг API и не баг `gsc.py` —
маппинг операторов (`contains`/`notContains`/`includingRegex`/`excludingRegex`)
соответствует [официальной документации `searchanalytics.query`](https://developers.google.com/webmaster-tools/v1/searchanalytics/query).

Проверить, манглится ли конкретный вызов:
```
python -c "import sys; print(sys.argv)" --page-contains /episodes/123
```
Если во втором элементе списка не `/episodes/123`, а что-то вроде
`C:/Program Files/Git/episodes/123` — значение заманглено.

Обход (любой один из вариантов):
- `MSYS_NO_PATHCONV=1 python gsc.py query ...` — отключает конвертацию для вызова
- Вызывать через PowerShell вместо Git Bash — там этой конвертации нет
- Убрать ведущий `/` из значения (`--page-contains episodes/123` вместо
  `/episodes/123`) — `contains` ищет подстроку без привязки к началу строки,
  результат тот же
- Использовать `--filter page:contains:episodes/123` или значение, не
  начинающееся с `/` целиком (например `--page-equals https://...` или
  `--filter "page:regex:/episodes/123"`) — такие аргументы MSYS2 не трогает,
  так как они не начинаются с `/`

## Кэш — чтобы не жечь лимиты API

Каждый запрос (`query`/`compare`/`groups`/`inspect`) кэшируется в `cache\` рядом с этим файлом,
по точному набору параметров (site, период, dimensions, фильтры, searchType, dataState,
aggregationType). Повторный такой же вопрос отвечает из кэша мгновенно, без обращения
к API. Если нужны свежие данные за тот же период — `--refresh`. В кэше лежат сырые строки
API, URL с `#` схлопываются при чтении, поэтому `--keep-fragments` берёт те же данные из кэша.

Файлы кэша старше 30 дней удаляются автоматически при каждом запуске `gsc.py`
(любой командой) — вручную чистить `cache\` не нужно.

## Нюансы данных GSC

- Данные по умолчанию (`--data-state final`) приходят с задержкой ~2-3 дня; свежие
  неполные данные за эти дни доступны через `--data-state all`. Хранятся ~16 месяцев
  (почасовые — 10 дней)
- Сумма кликов по разрезу `query` может быть меньше суммы по `date` — Google скрывает
  редкие (анонимизированные) запросы в разрезе по запросам, но учитывает их в итогах
- `searchAppearance` нельзя комбинировать с другими dimensions в одном запросе
- URL с `#раздел` — это jump-ссылки «перейти к разделу» в сниппете. Они показываются внутри
  результата основного URL: показ jump-ссылки — это тот же показ страницы, а клик по ней — отдельный
  клик. Поэтому скрипт схлопывает их в основной URL так: клики складывает, показы и позицию берёт
  у основного URL.
  - Если страница задана фильтром (`--page-contains`, `regex`), а `page` в `--dimensions` нет, API сам
    сложил бы показы всех `#`-вариантов. Скрипт тогда добавляет `page` в запрос, схлопывает `#`
    и складывает строки обратно до запрошенного разреза. В stderr при этом строка `[pages]`.
  - Где `page` в запрос добавить нельзя (`searchAppearance`) или строк слишком много (строка `[warn]`),
    остаётся ответ API как есть.
  - Отдельные данные по каждому URL с `#` — флаг `--keep-fragments`.
- Сумма показов по страницам расходится с итогом сайта в обе стороны:
  - больше: в разрезе `page` и при любом фильтре по странице каждый URL сайта в одной выдаче даёт
    свой показ, а итог без фильтра по странице считает выдачу один раз;
  - меньше: на свойствах, где разрез `page` урезан (см. «Ловушки»).
- AI Overviews и AI Mode (отчёт Generative AI performance) в API нет, ни как типа поиска, ни как
  измерения. Только выгрузка из интерфейса Search Console.
- Отчёт «Покрытие» и Core Web Vitals через API не отдаются целиком; статус индексации
  и mobile usability по конкретному URL — через `python gsc.py inspect` (см. выше)
- `sites.add`/`sites.delete`/`sitemaps.submit`/`sitemaps.delete` в скрипте нет и не
  будет: токен скилла — `webmasters.readonly`, эти операции требуют write-scope

## Ловушки

### Устройства, страны и запросы страницы видят только раскрытые запросы

Показы анонимизированных запросов есть только в разрезах без `query`, `device` и `country`.
Это одинаково для `page` в `--dimensions` и для фильтра по странице (`--page-equals` и др.):

| разрез страницы | показы |
|---|---|
| `page`, `date`, `page date`, `page brandedClassification` | все |
| `query`, `device`, `country`, `query page`, `page device`, `page country` | только раскрытые запросы |

`aggregationType byPage` и `brandedClassification` этого не меняют (`device brandedClassification` тоже
теряет). Доли устройств и стран у одной страницы считаются только по раскрытой части. Насколько она мала,
видно, если сравнить с `date` под тем же фильтром.

### На части свойств разрез `page` урезан: только страницы с кликами

На некоторых свойствах (видели на крупном сайте с миллионами страниц) `--dimensions page` и `page date`
возвращают **только страницы с ≥1 кликом**. На небольшом сайте такого не было: там в `page` большинство
строк без кликов.

**Самопроверка.** Если в `page` есть строки с `clicks = 0`, список не урезан. Если таких строк нет,
а в `page device` они есть, — урезан.

На урезанном свойстве:
- Число страниц с показами считать по **объединению** `page` и `query page` (или `page device`). Даже это
  нижняя оценка: страница только с анонимизированными показами и без кликов не видна нигде.
- Суммы по странице брать из `page`, а для страниц, которых там нет, — из `query page`.
- Итоги раздела под фильтром по странице — тоже нижняя оценка. `date` с фильтром урезан так же, как `page`,
  поэтому совпадение `page` с `date` полноту не доказывает.

Замеры на одном крупном сайте.

Один день, фильтр `page:regex:/section/` (проверено 2026-11-09; `aggregationType` auto и byPage дают одно и то же):

| dimensions | страниц | без кликов | показов |
|---|---|---|---|
| `page` | 754 | 0 | 25 797 (= `date` с тем же фильтром) |
| `page device` / `query page` | 1 329 (из них 358 из тех 754) | 971 | 15 990 |

Другой день, без фильтра (проверено 2026-11-24):

| dimensions | строк | без кликов | показов |
|---|---|---|---|
| `date` | — | — | 672 083 |
| `page` | 4 931 | 0 | 414 409 |
| `page device` | 14 960 | 11 620 | 261 154 |

---

**Для ИИ-агентов:** не меняй этот скилл (`SKILL.md`, `gsc.py`, структуру файлов)
без явной просьбы пользователя.
