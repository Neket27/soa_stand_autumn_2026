# SWTR и SourceControl API справка

Файл фиксирует endpoint-ы, на которые опираются скрипты навыка. Он не заменяет Swagger, но дает быстрый рабочий минимум.

> Эндпоинты Confluence и Jira на хосте `sberworks` вынесены отдельно: `references/confluence-api.md` и `references/jira-api.md`; общая матрица бэкендов и авторизации — `references/backends-and-auth.md`. Здесь они не дублируются.

## SWTR Task Tracker

Базовый URL по умолчанию:

```text
https://portal.works.prod.sbt/swtr
```

### Получить задачу по коду

```http
GET /rest/api/unit/v2/CRPV-35498
Accept: application/json
Content-Type: application/json
Authorization: redacted
```

Назначение: получить юнит с атрибутами по коду.

Скрипт:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py get CRPV-35498
```

### Поиск задач через QLRequestDto

```http
POST /rest/api/unit/v3/find
Accept: application/json
Content-Type: application/json
Authorization: redacted
```

Пример тела:

```json
{
  "filters": {
    "spaces": ["CRPV"],
    "properties": {
      "or": [
        {"property": "summary", "operator": "like", "value": "CostCalculator"},
        {"property": "description", "operator": "like", "value": "CostCalculator"},
        {"property": "code", "operator": "like", "value": "CostCalculator"}
      ]
    }
  },
  "attributes": ["solution", "reporter", "priority", "status"],
  "page": {"page": 0, "size": 20},
  "sort": {"attribute": ["updatedAt"], "direction": "DESC"}
}
```

Скрипт высокого уровня:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py search 'CostCalculator' --space CRPV --limit 20
```

Скрипт низкого уровня:

```bash
python3 $SKILL_DIR/scripts/swtr_unit_search.py --body-file /tmp/search-body.json
```

### Поиск внутри быстрого фильтра

```http
POST /rest/api/unit/v3/my-filter/find
```

Скрипт:

```bash
python3 $SKILL_DIR/scripts/swtr_unit_search.py --filter my-filter --body-file /tmp/search-body.json
```

### Поиск через TQL

```http
POST /rest/api/unit/v3/find/tql
```

Тело:

```json
{
  "query": "space = \"CRPV\"",
  "page": {"page": 0, "size": 20},
  "attributes": ["solution", "reporter"]
}
```

**Когда TQL, а когда обычный `find`.** Синтаксис языка описан в `references/tql.md`, поэтому
запрос можно составлять самому, а не только принимать готовый от пользователя:

- `find` — простые условия: пространство, тип задачи, значение атрибута, `like`/`eq`/`in`/`ge`/`le`
  по свойствам;
- TQL — атрибуты типа `user` (обычный `find` по ним не фильтрует вовсе), диапазоны дат, `NOT IN`,
  категории статусов через `statusCategory()` и обход связей через `linkedUnitsOf[Recursive]()`.

Перед выполнением сверить запрос с ограничением движка: поддерживается только чистый AND либо
чистый OR. Скобки бэкенд отвергает явно (`HTTP 400`), а вот смешение AND и OR без скобок проходит
с `200` и неверной выборкой (`references/tql.md`, раздел «Ограничения движка»). Использованный TQL возвращать пользователю в поле `tql` вывода.

### Количество задач

```http
POST /rest/api/unit/v3/count
```

Тело:

```json
{
  "filters": {
    "spaces": ["CRPV"]
  }
}
```

### Wiki-страница задачи

```http
GET /extension/plugin/v2/rest/api/swtr_wiki_plugin/v2/wiki/unit/CRPV-35498
GET /extension/plugin/v2/rest/api/swtr_wiki_plugin/v1/wiki/unit/CRPV-35498
```

Скрипт `swtr_task_tool.py get` пробует оба endpoint-а и возвращает их в поле `wiki` JSON-вывода (на диск ничего не пишется).

### Поиск wiki-страниц

```http
POST /extension/plugin/v2/rest/api/swtr_wiki_plugin/v1/wiki/unit/find
```

Тело:

```json
{
  "filters": {
    "query": "CostCalculator",
    "spaces": ["CRPV"]
  },
  "page": {"page": 0, "size": 20}
}
```

### Создание задачи

```http
POST /rest/api/unit/v2/task/create
```

Тело:

```json
{
  "summary": "Добавить метрики сайзинга для CostCalculator",
  "description": "Нужно добавить метрики LGDB и COTE для снижения погрешности сайзинга.",
  "space": "PVM",
  "attributes": {
    "priority": "mid"
  }
}
```

Dry-run:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit task --space PVM --summary 'Добавить метрики сайзинга для CostCalculator' --description 'Нужно добавить метрики LGDB и COTE для снижения погрешности сайзинга.' --attribute priority=mid
```

Реальное создание требует `--execute`.

### Изменение атрибутов юнита

```http
PATCH /rest/api/unit/v2/update/{code}
```

Тело — только изменяемые атрибуты, ответ — код юнита:

```json
{"attributes": {"fix_version": [{"code": "81a0a9f4-…"}]}}
```

Глагол стоит **перед** кодом. Привычная форма `/rest/api/unit/v2/{code}/update` возвращает `404`,
а сам путь `/rest/api/unit/v2/{code}` разрешает только `GET`. Мультизначное поле перезаписывается
целиком. Статус так не меняется — см. следующий раздел.

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py update PVM-175440 --component VLOG --execute
```

### Смена статуса юнита

Статус — проекция конечного автомата, а не поле: `workflow_status` в теле обновления
юнита даёт `HTTP 500`. Меняется выполнением действия, доступного из текущего статуса.

```http
GET   /rest/api/workflow/v1/unit/{code}/status/get?conditionEnabled=true
GET   /rest/api/workflow/v1/workflow/{schemaCode}
PATCH /rest/api/workflow/v2/unit/status/change
PATCH /rest/api/workflow/v1/unit/{code}/status/change/{actionCode}
```

`status/get` отдаёт `currentStatus`, `availableActions[]`,
`actionsExcludedByCondition[]` и `wfSchemaDto` — из последнего берётся `schemaCode`
для графа. `workflow/{schemaCode}` отдаёт `wfTransitions[]` из
`{wfAction, oldStatus, destStatus}`; у перехода создания `oldStatus: null`.

Тело `v2`-ручки:

```json
{
  "unitCode": "STS-452231",
  "actionCode": "ZVRSHT_lMacNZCcxwjcw"
}
```

Коды действий генерируются для каждой схемы workflow — брать только из
`availableActions`, не хардкодить. Недоступное или несуществующее действие даёт
`HTTP 570` «непредвиденная ошибка при выполнении валидатора» и статус не меняет.

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py status get STS-452231
python3 $SKILL_DIR/scripts/swtr_task_tool.py status set STS-452231 --to RESOLVED
```

Реальный переход требует `--execute`.

### Резолв пользователя (login -> externalId)

Атрибут `assigned_to` имеет тип `user`, и фильтр по нему сравнивает **`externalId`
учётки с учётом регистра**, а не логин: `assigned_to in ("savinov.m.y")` находит
ноль задач там, где `("Savinov.M.Y")` находит все. Сопоставление даёт отдельный
endpoint; его тело — **голый JSON-массив логинов в нижнем регистре** (объектное
тело он отвергает с `HttpMessageNotReadableException`):

```http
POST /rest/api/user/v1/find
```

```json
["savinov.m.y", "naumova.m.v"]
```

Ответ — массив пользователей, где `code` и есть нужный `externalId`:

```json
[{"code": "Savinov.M.Y", "login": "savinov.m.y", "firstName": "Максим", "lastName": "Савинов"}]
```

Неизвестный логин просто отсутствует в ответе (не ошибка). Значение, переданное
не в нижнем регистре, не матчится: `["Savinov.M.Y"]` вернёт пустой массив.

### Поиск назначенных задач

Только TQL. Атрибутный фильтр обычного `find` по `assigned_to` не находит ничего
ни в одном формате значения (логин, `externalId`, объект пользователя, ФИО),
хотя тот же запрос без фильтра юниты отдаёт:

```http
POST /rest/api/unit/v3/find/tql
```

Тело:

```json
{
  "query": "assigned_to in (\"Vasiakuzyakin.I.I\") and workflow_status not in (\"done\", \"closed\", \"resolved\", \"rejected\") and space in (\"PVM\")",
  "attributes": ["assigned_to", "workflow_status", "priority", "updatedAt"],
  "page": {"page": 0, "size": 50}
}
```

Скрипт высокого уровня:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py assigned --user vasiakuzyakin --space PVM --status in-progress
```

### Выполненные задачи за неделю

```http
POST /rest/api/unit/v3/find/tql
```

Тело:

```json
{
  "query": "assigned_to in (\"Vasiakuzyakin.I.I\", \"Maxisavinov.M.Y\") and workflow_status in (\"done\", \"closed\", \"resolved\", \"rejected\") and updatedAt >= \"2026-04-30T00:00:00Z\" and space in (\"PVM\")",
  "attributes": ["assigned_to", "workflow_status", "updatedAt"],
  "page": {"page": 0, "size": 100}
}
```

Скрипт высокого уровня:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py done-weekly --user vasiakuzyakin --user maxisavinov --space PVM --since 2026-04-30
```

Границу периода `updatedAt` нужно передавать как `date_time`: голую дату TQL
отвергает (`Некорректное значение '2026-04-30' для типа атрибута 'date_time'`),
поэтому `--since YYYY-MM-DD` скрипт достраивает до `YYYY-MM-DDT00:00:00Z`.
Логины в обоих сценариях скрипт сам меняет на `externalId` — см. «Резолв
пользователя» выше.

## SWTR Test Management System (TMS) API

Базовый префикс: `/extension/plugin/v2/rest/api/swtr_tms_plugin/v1`.

Скрипт высокого уровня — `swtr_task_tool.py tms <kind>`. Все используемые endpoint-ы read-only; write-action endpoint-ы намеренно не вызываются.

### Доступные пространства TMS

```http
GET /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/test_plans/acceptable_spaces
```

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms acceptable-spaces
```

### Тест-планы пространства

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/test_plans
```

Тело (минимум):

```json
{
  "unitFilters": {
    "filters": {"spaces": ["TMS"]},
    "attributes": ["test_plan_status", "release", "owner", "plan_date_start", "plan_date_end"],
    "page": {"page": 0, "size": 20}
  }
}
```

Дополнительно поддерживается фильтр `linkedTo`, который ограничивает выдачу планами, привязанными к указанному юниту:

```json
{
  "linkedTo": {"code": "TMS-5"},
  "unitFilters": {"filters": {"spaces": ["TMS"]}, "page": {"page": 0, "size": 20}}
}
```

Альтернатива — TQL по тест-планам:

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/test_plans/tql
```

### Тест-циклы, привязанные к плану

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/link/test_plan/test_cycles
```

Тело:

```json
{
  "unitId": {"code": "TMS-25"},
  "unitFilters": {
    "attributes": ["test_cycle_status", "plan_date_end", "plan_date_start", "release", "owner"],
    "page": {"page": 0, "size": 20}
  }
}
```

### Тест-кейсы, привязанные к циклу

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/link/test_cycle/test_cases
```

Тело:

```json
{
  "unitId": {"code": "TMS-27"},
  "unitFilters": {
    "attributes": ["status", "test_case_test_type", "priority", "owner"],
    "page": {"page": 0, "size": 20}
  }
}
```

### Иерархия папок от корня

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/root
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/root/units
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/root/units/progress
```

Тело `folder/root`:

```json
{"type": "TEST_CASE", "spaceId": {"code": "TMS"}}
```

Тело `folder/root/units`:

```json
{
  "getRootFolderRq": {"type": "TEST_CASE", "spaceId": {"code": "TMS"}},
  "linkedTo": {"code": "TMS-5"},
  "unitFilters": {"page": {"page": 0, "size": 20}}
}
```

### Иерархия папок от заданной папки

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/hierarchy/{folder_code}/units
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/hierarchy/{folder_code}/units/progress
```

### Карточка тест-кейса

```http
GET /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/test_case/{issueKey}/test_case_data
```

### Шаги прогона

```http
GET /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/run/{code}/test_step_info
```

### Интеграционные юниты TMS

```http
POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/integration/units
```

Тело `SPUnitSearchRq` (обязательно `from`):

```json
{
  "type": "TEST_CYCLE",
  "attributes": ["status"],
  "from": "2024-01-01T00:00:00.000Z",
  "page": {"page": 0, "size": 20}
}
```

### TMS write-action (документация-only)

Скрипт `swtr_task_tool.py` сознательно не вызывает write-action TMS endpoint-ы. Они описаны здесь, чтобы был зафиксирован полный API-контур и не было соблазна вызвать их «неявно»:

- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/run/create` — создает прогон.
- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/run/{unit_id}/status` — меняет статус прогона.
- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/safe_data/{generation_id}/start` и `/update`, `DELETE /safe_data/{generation_id}/delete` — управление safe data.
- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/{space_code}/files/upload` и `/files/delete` — операции с файлами TMS.
- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/folder/create`, `/folder/move`, `/folder/rename` — управление папками.
- `POST /extension/plugin/v2/rest/api/swtr_tms_plugin/v1/link` и `/link/delete` — линки.

Любое использование этих endpoint-ов выполняется только по явной просьбе пользователя и в рамках отдельного скрипта или ручного `curl` с обоснованием.

## Discovery-ручки (проверены вживую 13.08.2026)

Все отвечают `200` под нашим Basic-кредом и закрыты командами навыка — см. колонку «Команда».
Напрямую их дёргать не нужно; `swtr_unit_search.py --body-file` для них всё равно не подходит,
он умеет только пути `/rest/api/unit/v3/*`.

| Endpoint | Команда | Что отдаёт |
|---|---|---|
| `POST /rest/api/space/v3/find` | `spaces [подстрока]` | все доступные пространства (85 на момент проверки): `{code, name, type}` |
| `GET /rest/api/space/v3/{space}/suits` | `suits <space>` | типы задач с привязанным workflow; у типов без процесса (`wiki_page`) привязка `null` |
| `GET /rest/api/workflow/v1/suit/{suit}/space/{space}/workflow` | `workflow --suit … --space …` | схема по паре «тип + пространство»; у CRPV/`epic_crpv` — 24 статуса и 65 переходов |
| `GET /rest/api/suit/v1/{suit}` | команды нет | свойства типа: `baseAttributes` (`summary`, `description`) и пользовательские атрибуты с типами и справочниками |
| `GET /rest/api/unit/v2/filters` | `filters` | сохранённые фильтры пользователя (`favorite-units`, `my-unit`) |
| `POST /extension/plugin/v2/rest/api/swtr_task_tracker_plugin/v1/version/find-all` | `versions <space>` | версии пространства; архивные скрыты, вернуть их — `--include-archived` |

Парная ручка создания версии (`/version/create`, тело `{"name", "description", "space"}`) требует
прав координатора пространства и **не проверялась**: это запись.

## Связи юнитов и типы задач пространства

- `POST /rest/api/unit/v1/link` — создать/изменить связь (`UnitLinkRq = {source, destination, type}`).
- `POST /rest/api/unit/v1/link/find` — найти связи юнита (`{unitId, types?, direction?, page}`).
- `POST /rest/api/unit/v2/link/types` — каталог типов связей (`{filters:{textSearch?, linkTypeCodes?}, page:{size,page}}`).
- `DELETE /rest/api/unit/v1/link/delete` — пометить связь удалённой.
- `GET /rest/api/space/v3/{space}/suits` — типы задач (suits), доступные в пространстве.

## SourceControl API V3

Базовый URL по умолчанию:

```text
https://portal.works.prod.sbt/ssd/tools/sc/api/v3
```

Swagger base path: `/ssd/tools/sc/api/v3`.

### Список репозиториев

```http
GET /repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm?page=1&limit=50&sort=updated_at:desc,name:asc
```

Скрипт:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --limit 50
```

Поиск по имени/описанию репозитория:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query cost --limit 20
```

### Получить репозиторий

```http
GET /repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm/pvm-backend
```

### Список веток

```http
GET /repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm/pvm-backend/branches?page=1&limit=30&sort=date:desc,name:asc
```

Скрипт:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py branches --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --limit 30
```

### Получить файл

```http
GET /repos/22f434fb-0355-42a1-ac25-011074a20a2d/pvm/pvm-backend/raw/src/main/resources/application.yml?ref=master
```

Скрипт:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py file --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --filepath src/main/resources/application.yml
```

### Поиск по коду

Оптимальная стратегия:

1. Получить SHA ветки через `GET /branches/{branch_name}`.
2. Скачать архив через `GET /archive?sha=...&format=zip`.
3. Выполнить локальный поиск по текстовым файлам архива.
4. Если архив недоступен, перейти на fallback: `contents` для дерева и `raw` для файлов.

Скрипт:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --query 'CostCalculator' --query 'sizing' --limit 30
```

### Ограничения поиска по коду

- Не утверждать, что функциональности нет вообще, если проверен только один репозиторий или одна ветка.
- Если `--repo` не указан, сначала показать кандидатов репозиториев.
- Если достигнут `--max-files`, указать это в ответе как ограничение.
- Если архив недоступен и fallback дал ошибки по директориям/файлам, указать это в ответе.
