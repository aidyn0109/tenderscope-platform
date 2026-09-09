# CLAUDE.md — TenderScope (платформа анализа госзакупок)

## Обзор проекта

Веб-платформа для автоматизированного сбора и анализа данных госзакупок с портала [goszakup.gov.kz](https://goszakup.gov.kz/). Два независимых режима: анализ реестра договоров по БИН поставщиков (с расчётом показателя загрузки) и поиск объявлений с победителем, но без договора.

**Стек:** Python 3.11 + Streamlit + requests + BeautifulSoup + openpyxl
**Язык интерфейса и кода/комментариев:** русский
**Деплой:** Render, переменные окружения через Render Environment

> Пользовательская документация — в [README.md](README.md). Этот файл — рабочие заметки для работы с кодом.

---

## Архитектура

```
TenderScope/
├── app.py                        # Streamlit-интерфейс (авторизация, навигация, формы, прогресс, выгрузка)
├── worker.py                     # Подпроцесс парсинга (запускается через subprocess)
├── auth.py                       # Авторизация пользователей (HMAC-токены, хэши паролей)
│
├── scraper.py                    # Режим 1: договоры (GraphQL v2 + v3 ContractSpecSum)
├── excel_export.py               # Режим 1: Excel-отчёт (листы «Сводка» + «Договоры»)
│
├── scraper_announcements.py      # Режим 2: объявления (GraphQL v3 + HTML протоколов и лотов)
├── excel_export_announcements.py # Режим 2: Excel-отчёт (лист «Объявления»)
│
├── requirements.txt
├── render.yaml
├── README.md
└── CLAUDE.md
```

---

## Технический стек

| Компонент | Библиотека / Инструмент |
| --- | --- |
| UI | `streamlit` |
| HTTP-запросы | `requests` + `urllib3` (`verify=False` — казахстанский SSL) |
| Парсинг HTML протоколов и страниц лотов | `beautifulsoup4` |
| GraphQL API | `requests.post` к эндпоинтам госзакупок |
| Excel | `openpyxl` |
| Прогресс | `st.progress` + file-based polling (JSON-файлы) |
| Авторизация платформы | HMAC-SHA256 токены, хэши паролей SHA-256 |

> **Playwright не используется.** Убран в коммите `7d8053a` (переход на GraphQL API). Все данные — через официальный GraphQL API с Bearer-токеном; HTML протоколов итогов и страниц лотов скачивается напрямую через `requests`. `pandas` в requirements.txt остался с прежних версий и в коде не импортируется.

---

## Конфигурация API

| Параметр | Значение |
| --- | --- |
| Договоры (список, subjects) | `https://ows.goszakup.gov.kz/v2/graphql` |
| Предметы договоров, объявления, лоты | `https://ows.goszakup.gov.kz/v3/graphql` |
| Авторизация | Bearer токен в заголовке `Authorization` |
| Переменная окружения | `GOSZAKUP_TOKEN` |
| Fallback | `st.secrets["goszakup_token"]` |
| SSL | `verify=False` + `urllib3.disable_warnings()` |
| Лимит на запрос | 50 записей (`Lots` — 100) |
| Пагинация | через `extensions.pageInfo.hasNextPage` и `lastId` |
| Потолок выборки | `MAX_RECORDS = 10_000` |
| Retry | 2 попытки с паузой 3 сек |
| Таймаут | 60 сек на GraphQL, 30 сек на HTML-страницы |

Порядок разрешения токена в обоих скраперах: `os.environ["GOSZAKUP_TOKEN"]` → `st.secrets["goszakup_token"]` → `RuntimeError`.

---

## Зависимости (requirements.txt)

```
streamlit>=1.35.0
requests>=2.31.0
urllib3>=2.0.0
openpyxl>=3.1.2
pandas>=2.0.0
beautifulsoup4>=4.12.0
```

---

## Авторизация платформы (auth.py)

* Пользователи — из `st.secrets["users"]`, иначе из переменных окружения (`ADMIN_HASH` / `USER_HASH`)
* Пароли хранятся как SHA-256 хэши, сравнение — прямое сравнение хэшей
* Сессии — самодостаточные токены `<base64url(payload)>.<HMAC-SHA256[:32]>` в URL-параметре `auth`, TTL 7 дней, серверного состояния нет
* Проверка подписи — `hmac.compare_digest`
* Секрет подписи: `st.secrets["auth_secret"]` → `AUTH_SECRET` → **встроенный дефолт**

> ⚠️ Если `AUTH_SECRET` не задан, используется `_DEFAULT_SECRET` из исходников — подпись сессии можно подделать. На продакшене переменная обязательна.

---

## Режим 1: Анализ реестра договоров (scraper.py)

### Форма ввода

* Пары полей **БИН + Максимальный доход (тг)**, кнопка **«＋ Добавить БИН»** добавляет строку, **«✕»** удаляет
* Валидация: БИН — ровно 12 цифр; максимальный доход — число > 0 (иначе кнопка запуска заблокирована)
* Кнопка **«🔍 Запустить анализ»**

### Шаг 1 — Список договоров (v2)

```graphql
query($f: ContractFiltersInput, $after: Int) {
  contract(limit: 50, after: $after, filters: $f) {
    id
    contract_number_sys
    crdate
    description_ru
    supplier_biin
    ref_subject_type_id
    ref_contract_status_id
    fin_year
    contract_units { id item_price fact_sum }
  }
}
```

**Фильтры API:**

* `supplier_biin` — БИН поставщика
* `ref_contract_status_id` — `[190, 460, 450]` (Действует / Передан.Действует / Создано доп.соглашение)

**Фильтрация в Python** (не поддерживается фильтрами API):

* `ref_subject_type_id == 2` (Работа)
* `crdate` начинается с `TARGET_YEAR` (`2026`)

### Шаг 2 — Имя поставщика (v2 subjects)

`subjects(filters: {bin: …}) { bin name_ru }` — результат кэшируется в `_supplier_name_cache` (очищается в начале `scrape_all`).

### Шаг 3 — Суммы из предметов договора (v3), функция `_calc_amounts`

```graphql
query($f: ObContractFiltersInput) {
  ObContract(limit: 1, filter: $f) {
    id
    ContractSpecSum { id unitId finYear planSum factSum }
  }
}
```

Алгоритм:

1. Собрать порядок уникальных `unitId` из `ContractSpecSum`
2. **Основной предмет договора = последний `unitId`** (куда ведёт ссылка `loadunit` на сайте)
3. Сгруппировать записи этого `unitId` по `finYear`:
   * `planSum` = сумма `planSum` за **максимальный** `finYear`
   * `factSum` = сумма `factSum` за **все годы кроме максимального**
4. Fallback: если `planSum ≈ factSum` (разница < 1.0) и `units[0].item_price > planSum × 10`, взять `item_price` первого unit из v2 (сумма по предмету договора с НДС)

Соответствующий unit в v2 берётся по индексу `len(units) - 1`.

### Маппинг полей

* `contract_number_sys` → номер договора
* `description_ru` → краткое содержание (пусто → `"(описание отсутствует)"`)
* `crdate` → дата создания
* `amount_planned` (Сумма 1) → плановая/утверждённая
* `amount_actual` (Сумма 2) → фактически исполненная
* `amount_total` → `round(amount_planned - amount_actual, 2)`
* `max_income` → введённый пользователем максимальный доход
* URL: `https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{id}`

### Структуры данных

```python
@dataclass
class ContractRecord:
    bin: str
    supplier_name: str
    contract_number: str
    description: str
    cr_datetime: str
    amount_planned: float   # Сумма 1
    amount_actual: float    # Сумма 2
    amount_total: float     # Сумма 1 − Сумма 2
    max_income: float
    url: str
    error: str = ""

@dataclass
class ScrapeResult:
    bin: str
    max_income: float = 0.0
    records: list[ContractRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
```

### Excel-отчёт (excel_export.py)

**Лист «Сводка»** (первый, `index=0`) — агрегаты по каждому БИН:

| Столбец | Описание |
| --- | --- |
| A — БИН компании | |
| B — Наименование компании | |
| C — Кол-во договоров | |
| D — Кол-во ошибок | красным, если > 0 |
| E — Общая итоговая сумма (тг) | сумма `amount_total` по договорам без ошибок |
| F — Максимальный доход (тг) | |
| G — Результат допустимого показателя загрузки | E ÷ F |
| H — Индикатор | 🟢 если ≤ `LOAD_THRESHOLD` (1.5), 🔴 если > |

**Лист «Договоры»** — строка на договор:

| Столбец | Описание |
| --- | --- |
| A — БИН | |
| B — Наименование компании | |
| C — Номер договора | |
| D — Краткое содержание | |
| E — Дата создания | |
| F — Сумма 1 (плановая/утверждённая, тг) | |
| G — Сумма 2 (фактически исполненная, тг) | |
| H — Общая итоговая сумма (тг) | F − G, отрицательные красным |
| I — Максимальный доход (тг) | |
| J — Результат допустимого показателя загрузки | H ÷ I, «—» при ошибке или I ≤ 0 |
| K — Ссылка на договор | Гиперссылка |

Итоговая строка суммирует F, G, H. Файл: `goszakup_report_YYYYMMDD_HHMMSS.xlsx`.

---

## Режим 2: Анализ объявлений (scraper_announcements.py)

Ищет объявления, у которых **есть победитель, но ещё нет договора**.

### Форма ввода

```
[ Протокол итогов с: дата ] [ Протокол итогов по: дата ]
[ Запустить анализ ]
```

* Только диапазон дат публикации протокола итогов (`itogiDatePublic`). **БИН не вводится** — берётся из победителя в протоколе
* Кнопка заблокирована, если `date_from > date_to`

### Шаг 1 — Список объявлений

```graphql
query($filter: TrdBuyFiltersInput, $after: Int) {
  TrdBuy(limit: 50, filter: $filter, after: $after) {
    id numberAnno nameRu refBuyStatusId refTradeMethodsId
    refSubjectTypeId itogiDatePublic countLots totalSum
  }
}
```

**Фиксированные фильтры API:**

* `refBuyStatusId`: `[350]` (Договор подписан — только у него заполнен `itogiDatePublic`)
* `refTradeMethodsId`: `[32, 188]` (Рейтингово-балльная система, Строительство «под ключ»)
* `refSubjectTypeId`: `2` (Работа)

**Фильтрация по дате в Python:**

```python
filtered = [r for r in items
            if r.get("itogiDatePublic")
            and date_from <= str(r["itogiDatePublic"])[:10] <= date_to]
```

### Шаг 2 — Протокол итогов и победитель

1. `TrdBuy(filter: {id: [ann_id]}) { Files { id nameRu filePath } }` → файл, содержащий `"протокол итогов"` в `nameRu` (lowercase). **Нет протокола → объявление пропускается**
2. Скачать `filePath` через `requests.get` с Bearer-заголовком → `BeautifulSoup`. Ошибка → пропуск
3. `_parse_winner_from_protocol`: таблица с `"победител"` / `"жеңімпаз"` в первой строке → в строках `cells[1]` = имя, `cells[2]` = БИН (принимается только чистые 12 цифр); строки-нумерации (`1,2,3…`) отбрасываются. **Нет БИН победителя → пропуск**

### Шаг 3 — Проверка договора

`Contract(limit: 1, filter: {trdBuyNumberAnno: numberAnno})` → **если договор есть, объявление пропускается** (нас интересуют только закупки без договора).

### Шаг 4 — Сумма 1 год

```graphql
query($filter: LotsFiltersInput) {
  Lots(limit: 100, filter: $filter) {
    id lotNumber nameRu
    Plans { id plnPointYear sum1 }
  }
}
```

Суммируются `sum1` всех планов всех лотов, у которых `plnPointYear == 2026`.

### Структуры данных

```python
@dataclass
class AnnouncementRecord:
    bin: str                  # БИН победителя
    supplier_name: str        # Наименование победителя
    announcement_number: str  # numberAnno
    announcement_name: str    # nameRu
    year1_sum: float          # Сумма 1 год
    protocol_url: str
    announcement_url: str
    error: str = ""

@dataclass
class ScrapeAnnouncementsResult:
    results: list[AnnouncementRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_after_filter: int = 0     # после фильтрации по датам
    total_after_algorithm: int = 0  # прошли алгоритм (победитель есть + договора нет)
```

Ссылка на объявление: `https://goszakup.gov.kz/ru/announce/index/{id}`.
Шаблон страницы лота: `https://goszakup.gov.kz/ru/lots/index/{lot_id}` — используется во вспомогательной функции `_fetch_year1_sum` (HTML-парсинг «Сумма 1 год» из таблиц / `dt`-`dd` / regex); в текущем пути исполнения не вызывается, сумма берётся из `Plans` API.

### Excel-отчёт (excel_export_announcements.py)

**Один лист «Объявления»** — строка на объявление:

| Столбец | Описание |
| --- | --- |
| A — БИН | БИН победителя |
| B — Наименование компании | |
| C — Номер объявления | |
| D — Наименование объявления | |
| E — Сумма 1 год (тг) | «—» если 0 |
| F — Ссылка на протокол | Гиперссылка |
| G — Ссылка на объявление | Гиперссылка |

Итоговая строка суммирует столбец E. Файл: `goszakup_announcements_YYYYMMDD_HHMMSS.xlsx`.

---

## Архитектура воркера (worker.py)

Subprocess (`subprocess.Popen([sys.executable, "-u", worker.py, input, output])`), обмен через JSON-файлы во временной директории (`tempfile.mkdtemp`).

**input.json (режим договоров):**

```json
{
  "bins": [{"bin": "031240001439", "max_income": 500000000.0}],
  "progress_file": "path/progress.json"
}
```

Обратная совместимость: если `bins` — список строк, конвертируется в `[{"bin": b, "max_income": 0.0}]`.

**input.json (режим объявлений):**

```json
{
  "mode": "announcements",
  "date": "YYYY-MM-DD",
  "date_to": "YYYY-MM-DD",
  "progress_file": "path/progress.json"
}
```

**Файлы во временной директории:**

| Файл | Назначение |
| --- | --- |
| `input.json` | параметры запуска |
| `progress.json` | текущий прогресс, читается UI по опросу; `done=True` только после полного завершения |
| `output.json` | накапливаемые записи, перезаписывается **после каждой** записи через `on_record` |
| `worker.log` | stdout воркера, показывается в UI при ошибке (последние 3000 символов) |

* Оба файла пишутся атомарно (`.tmp` → `os.replace`) — UI не прочитает обрезанный файл
* При досрочном завершении воркера (например, OOM) `app.py` читает частичный `output.json` и показывает данные с предупреждением
* В режиме объявлений в `output.json` дополнительно пишутся `total_after_filter` и `total_after_algorithm`
* На POSIX воркер запускается в своей группе процессов (`preexec_fn=os.setsid`), чтобы корректно останавливаться

---

## Обработка ошибок

| Ситуация | Поведение |
| --- | --- |
| Договоры по БИН не найдены | строка-заглушка «Договоры не найдены (после фильтрации)», `error="Договоры не найдены"` |
| Протокол не найден в `Files` | объявление пропускается |
| Ошибка скачивания протокола | объявление пропускается, запись в лог |
| Победитель не распознан | объявление пропускается |
| Договор по объявлению уже есть | объявление пропускается (это и есть цель фильтра) |
| Сеть недоступна | retry 2 раза × 3 сек, затем ошибка в `errors` |
| Числовое поле пустое | `0.0` |
| Воркер завершился досрочно | показ частичных данных + предупреждение |
| Таймаут воркера (`WORKER_TIMEOUT = 600` сек) | принудительная остановка |

Кнопка **«⛔ Остановить сбор данных»** убивает процесс воркера (по группе процессов на POSIX).

---

## Справочники

### Статусы объявлений (refBuyStatusId)

| Код | Название | |
| --- | --- | --- |
| 210 | Завершено | |
| 220 | Формирование протокола итогов | |
| 330 | Итоги опубликованы | |
| 350 | Договор подписан | ✅ используется |

### Статусы договоров (ref_contract_status_id)

| Код | Название | |
| --- | --- | --- |
| 190 | Действует | ✅ используется |
| 450 | Создано доп.соглашение | ✅ используется |
| 460 | Передан. Действует | ✅ используется |

### Способы закупки (refTradeMethodsId)

| Код | Название | |
| --- | --- | --- |
| 1 | Конкурс | |
| 2 | Аукцион | |
| 3 | Запрос ценовых предложений | |
| 4 | Из одного источника | |
| 5 | Запрос предложений | |
| 6 | Закупка из одного источника | |
| 32 | Рейтингово-балльная система | ✅ используется |
| 188 | Строительство «под ключ» | ✅ используется |
| 201 | Конкурс с предквалификацией | |

### Тип предмета (refSubjectTypeId / ref_subject_type_id)

| Код | Название | |
| --- | --- | --- |
| 2 | Работа | ✅ используется в обоих режимах |

---

## Зашитые константы, требующие внимания

| Константа | Где | Значение |
| --- | --- | --- |
| `TARGET_YEAR` | `scraper.py` | `2026` — фильтр `crdate` в режиме договоров |
| `plnPointYear == 2026` | `scraper_announcements.py` | год планов при расчёте «Сумма 1 год» |
| `LOAD_THRESHOLD` | `excel_export.py` | `1.5` — порог индикатора 🟢/🔴 |
| `WORKER_TIMEOUT` | `app.py` | `600` сек |
| `MAX_RECORDS` | оба скрапера | `10_000` |

> ⚠️ Год зашит в двух местах. С 2027 года оба режима начнут возвращать пустой результат без правки кода.

---

## Деплой на Render

**Переменные окружения:**

| Переменная | Описание |
| --- | --- |
| `GOSZAKUP_TOKEN` | Bearer-токен для GraphQL API госзакупок |
| `AUTH_SECRET` | Секрет для подписи HMAC-токенов сессий (обязателен) |
| `ADMIN_HASH` | SHA-256 хэш пароля администратора |
| `USER_HASH` | SHA-256 хэш пароля пользователя |
| `ADMIN_ROLE` / `USER_ROLE` | Роли (по умолчанию `admin` / `user`) |
| `ADMIN_DISPLAY` / `USER_DISPLAY` | Отображаемые имена |

Сервис описан в [render.yaml](render.yaml) — нативный Python-runtime:

**Build Command:** `pip install -r requirements.txt`
**Start Command:** `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`

> `Dockerfile` удалён: он остался от Playwright-эры и после коммита `7d8053a` (переход на GraphQL) содержал `RUN playwright install chromium` при отсутствии `playwright` в requirements.txt, то есть заведомо не собирался. Если сервис на Render создан вручную через дашборд, а не через Blueprint, `render.yaml` игнорируется и команды задаются в настройках сервиса.

Локальные секреты — `.streamlit/secrets.toml` (в `.gitignore`, в репозиторий не попадает). Настройки сервера — `.streamlit/config.toml` (`headless = true`, CORS/XSRF отключены).

---

## Локальная отладка

Скраперы запускаются автономно, без UI:

```bash
python scraper.py 031240001439
python scraper_announcements.py 2026-07-04 2026-07-28
```

Требуется `GOSZAKUP_TOKEN` в переменных окружения (вне Streamlit `st.secrets` недоступны).

---

## TODO

* [ ] Хранение истории запросов (база данных)
* [ ] Экспорт в CSV
* [ ] Кэширование результатов
* [ ] Вынести `TARGET_YEAR` и `plnPointYear` в настройки интерфейса
* [ ] Фильтрация договоров по произвольному диапазону дат
* [ ] Множественный фильтр по БИН в режиме объявлений
* [ ] Убрать неиспользуемый `pandas` из requirements.txt
