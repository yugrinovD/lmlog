# lmlog

**Локальный CPU-first анализатор логов: сигнатуры, ошибки, аномалии и объяснения для очень больших объёмов.**

`lmlog` превращает миллионы строк структурированных логов в несколько десятков
понятных сигнатур, находит ошибки, новые шаблоны и аномалии по истории, а затем
объясняет находки человеческим языком — детерминированно или через локальную LLM.

Проект рассчитан на **50 млн строк в сутки и больше**, не требует GPU, не
отправляет данные наружу и работает в изолированных (air-gapped) средах.

[English version](README.en.md)

---

## Зачем это нужно

В коробочных инсталляциях крупных PHP/Symfony-приложений основной поток логов —
это шум: десятки миллионов однотипных INFO-строк в сутки. На их фоне теряются
единичные реальные ошибки, а ручной `grep` по гигабайтам файлов не масштабируется.

`lmlog` решает три задачи:

1. **Редукция**: миллионы строк → десятки уникальных сигнатур с частотой и трендом.
2. **Детекция**: ошибки, новые шаблоны, аномальный рост и «замолчавшие» паттерны.
3. **Объяснение**: локальная LLM или эвристический fallback отвечает, что
   происходит, насколько это критично и что проверить.

## Ключевые возможности

- **Потоковый ingest** — память не растёт с объёмом логов; списки строк никогда
  не материализуются.
- **Точные байтовые offset + inode** — корректный инкрементальный дочитыв и
  обнаружение ротации файлов.
- **Шаблонизация** — UUID, числа, IP, хеши и timestamp'ы схлопываются в токены.
- **Параллельный парсинг** — `--workers N` для нескольких файлов/ядер.
- **Детекция** — ERROR-кластеры, новые сигнатуры, частый шум, всплески, спады.
- **Тренды по дням** — `daily_counts` в SQLite, медианная базовая линия.
- **LLM через gateway** — по умолчанию `lmlog` ходит в `gogate` по
  OpenAI-совместимому API; standalone-режим `direct` оставляет прямую работу с
  Ollama. Кэш объяснений хранится в SQLite, при недоступности LLM автоматически
  используется эвристика.
- **Генератор логов** — реалистичный синтетический поток для демо и бенчмарков.
- **Docker + Compose** — генератор, CLI, демон и опциональный standalone
  Ollama-профиль `ai`.
- **Ноль обязательных зависимостей** — только стандартная библиотека Python;
  `orjson` подхватывается автоматически, если установлен.

## Архитектура

```mermaid
flowchart LR
    A[Log files / journald] -->|stream, byte offsets| B[Parser]
    B --> C[Normalizer]
    C --> D[Signature]
    D --> E[Clusterer + daily counters]
    E --> F[(SQLite snapshot)]
    F --> G[Detect: errors / new / noise]
    F --> H[Trends: spikes / drops / new]
    G --> I{Explain}
    H --> I
    I -->|gateway| J[gogate :8080]
    J --> O[(Ollama, shared infra)]
    I -->|direct, standalone| O
    I -->|fallback| K[Heuristic rules]
```

Тяжёлый проход (`ingest`) читает файлы и сохраняет снимок кластеров в SQLite.
Все запросы (`scan`, `errors`, `trends`, `explain`) работают по снимку и
выполняются мгновенно. Демон `lmlog daemon` периодически дочитывает только новые
байты и держит снимок свежим.

## Быстрый старт: Docker

```bash
cp .env.example .env
make demo
```

`make demo` собирает образ, генерирует синтетические логи, выполняет `ingest`,
показывает находки и ERROR-кластеры.

Полезные команды:

```bash
make generate     # сгенерировать логи (LMLOG_DEMO_LINES, LMLOG_DEMO_DAYS)
make ingest       # прочитать логи и сохранить снимок
make scan         # находки
make errors       # ERROR-кластеры
make explain      # объяснения (эвристика, LLM не нужна)
make trends       # аномалии по истории
make daemon-up    # фоновый инкрементальный ingest
make bench        # замер пропускной способности
make ai-up        # standalone-dev: поднять Ollama и скачать модель
make pull-model   # только скачать модель
make down         # остановить сервисы
```

Ручной запуск в контейнере:

```bash
docker compose run --rm lmlog ingest
docker compose run --rm lmlog scan
docker compose run --rm lmlog errors
docker compose run --rm lmlog explain --no-llm
```

## Быстрый старт: локально

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 1. Сгенерировать демо-логи в data/logs
lmlog generate --lines 200000 --days 1 --profile demo

# 2. Прочитать логи и сохранить снимок
lmlog ingest

# 3. Мгновенные запросы по снимку
lmlog scan
lmlog errors
lmlog classes Handler
lmlog explain --no-llm
```

По умолчанию состояние хранится в `~/.lmlog/lmlog.db`, а логи читаются из
`data/logs` (переопределяется через `--log-dir` / `LMLOG_LOG_DIR`).

### Пример вывода

```text
$ lmlog ingest
Ingest finished: files=1, lines=200 000, entries=200 000, signatures=12,
time=2.2s (90 415 lines/s).
Snapshot saved. `scan`, `errors`, `classes` and `explain` now read from it instantly.

$ lmlog errors -n 3
ERROR clusters: 6

======================================================================
errors=1189  total=1 189  first=2026-08-25T00:00:31  last=2026-08-25T23:59:43
  msg: Failed request to Storage {method} {url} {statusCode}
======================================================================
errors=419  total=419  first=2026-08-25T00:02:32  last=2026-08-25T23:55:38
  msg: Storage Disk Space Error:Cannot complete Storage request in 3 attempts
======================================================================
errors=410  total=410  first=2026-08-25T00:02:35  last=2026-08-25T23:53:49
  classes: Infrastructure\Repository\EventTimetableRepository
  msg: Uncaught PHP Exception Doctrine\DBAL\Exception\IntegrityConstraintViolationException
```

## Демо сценарий

1. **Генерация**: `lmlog generate --lines 200000 --days 7 --profile demo` —
   семь дней реалистичного потока (шум + повторяющиеся ошибки). Для многодневного
   демо генератор специально добавляет всплеск ошибок, «замолчавший» консьюмер и
   новый шаблон — чтобы `trends` сразу показал осмысленный результат
   (`--no-anomaly` отключает это поведение).
2. **Ingest**: `lmlog ingest` — снимок и дневная история.
3. **Разбор**: `lmlog scan` и `lmlog errors` — ошибки, новые шаблоны, шум.
4. **Тренды**: `lmlog trends` — всплески, «замолчавшие» паттерны, новые шаблоны.
5. **Explain**: `lmlog explain --no-llm` — структурированные эвристики;
   с `make ai-up` и `LMLOG_LLM_MODE=direct` — объяснения локальной модели,
   в полном стеке — через `gogate`.
6. **Daemon**: `make daemon-up` — фоновое обновление снимка каждые 5 минут.
7. **Benchmark**: `lmlog bench --lines 1000000` — пропускная способность.

## Команды

| Команда | Назначение |
| --- | --- |
| `lmlog` / `lmlog wizard` | Интерактивный ассистент |
| `lmlog generate` | Сгенерировать синтетические логи |
| `lmlog ingest` | Прочитать логи и сохранить снимок |
| `lmlog scan` | Находки: ошибки, новые и частые шаблоны |
| `lmlog errors` | ERROR/CRITICAL-кластеры |
| `lmlog classes <name>` | Поиск по class/namespace |
| `lmlog explain` | Объяснения (LLM или эвристика) |
| `lmlog trends` | Проактивные тренды по истории дней |
| `lmlog status` / `summary` | Текущая ситуация |
| `lmlog daemon` | Фоновый инкрементальный ingest |
| `lmlog serve` | HTTP API (нужен extra `serve` и токен) |
| `lmlog bench` | Замер пропускной способности |
| `lmlog journal` | Системный журнал (если доступен journalctl) |

Полная справка: `lmlog <command> --help`.

## HTTP API: `lmlog serve`

Для веб-консоли есть опциональный режим HTTP API. Он не входит в базовую установку:

```bash
pip install -e ".[serve]"
export LMLOG_API_TOKEN="$(openssl rand -hex 32)"
lmlog serve --host 127.0.0.1 --port 8090
```

Docker:

```bash
LMLOG_API_TOKEN="$(openssl rand -hex 32)" make serve
```

API защищён `Authorization: Bearer ${LMLOG_API_TOKEN}` (constant-time сравнение).
Токен обязателен: без него сервис не стартует.

| Метод | Путь | Назначение |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | загрузить `.log/.txt/.jsonl` (≤ 50 МБ) и запустить ingest |
| `GET` | `/api/v1/jobs` | последние задания |
| `GET` | `/api/v1/jobs/{id}` | статус и статистика задания |
| `GET` | `/api/v1/jobs/{id}/events` | SSE-прогресс с `Last-Event-ID` |
| `GET` | `/api/v1/snapshots/{id}/summary` | сводка снимка |
| `GET` | `/api/v1/snapshots/{id}/signatures` | сигнатуры с фильтрами и пагинацией |
| `GET` | `/api/v1/snapshots/{id}/errors` | ERROR/CRITICAL-кластеры |
| `GET` | `/api/v1/snapshots/{id}/trends` | всплески, спады и новые шаблоны |
| `POST` | `/api/v1/explain` | объяснение кластера (LLM или эвристика) |
| `POST` | `/api/v1/chat` | вопрос по снимку: top-N сигнатур + выбранный кластер |
| `GET` | `/healthz` | liveness без авторизации, режим и модель LLM |

Ограничения режима `serve`:

- задания и SSE-очереди живут в памяти процесса; запускайте один uvicorn-worker;
- загруженные файлы и SQLite-снимки хранятся в `LMLOG_SERVE_DATA_DIR`
  (по умолчанию `~/.lmlog/serve`);
- чат не использует RAG/эмбеддинги: модель видит только сводку, top-N сигнатур,
  выбранный кластер и последние тренды;
- при недоступном `gogate` (или Ollama за ним) объяснения и чат переключаются на
  детерминированную эвристику; ingest/scan/trends продолжают работать;
- имена файлов санитизируются, на диск всегда пишется сгенерированное имя;
- лимит загрузки по умолчанию 50 МБ, расширения — `.log`, `.txt`, `.jsonl`.

## Корректность данных

- `clusters` — каталог сигнатур: **любой полный ingest всегда пересобирает все
  файлы `log_dir`** (фильтры `--since/--limit` для него игнорируются), а
  incremental добавляет только дельту нового прохода. Повторный full-ingest
  идемпотентен.
- При ротации или усечении файла в incremental-режиме `lmlog` пересобирает
  снимок из **всех** файлов `log_dir` — это дороже, но исключает двойной учёт.
- Базы версии 1.0.0 автоматически пересобираются при первом `ingest`
  (миграция схемы 1 → 2).
- Timestamp'ы сравниваются в UTC, поэтому смещения (`+03:00`, `Z`) не ломают
  порядок `first_seen`/`last_seen`.
- Физические строки и успешно распарсенные записи считаются отдельно, поэтому
  битые строки видны в статистике и не искажают throughput.

## Масштаб и производительность

`lmlog` спроектирован под **50M+ строк в сутки**:

- ingest потоковый: память зависит от числа уникальных сигнатур, а не от объёма
  строк;
- чтение бинарное, offset — байтовый, с учётом inode;
- SQLite в режиме WAL, батчевые вставки, `executemany`;
- при необходимости — параллельный парсинг нескольких файлов (`--workers N`);
- опционально `orjson` для ускорения разбора JSON.

Замер на синтетическом stress-профиле (один процесс, обычная dev-машина):

```text
$ lmlog bench --lines 1000000
Throughput      : ~85 000 lines/s
50M lines at this rate: ~9.8 min
```

Цифры зависят от железа, формата строк и доли JSON. Это не «потолок», а
ориентир: инструмент рассчитан на десятки миллионов строк в сутки, а не на
конкретный объём.

## Формат логов

Поддерживается Symfony/Monolog:

```text
[2026-08-25T11:44:54.253480+00:00] app.INFO: message {"context"} {"extra"}
```

- `context` может содержать `class` — namespace кода.
- `extra` часто содержит `uid`.
- Дополнительно (опционально) читается journald через `journalctl`.

Файлы по умолчанию: `console-*.log`, `prod-*.log`, `courses_console-*.log`, `*.log`.
Каталог задаётся через `--log-dir` или `LMLOG_LOG_DIR`.

## LLM: gateway и direct

`lmlog` не владеет LLM-инфраструктурой. По умолчанию он вызывает `gogate` по
OpenAI-совместимому API (`/v1/chat/completions`), а `gogate` уже ходит в Ollama.
Это даёт единые retry/таймауты/лимиты/кэш/метрики на весь AI-трафик.

```dotenv
# Полный стек (рекомендуется)
LMLOG_LLM_MODE=gateway
GOGATE_BASE_URL=http://gogate:8080
GOGATE_API_KEY=<ключ из gogate>

# Standalone-разработка без gogate
LMLOG_LLM_MODE=direct
OLLAMA_HOST=ollama:11434
```

```bash
make ai-up                   # standalone-dev: profile ai + ollama pull
LMLOG_LLM_MODE=direct lmlog explain
lmlog explain                # gateway по умолчанию
lmlog explain --no-llm       # только эвристика
lmlog explain --refresh      # игнорировать кэш
```

При недоступном gateway или модели `lmlog` возвращает `source=heuristic` и не
ломает ingest/scan/trends. Объяснения кэшируются по сигнатуре в SQLite.

> **Migration note:** раньше `lmlog` по умолчанию ходил в Ollama напрямую.
> Теперь default — `gateway`. Для старого поведения задайте
> `LMLOG_LLM_MODE=direct` (standalone compose делает это автоматически).

> Standalone-профиль `ai` и полный стек взаимоисключающие: не запускайте два
> Ollama одновременно — это двойной расход RAM и конфликт порта `11434`.
> В полном стеке используйте `infra/`.

## Конфигурация

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `LMLOG_HOME` | `~/.lmlog` | Каталог состояния (daemon pid/log) |
| `LMLOG_DB` | `$LMLOG_HOME/lmlog.db` | Путь к SQLite |
| `LMLOG_LOG_DIR` | `data/logs` | Каталог логов |
| `LMLOG_LLM_MODE` | `gateway` | `gateway` (gogate) или `direct` (Ollama) |
| `LMLOG_MODEL` | `qwen2.5:3b` | Модель в gateway или Ollama |
| `GOGATE_BASE_URL` | `http://127.0.0.1:8080` | Public listener gogate |
| `GOGATE_API_KEY` | — | API-ключ для gateway |
| `GOGATE_API_KEY_FILE` | — | Файл с ключом (Docker secrets) |
| `OLLAMA_HOST` | `127.0.0.1:11434` | Адрес Ollama, только в `direct` |
| `NO_COLOR` | — | Отключить ANSI-цвета |

## Разработка

```bash
pip install -e ".[dev]"
pytest
ruff check .
lmlog bench --lines 1000000
```

CI: GitHub Actions, Python 3.10–3.13, lint + tests.

## Структура проекта

```text
src/lmlog/
  parser.py      # разбор Monolog
  normalize.py   # шаблонизация и сигнатуры
  cluster.py     # кластеризация и статистика
  ingest.py      # потоковое чтение файлов и journald
  state.py       # SQLite: offsets, снимок, история, кэш
  detect.py      # ошибки, новые шаблоны, тренды
  explain.py     # эвристические объяснения
  llm.py         # клиенты gogate (gateway) и Ollama (direct)
  generator.py   # генератор синтетических логов
  config.py      # конфигурация и env
  cli.py         # CLI
  serve/         # optional FastAPI HTTP API
  wizard.py      # интерактивный режим
  daemon.py      # фоновое обновление
tests/           # pytest
examples/        # пример лога
```

## Ограничения

- Ориентирован на структурированные Monolog-логи; произвольный текст
  обрабатывается как сообщение без контекста.
- journald доступен только там, где есть `journalctl` (в контейнере — опционально).
- LLM объясняет десятки уникальных шаблонов, а не поток строк — это осознанное
  архитектурное решение. В полном стеке LLM-вызовы идут только через `gogate`.
- SQLite — единая точка состояния; для одного хоста и одного набора логов этого
  достаточно.
- При ротации/усечении в incremental-режиме выполняется пересборка всех файлов
  `log_dir` (полный проход); `--since/--limit` для full/rebuild игнорируются.
- Copytruncate, при котором файл усечён и успел вырасти до прежнего размера между
  проверками, может быть не обнаружен. Надёжнее rename/create-ротация или
  уменьшение размера файла между проверками.
- Сжатые `*.gz` не читаются; предупреждение выводится, только если имя файла
  соответствует шаблонам логов.

## Лицензия

MIT. См. [LICENSE](LICENSE).
