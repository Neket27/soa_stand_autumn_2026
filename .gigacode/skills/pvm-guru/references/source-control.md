# SourceControl операции и поиск кода

## Базовые endpoint-ы

SourceControl API V3 использует base path `/ssd/tools/sc/api/v3`. Основные endpoint-ы:

- `GET /repos/{tenant_UUID}/{project_name}` — список репозиториев проекта, поддерживает `page`, `limit`, `sort`, `q`, `topic`.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}` — получить репозиторий.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}/branches` — список веток, поддерживает `page`, `limit`, `q`, `sort`.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}/branches/{branch_name}` — получить ветку и commit.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}/contents/{filepath}` — получить метаданные директории или файла, поддерживает `ref`, `page`, `limit`, `sort`.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}/raw/{filepath}` — получить содержимое файла в Base64, поддерживает `ref`.
- `GET /repos/{tenant}/{project}/{repo}/archive?sha=...&format=zip` — скачать архив репозитория по SHA.
- `GET /repos/{tenant_UUID}/{project_name}/{repo_name}/commits` — список коммитов, поддерживает `since`, `until`, `path`, `page`, `limit`.

## Секреты и базовый URL

SourceControl-секреты читаются из `~/.swtr-review.env` или переменных окружения:

```bash
export SOURCE_CONTROL_AUTHORIZATION='Bearer eyJhbGciOi...'
export SOURCE_CONTROL_BASE_URL='https://portal.works.prod.sbt/ssd/tools/sc/api/v3'
```

Также поддерживаются `SC_AUTHORIZATION`, `SOURCE_CONTROL_BASIC_AUTH`, `SC_BASIC_AUTH`, `SOURCE_CONTROL_BEARER_TOKEN`, `SC_BEARER_TOKEN`, `SOURCE_CONTROL_TOKEN`, `SC_TOKEN`. SourceControl использует **только собственные** `SOURCE_CONTROL_*` / `SC_*` креды: если они не заданы, скрипт возвращает ошибку «not configured», а не подставляет SWTR-секреты. Переиспользование SWTR-кредов включается **явно** через `SOURCE_CONTROL_REUSE_SWTR_AUTH=1` (только при общем SSO — иначе секрет одной системы уходит на хост другой).

## Tenant, проект и профиль умолчаний

`--tenant` и `--project` обязательными аргументами больше не являются: оба резолвятся в порядке «явное → окружение (`SOURCE_CONTROL_TENANT`/`SC_TENANT`, `SOURCE_CONTROL_PROJECT`/`SC_PROJECT`) → профиль (`source_control.tenant`, `source_control.project`)». Источник значения приходит в поле `defaults_applied` записями `{key, value, source}`. Не дал ни один источник — прежний отказ словами самого argparse (`the following arguments are required: --tenant, --project`) и код возврата 2: пользователь без профиля разницы не замечает. То же самое у `source_control_pr.py`, где в этот же отказ попадает и `--repo` — он из профиля не подставляется никогда.

Репозитории профиля (`source_control.repos`) сами тоже не подставляются: выбор остаётся за человеком. Когда `--repo` не назван, список приходит отдельным полем `profileRepos` — и в выдаче `repos`, и у `search`, — а те из них, что встретились в самой выдаче, перечислены полем `profileRepoMatches`; предлагать их первыми. Ни того, ни другого поля нет, когда список в профиле пуст или чтение отключено.

`--no-profile` выключает чтение профиля на один вызов: файл не читается вовсе, и всё ведёт себя как у пользователя без профиля.

## Показать репозитории проекта

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --limit 50
```

Поиск репозитория по теме:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query cost --limit 20
```

## Показать ветки репозитория

```bash
python3 $SKILL_DIR/scripts/source_control_search.py branches --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --limit 30
```

Поиск ветки по части имени:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py branches --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --query release --limit 30
```

## Найти реализацию функции или фичи

Если проект, репозиторий и ветка известны:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --query 'CostCalculator' --query 'sizing' --query 'LGDB' --limit 30
```

Если нужно ограничить поиск папкой:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --path-prefix src/main --query 'CostCalculator' --limit 30
```

Скрипт сначала пытается найти SHA ветки и скачать zip-архив репозитория. Это быстрее и масштабнее, чем читать каждый файл через API. Если архив недоступен, скрипт переходит на fallback через `contents` и `raw`.

## Если репозиторий не указан

Сначала получить кандидатов:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query 'CostCalculator sizing' --repo-query 'cost calculator'
```

Скрипт напечатает в stdout JSON `{command: "search", needsRepository: true, tenant, project, queries, candidates}`, где `candidates` — сырой ответ API со списком репозиториев. Ничего на диск не пишется.

После этого нужно выбрать репозиторий и ветку. Если пользователь заранее разрешил выбрать автоматически и есть один очевидный кандидат, можно продолжать без дополнительного вопроса.

## Получить конкретный файл

```bash
python3 $SKILL_DIR/scripts/source_control_search.py file --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --filepath src/main/resources/application.yml
```

## Заземление без локального клона

Сабкоманда `grep` — тот же поиск, но вывод нормализован под потребителя, которому нужны координаты, а не ранжирование:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py grep \
  --tenant <tenant> --project <project> --repo <repo> --ref master \
  --query 'expiresAt' --evidence
```

Каждая находка — `{repo, ref, path, line, text, context, evidence}`, где `evidence` — готовая строка `путь:строка`. Флаг `--evidence` оставляет только находки с координатами: без них позицию невозможно проверить, а значит это и не находка.

**Когда это вместо `rg`.** Разведке кодовой базы (`guru-design`) и кросс-репо потоку нужны репозитории, которых нет на диске. Локальный `rg` там работать не может, семантический индекс может быть не подключён. Чтение архива репозитория по SHA через API снимает оба ограничения: клонировать ничего не нужно.

**Границы, которые проговариваются в ответе всегда.** Читается **архив одной ветки** на один SHA, с лимитом просмотренных файлов (`--max-files`). Достигнутый лимит попадает в `warnings` — значит, область просмотрена не полностью.

**Пустой результат не доказывает отсутствия.** Он доказывает отсутствие в просмотренной области: одна ветка, один репозиторий, в пределах лимитов. Команда пишет это в `warnings` сама, чтобы формулировка «такого в коде нет» не появилась там, где проверено одно из многого. Не удалось доказать ни наличие, ни отсутствие — так и говори, а не выбирай удобное.

## Как отвечать по найденному коду

В ответе пользователю разделять:

1. Что именно искали: фраза, функция, класс, API, метрика, таблица.
2. Где искали: project, repo, ref, path-prefix.
3. Что найдено: файлы, строки, краткая интерпретация.
4. Что не найдено: явное отсутствие совпадений в просмотренной области.
5. Ограничения: не тот репозиторий, не та ветка, архив недоступен, достигнут `--max-files`, не указаны tenant/project/repo.

Нельзя утверждать, что функции нет во всей кодовой базе, если был проверен только один репозиторий, одна ветка или ограниченный путь.
