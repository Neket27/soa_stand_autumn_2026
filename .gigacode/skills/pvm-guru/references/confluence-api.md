# Confluence (sberworks) API справка

Скрипт `confluence_tool.py` работает с Confluence Data Center на `sberworks`. Базовый URL по умолчанию `https://sberworks.ru/wiki` (override — `CONFLUENCE_BASE_URL`). Авторизация — Bearer PAT (см. `references/backends-and-auth.md`). Скрипт печатает один JSON-объект в stdout, файлов не создаёт, секреты не печатает. Read-only по умолчанию; `update` мутирует только с `--execute`.

**Приоритет.** Confluence sberworks — крайний приоритет: вики по умолчанию — SWTR Wiki (`swtr_task_tool.py search`). Все команды `confluence_tool.py` требуют флаг `--confirm-sberworks`; без него скрипт отказывается работать (`{"ok": false, "gate": "sberworks-not-default"}` в stderr, exit 1) и не делает ни одного сетевого вызова. Ставить флаг только когда пользователь явно назвал Confluence/sberworks или дал ссылку `sberworks.ru` — см. «Приоритет систем» в `SKILL.md`.

## Endpoint-ы

- `GET /rest/api/content/{id}?expand=version,space,body.storage,ancestors` — получить страницу: тело в storage-формате (XHTML), номер версии, space, цепочку родителей.
- `GET /rest/api/content/search?cql=<cql>&limit=N&expand=space,version` — поиск контента по CQL.
- `GET /rest/api/space/{KEY}` и `GET /rest/api/space/{KEY}/content/page?limit=N` — карточка space и его страницы.
- `PUT /rest/api/content/{id}` — обновить страницу (write, только с `--execute`).

## get — получить страницу

Принимает числовой `pageId`, либо URL вида `https://sberworks.ru/wiki/pages/viewpage.action?pageId=1337065629` или `.../pages/1337065629/...` (id извлекается из `?pageId=` или из `/pages/{id}`).

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py get --id 1337065629 --confirm-sberworks
python3 $SKILL_DIR/scripts/confluence_tool.py get "https://sberworks.ru/wiki/pages/viewpage.action?pageId=1337065629" --confirm-sberworks
```

JSON-вывод: `{command:"confluence.get", id, title, type, space:{key,name,id}, version, ancestors:[{id,title}], body, url}`. Поле `body` — XHTML storage-формат страницы.

## search — поиск по CQL

Если передан только текст, скрипт строит `cql = 'text ~ "<текст>"'` (плюс `and space = "<KEY>"`, если задан `--space`). Можно передать готовый CQL через `--cql` (переопределяет текст).

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py search 'сайзинг CostCalculator' --space PVM --limit 25 --confirm-sberworks
python3 $SKILL_DIR/scripts/confluence_tool.py search --cql 'space = "PVM" and title ~ "мониторинг" order by lastmodified desc' --confirm-sberworks
```

JSON-вывод: `{command:"confluence.search", cql, size, results:[{id,title,type,space,version,url}]}`.

Шпаргалка CQL: `text ~ "фраза"` (полнотекст), `title ~ "..."`, `space = "KEY"`, `type = page`, `label = "..."`, `lastmodified >= "2026-01-01"`, `order by lastmodified desc`. Оператор `~` — нечёткое совпадение, `=` — точное.

## space — обзор пространства

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py space --key PVM --limit 25 --confirm-sberworks
```

JSON-вывод: `{command:"confluence.space", space:{key,name,id}, pages:[{id,title}]}`.

## update — обновить страницу (write, gated)

Запись выполняется только при явной просьбе пользователя и только с флагом `--execute`. Без флага — dry-run: скрипт читает текущую версию и печатает намерение, **без** PUT.

```bash
# dry-run: показать from/to версию, без мутации
python3 $SKILL_DIR/scripts/confluence_tool.py update --id 1337065629 --storage-file /tmp/body.xhtml --message 'уточнение метрик' --confirm-sberworks

# реальное обновление
python3 $SKILL_DIR/scripts/confluence_tool.py update --id 1337065629 --storage-file /tmp/body.xhtml --message 'уточнение метрик' --execute --confirm-sberworks
```

- `--storage-file` содержит **готовое XHTML-тело** в storage-формате (то, что лежит в `body.storage.value`). Скрипт не конвертирует markdown/HTML — тело должно быть валидным storage-XHTML.
- `--title` — новый заголовок (по умолчанию переиспользуется текущий из GET).
- `--message` — комментарий к версии.
- Тело PUT: `{version:{number: <current+1>, message}, title, type:"page", body:{storage:{value:<file>, representation:"storage"}}}`. Номер версии всегда `current + 1`.

JSON dry-run: `{command:"confluence.update", mode:"dry-run", id, fromVersion, toVersion, title, storageChars, note}`. JSON после `--execute`: `{command, mode:"executed", id, toVersion, httpStatus, response}`.

## Storage-формат и экранирование

Storage — это строгий XHTML (Confluence валидирует его на PUT). Практические правила:

- Голый амперсанд недопустим: `&` → `&amp;` (а также `&lt;`/`&gt;` для `<`/`>` в тексте).
- Макросы оформляются как `<ac:structured-macro …>`; ссылки — как `<ac:link>`; таблицы — обычные `<table>`.
- Безопаснее всего брать текущий `body` из `get`, править нужный фрагмент и подавать обратно через `--storage-file`, сохраняя структуру.

## Границы read/write

`get`, `search`, `space` — read-only. `update` — единственная мутация, гейтирована `--execute`. Без `--execute` ни один сетевой вызов мутации не выполняется (выполняется только GET текущей версии для расчёта `toVersion`).
