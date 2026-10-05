# Jira (sberworks) API справка

Скрипт `jira_tool.py` работает с Jira Data Center на `sberworks`. Базовый URL по умолчанию `https://sberworks.ru/jira` (override — `JIRA_BASE_URL`). Авторизация — Bearer PAT, отдельный от Confluence (см. `references/backends-and-auth.md`). Скрипт печатает один JSON-объект в stdout, файлов не создаёт, секреты не печатает. Read-only по умолчанию; `comment`, `create` и `edit` мутируют только с `--execute`.

**Приоритет.** Jira sberworks — крайний приоритет: задачи по умолчанию живут в SWTR Task Tracker (`swtr_task_tool.py`). Все команды `jira_tool.py` требуют флаг `--confirm-sberworks`; без него скрипт отказывается работать (`{"ok": false, "gate": "sberworks-not-default"}` в stderr, exit 1) и не делает ни одного сетевого вызова. Ставить флаг только когда пользователь явно назвал Jira/sberworks или дал ссылку `sberworks.ru` — см. «Приоритет систем» в `SKILL.md`.

## Endpoint-ы

- `GET /rest/api/2/issue/{KEY}?fields=…&expand=renderedFields` — задача с полями и отрендеренным описанием.
- `GET /rest/api/2/search?jql=<jql>&maxResults=N&fields=…` — поиск по JQL.
- `GET /rest/api/2/issue/{KEY}/comment` — комментарии задачи.
- `POST /rest/api/2/issue/{KEY}/comment` — добавить комментарий (write, `--execute`).
- `POST /rest/api/2/issue` — создать задачу (write, `--execute`).
- `PUT /rest/api/2/issue/{KEY}` — изменить поля задачи (write, `--execute`).
- `GET /rest/api/2/myself` — текущий пользователь (используется доктором как probe).

## get — получить задачу

```bash
python3 $SKILL_DIR/scripts/jira_tool.py get PVM-52489 --confirm-sberworks
```

Запрашиваются поля `summary,status,issuetype,priority,assignee,reporter,created,updated,labels,components,description,parent,subtasks,comment,issuelinks` плюс `expand=renderedFields`. JSON-вывод нормализован: `{command:"jira.get", key, type, status, priority, summary, assignee, reporter, created, updated, labels, components, parent, subtasks, links:[{type,key}], comments_total, description, rendered_description}`.

## search — поиск по JQL

Если передан текст, скрипт строит `jql = 'text ~ "<текст>"'` (плюс `and project = <KEY>`, если задан `--project`) ` order by updated desc`. Готовый JQL — через `--jql` (переопределяет текст).

```bash
python3 $SKILL_DIR/scripts/jira_tool.py search 'мониторинг бизнес-процессов' --project PVM --limit 30 --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py search --jql 'project = PVM and status = "In Progress" order by updated desc' --confirm-sberworks
```

JSON-вывод: `{command:"jira.search", jql, total, size, issues:[{key,project,type,status,summary,updated}]}`.

Шпаргалка JQL: `text ~ "фраза"` (полнотекст), `summary ~ "..."`, `project = PVM`, `key = PVM-1`, `status = "To Do"`, `assignee = login`, `labels = "..."`, `issuetype = Epic`, `updated >= -7d`, `order by updated desc`. Оператор `~` — нечёткое совпадение.

## similar — похожие задачи

Принимает ключ (`PVM-52489`) или произвольный текст. Для ключа сначала читается `summary` задачи, из неё извлекаются термины (короткие слова и стоп-слова отбрасываются), строится JQL `(summary ~ "t1" OR summary ~ "t2" …)`, исключается сам ключ, `order by updated desc`. Результаты ранжируются по числу совпавших терминов в `summary`.

```bash
python3 $SKILL_DIR/scripts/jira_tool.py similar PVM-52489 --project PVM --limit 20 --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py similar 'end-to-end мониторинг бизнес-процессов' --confirm-sberworks
```

JSON-вывод: `{command:"jira.similar", seed, terms, jql, total, results:[{key,project,status,summary,updated,score}]}`, отсортировано по `score` (число совпавших терминов).

> **Омонимы дают шум.** Короткие аббревиатуры (например `E2E`) встречаются в разных контекстах (мониторинг и тесты). Сужать выдачу через `--project` и опираться на `score`; при необходимости — отдельный `search --jql` по более точному условию.

## comments — комментарии задачи

```bash
python3 $SKILL_DIR/scripts/jira_tool.py comments PVM-52489 --confirm-sberworks
```

JSON-вывод: `{command:"jira.comments", key, total, comments:[{author,created,body}]}`.

## comment — добавить комментарий (write, gated)

Без `--execute` — dry-run (печать намерения, без POST).

```bash
# dry-run
python3 $SKILL_DIR/scripts/jira_tool.py comment PVM-52489 --body-file /tmp/comment.txt --confirm-sberworks
# реальная отправка
python3 $SKILL_DIR/scripts/jira_tool.py comment PVM-52489 --body-file /tmp/comment.txt --execute --confirm-sberworks
```

Тело POST: `{"body": "<wiki markup>"}`. JSON dry-run: `{command:"jira.comment", mode:"dry-run", key, bodyChars, note}`; после `--execute`: `{command, mode:"executed", key, httpStatus, response}`.

## create — создать задачу (write, gated)

```bash
# dry-run печатает payload
python3 $SKILL_DIR/scripts/jira_tool.py create --project PVM --type Task --summary 'Добавить метрики сайзинга' --description-file /tmp/desc.txt --field priority=High --confirm-sberworks
# реальное создание
python3 $SKILL_DIR/scripts/jira_tool.py create --project PVM --type Task --summary 'Добавить метрики сайзинга' --description-file /tmp/desc.txt --execute --confirm-sberworks
```

Тело: `{"fields": {"project":{"key":<PROJECT>}, "issuetype":{"name":<TYPE>}, "summary":<...>, "description":<file>, …}}`. `--field k=v` (повторяемый) добавляет произвольные поля как строки (для структурированных полей лучше сначала посмотреть dry-run payload и при необходимости готовить тело вручную). JSON dry-run: `{command:"jira.create", mode:"dry-run", request, note}`.

## edit — изменить поля задачи (write, gated)

Обновляет поля существующей задачи через `PUT /rest/api/2/issue/{KEY}`. Три способа задать поле:

- `--set <fieldId>=<value>` — скалярное значение (повторяемый);
- `--field <fieldId>=<value>` — значение как элемент списка, для labels-подобных полей (повторяемый);
- `--component <name>` — заменяет набор компонентов (`components`) на переданные имена (повторяемый).

Если ни одно из них не задано — ошибка `Nothing to set …` (exit 1, сетевого вызова нет). Без `--execute` — dry-run (печать намерения, без PUT).

```bash
# dry-run печатает тело PUT
python3 $SKILL_DIR/scripts/jira_tool.py edit CRPV-152289 --set customfield_30901=Platform_V_backlog --confirm-sberworks
# реальное изменение
python3 $SKILL_DIR/scripts/jira_tool.py edit CRPV-152289 --set customfield_30901=Platform_V_backlog --execute --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py edit CRPV-152289 --component Backend --component API --execute --confirm-sberworks
```

Тело: `{"fields": {<fieldId>: <value|[values]>, "components":[{"name":…}]}}`. JSON dry-run: `{command:"jira.edit", mode:"dry-run", key, request, note}`; после `--execute`: `{command, mode:"executed", key, httpStatus, applied, response}`.

## Wiki-разметка Jira DC

Jira Data Center использует **wiki-разметку**, а не HTML и не Markdown:

- `*жирный*`, `_курсив_`, `{{моноширинный}}`, `??цитата??`;
- заголовки `h1.`/`h2.`; списки `* пункт` / `# пункт`;
- таблицы: заголовок `||A||B||`, строки `|c1|c2|`;
- код: `{code:java}…{code}`; ссылка: `[текст|http://...]`.

Тело комментария и `description` подавать в wiki-разметке.

## Границы read/write

`get`, `search`, `similar`, `comments` — read-only. `comment`, `create` и `edit` — мутации, гейтированы `--execute`; без флага сетевой вызов мутации не выполняется.
