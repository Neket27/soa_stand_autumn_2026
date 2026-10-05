# CONTRIBUTING — pvm-guru (для разработчиков навыка)

Техническая документация навыка. Пользовательская часть (что умеет, настройка, troubleshooting) — в `README.md`. Сценарии, команды и «Фразы пользователя» — в `references/usage-scenarios.md`. Матрица авторизации — в `references/backends-and-auth.md`.

> **Чекаут и установка — разные пути.** Разработчик правит и тестирует `.gigacode/skills/pvm-guru/...` в чекауте — в этом файле все dev-команды идут через него. Пользователь запускает установленную копию, и где она лежит, зависит от установки: `~/.agents/skills/pvm-guru/` после `npx skills add -g`, каталог хоста — если ставили в хост. Поэтому **в поставляемых файлах путь к своим же скриптам не хардкодится**:
>
> - `SKILL.md`, `README.md`, `references/*.md` — пишут `$SKILL_DIR/scripts/...`; правило резолва живёт в разделе «Каталог навыка» в `SKILL.md`, и модель подставляет значение сама;
> - `extras/commands/*.md` — пишут `{{SKILL_DIR}}/scripts/...`; плейсхолдер подставляет `install.mjs` при раскладке команд.
>
> Литерал `.gigacode/skills/<name>/` в поставляемых файлах запрещён: он резолвится только внутри чекаута, у пользователя такого каталога нет, и сценарий падает на первом же шаге. Это проверяет smoke-тест «[paths] поставляемые файлы не хардкодят .gigacode/skills».

## Архитектура

- **`scripts/_swtr_common.py`** — общий слой для всех инструментов:
  - HTTP-клиент поверх `urllib` (retry на 429/502/503/504 с backoff, timeout);
  - `build_authorization()` — сборка заголовка `Authorization` по системе (`task_tracker` / `source_control` / `confluence` / `jira`), с изоляцией секрета по хосту;
  - `base_url_for(system)` — базовый URL системы;
  - `redact_secrets()` / `safe_headers_for_log()` — маскирование секретов в выводе и логах;
  - `emit_error()` — единый error-contract;
  - текстовые хелперы: `value_to_text`, `normalize_text`, `extract_unit_code`, `significant_terms` (стоп-слова параметром), регулярки `UNIT_CODE_RE` / `URL_RE`;
  - **профиль умолчаний — только чтение** (писателя здесь нет, файл пишет только `guru_profile.py`):
    - путь и схема: `PROFILE_REL`, `PROFILE_PATH_HINT`, `profile_path()`, `profile_schema()`, `normalize_profile()`, `validate_profile()`;
    - чтение: `load_profile()` → `LoadedProfile(status, path, data, version, reason, raw)` со статусами `ok` / `missing` / `corrupt` / `unsupported`; доступ — `profile_get()`;
    - порядок источников: `resolve_default()` (явное → окружение → профиль) и накопитель `DefaultsApplied` с `with_defaults()` / `profile_error_context()`;
    - обвязка argparse (единственное место слоя, которое знает про namespace, — иначе одна и та же правка повторялась бы в пяти `main()`): `attach_profile()` кладёт профиль и накопитель на namespace, `args_with_defaults()` / `args_error_context()` читают их обратно, `PROFILE_ERROR_HINT_GENERIC` — общий текст подсказки трёх CLI, `SC_PROJECT_DEFAULTS` + `apply_sc_project_defaults()` — tenant/проект обоих SourceControl-инструментов.
- **Инструменты** (CLI, stdlib-only, Python 3.7+, без внешних зависимостей): `fetch_swtr_sources.py`, `spec_sources.py`, `swtr_task_tool.py`, `swtr_unit_search.py`, `swtr_unit_comments.py`, `source_control_search.py`, `source_control_pr.py`, `confluence_tool.py`, `jira_tool.py`, `guru_doctor.py`, `guru_profile.py`, `local_context_search.py`.
- Каждый инструмент — тонкая CLI-обёртка: разбор аргументов → вызов через `_swtr_common` → печать JSON. Новую сетевую логику дублировать нельзя, всё через общий слой.

## Контракт вывода

- Все скрипты печатают результат в stdout как **один JSON-объект** (исключение — `guru_doctor.py`, он печатает человекочитаемую диагностику). В каталог проекта не пишет ни один из них; файлы на диске появляются ровно в трёх местах:
  - **отчёт о ревью** `docs/ai/<slug>/<YYYY-MM-DD>-review.md` — его пишет **агент** своими файловыми инструментами после анализа stdout, а не скрипт;
  - **профиль умолчаний** `~/.config/pvm-guru/profile.json` — его пишет `guru_profile.py` (`set`, `unset`, `reset`), единственный писатель профиля: запись атомарная (временный файл в том же каталоге + `os.replace`), права `0600`, резервной копии нет — профиль восстанавливается онбордингом;
  - **env-файл с кредами** `~/.swtr-review.env` — его пишет `guru_env_setup.py`, единственный писатель секретов.
- **Единый error-contract** (все CLI-инструменты, через `emit_error()` в `_swtr_common.py`): в stderr печатается `{"ok": false, "command": "<имя>", "error": "<redacted>", ...extra}` и процесс завершается с кодом 1. Секрет в тексте ошибки замаскирован. Существующие доп. поля (например `unitCode`) сохраняются.
- **`defaults_applied`** — часть контракта вывода тех пяти инструментов, которые умеют взять значение не только из своих флагов (`swtr_task_tool.py`, `source_control_search.py`, `source_control_pr.py`, `jira_tool.py`, `confluence_tool.py`): список записей `{key, value, source}`, где `source` — `profile`, `env`, `os_user` или `credential` (явно переданное в список не попадает вовсе, пустой список не печатается). Собирает его накопитель `DefaultsApplied`, в payload добавляет `with_defaults()`, в текст ошибки — `profile_error_context()` (те же записи плюс `hint`, когда часть значений пришла из профиля).

## JSON-поля по командам

| Команда | Ключевые поля вывода |
|---|---|
| `swtr_task_tool.py get <код>` | `{command, mode, code, summary, unit, wiki}` |
| `swtr_task_tool.py get <строка>` (lookup) | `{command, mode: "lookup", query, units, wiki}` |
| `swtr_task_tool.py search` | `{command, query, spaces, suits, attributeCodes, units, wiki}` + `notes`, когда область выборки взята из профиля |
| `swtr_task_tool.py count` | `{command, query, spaces, endpoint, request, matched, response}` + `notes`, когда область выборки взята из профиля (эндпойнт `POST /rest/api/unit/v3/count/composite`; тело — OR по `code/summary/description` с `like`) |
| `swtr_task_tool.py similar` | `{command, sourceCode, terms, items, rawItems, rawRuns}` + `notes`, когда область выборки взята из профиля |
| `swtr_task_tool.py assigned` | `{command, users, status, spaces, suits, endpoint, tql, request, notes, response}` |
| `swtr_task_tool.py done-weekly` | `{command, users, since, doneStatus, spaces, endpoint, tql, notes, response}` |
| `swtr_task_tool.py tms <kind>` | `{command, kind, endpoint, data}` (`data` = `{request, response}` или прямой ответ GET) |
| `swtr_task_tool.py create` | dry-run печатает request payload; `--execute` печатает request + response |
| `swtr_task_tool.py suits` | `{command, space, suits: [{code, name}], response}` (эндпойнт `GET /rest/api/space/v3/{space}/suits`; живой ответ имеет форму `{code, suits: [...]}` — парсер толерантен и к голому списку) |
| `swtr_task_tool.py link find` | `{command: "link.find", request, response}` (эндпойнт `POST /rest/api/unit/v1/link/find`; тело `{unitId, types?, direction?, page}` — схема `additionalProperties: false`) |
| `swtr_task_tool.py link create\|delete` | dry-run печатает request payload; `--execute` печатает request + response (эндпойнт `POST`/`DELETE /rest/api/unit/v1/link`, тело `{source, destination, type}`, `max_retries=0` — повтор записи создал бы дубль связи) |
| `fetch_swtr_sources.py` | `{ok, request, source}` (`source` для SWTR — `summary, description, attributes, unit, wiki, wikiTexts`; тексты приходят РАЗВЁРНУТЫМИ из документа ProseMirror — сырая разметка остаётся в `unit`/`wiki`, откуда берутся ссылки `attrs.href` (`swtr_unit_comments.collect_rich_hrefs`); для файла — `text, parsedJson`) |
| `spec_sources.py <код>` | `{ok, command: "spec-sources", request, root, source, comments, links, linksUnread, linked, external, skipped, notes, markdown}` — `source` повторяет форму `fetch_swtr_sources.py` (вывод годится как его `--source-file` для `openspec_seed.py`); `linked` — дочитанные связанные юниты, `external` — ссылки на sberworks и чужие хосты, которые скрипт НЕ читает: у каждой записи `{url, system, key, readCommand}`, где `readCommand` — готовая команда (у чужого хоста `null`); `skipped` — непрочитанное с причиной; `linksUnread` — признак «связи не прочитались», без него пустой `linked` неотличим от несостоявшегося чтения |
| `swtr_unit_comments.py <код>` | `{ok, unitCode, fetchedAt, request, response, normalized}` (JSON-документ редактора → читаемый текст; исходник в `rawCommentText`) |
| `source_control_search.py search` | `{command, tenant, project, repo, ref, queries, meta, matches, matchCount}` или `{command, needsRepository: true, candidates}` |
| `confluence_tool.py get` | `{command, id, title, type, space, version, ancestors, body, url}` (`body` — XHTML storage) |
| `jira_tool.py get` | нормализованный `{command, key, type, status, priority, summary, assignee, reporter, created, updated, labels, components, parent, subtasks, links, comments_total, description, rendered_description}` |
| `jira_tool.py` / `confluence_tool.py` без `--confirm-sberworks` | `{"ok": false, "command": "<jira\|confluence>.<cmd>", "error": <текст>, "gate": "sberworks-not-default", "useInstead": <команда swtr_task_tool>}` в stderr, exit 1. Гейт срабатывает до `load_env_file` и до любого сетевого вызова |
| `swtr_unit_search.py` | `{ok, endpoint, request, response}` |
| `local_context_search.py` | `{ok, paths, queries, totalMatches, results}` |
| `guru_profile.py show` | `{ok, command, path, status, version, updated_at, profile}` + `notes`; у статусов `corrupt` / `unsupported` — ещё `reason` одной строкой, у `corrupt` вдобавок `issues: [{key, reason}]` (ключи, которые задаёт человек) и `broken_stamp: [ключи]` (штамп, который семейство перепишет само); у файла с секретоподобными ключами — `secret_like_keys` |
| `guru_profile.py set\|unset` | то же плюс `changed: [ключи]` (только по ключам запроса); выброшенное при перезаписи — `dropped_invalid` и `removed_secret_like`, переписанный штамп — `restamped` |
| `guru_profile.py reset` | `{ok, command, path, removed, notes}` |
| `guru_profile.py suggest` | `{ok, command, login, spaces, primary_space, spaces_in_view, default_suit, source_control, sample, notes}` — единственная подкоманда профиля, которая ходит в сеть, и только на чтение |

Общий принцип: вывод содержит и сырой ответ API (`response` / `unit` / `wiki` / `data`), и нормализованные поля (`summary`, `notes`, `tql`).

## Важные опции

- `fetch_swtr_sources.py`: `--skip-wiki`, `--env-file`, `--timeout`, `--base-url`.
- `spec_sources.py`: `--depth` (глубина обхода, по умолчанию 1), `--max-linked` (предел чтения, по умолчанию 10; неудачная попытка тоже его тратит — иначе предел не ограничивает число запросов), `--link-type` (повторяемый), `--link-page-size`, `--no-links`, `--no-mentions`, `--with-comments`, `--max-chars` (предел текста источника в `markdown`; в JSON текст остаётся полным), `--skip-wiki`, `--env-file`, `--timeout`, `--base-url`.
- `swtr_unit_search.py`: `--body-file`, `--body-json`, `--filter`, `--count`, `--env-file`, `--timeout`.
- `swtr_unit_comments.py`: `--include-deleted`, `--page`, `--size`, `--env-file`, `--timeout`, `--base-url`.
- `local_context_search.py`: `--max-results`, `--context-lines`.
- `source_control_search.py`: `--no-archive`, `--max-files`, `--max-pages` (guard пагинации листинга директорий, по умолчанию 50; при усечении в `notes` вывода добавляется пометка), `--max-matches-per-file`, `--context`.
- `guru_doctor.py`: `--system`, `--base-url`, `--probe-path`, `--timeout` (по умолчанию 20 c), `--no-network`.
- `swtr_task_tool.py count`: `--extra-or` / `--extra-and` (JSON-массивы `QLPropertyFilter`), `--tql-filter-id` (повторяемый).
- `guru_profile.py`: `--home` — у файловых команд (`show`, `set`, `unset`, `reset`); `--env-file` и `--timeout` — только у `suggest`, единственной команды с сетью. Половинки не пересекаются: файловым командам env-файл не нужен.
- `--no-profile` — у `swtr_task_tool.py`, `source_control_search.py`, `source_control_pr.py`, `jira_tool.py` и `confluence_tool.py`: файл профиля не читается вовсе, и вызов ведёт себя как у пользователя без профиля. Значения, которые профиль закрывает (пространства, логин, тип/продукт/компоненты, tenant и проект SourceControl, проект Jira, пространство Confluence), тогда снова берутся только из флагов и окружения — вплоть до прежнего отказа argparse с кодом возврата 2.

**API-native дефолты пагинации** (сознательно НЕ унифицируются): `--page` = 0 у SWTR (0-based API) и = 1 у SourceControl (1-based API); `--limit` варьируется по эндпойнту. Значения зафиксированы в описаниях `--help`.

Для сложных или многострочных `QLRequestDto` используйте `--body-file`.

## Низкоуровневый QL-поиск

Прямой доступ к `POST /rest/api/unit/v3/find` (и `/count`) с ручным `QLRequestDto` — обход высокоуровневого `swtr_task_tool.py search`:

```bash
cat > /tmp/swtr-search-body.json <<'JSON'
{
  "filters": {"spaces": ["PVM"]},
  "page": {"page": 0, "size": 20}
}
JSON
python3 $SKILL_DIR/scripts/swtr_unit_search.py --body-file /tmp/swtr-search-body.json
```

Скрипт печатает `{ok, endpoint, request, response}`. Поддерживаются `--filter <fast-filter>` и `--count` (для `/rest/api/unit/v3/count`).

## Одно дерево: `.gigacode/`

Навык существует в одной копии — `.gigacode/skills/<name>/`. Она трекается в git, из неё же навык
исполняется в чекауте, и правки вносятся прямо в неё. Синхронизировать нечего: прежнее второе
дерево `.gigacode.example/` и скрипт `tools/sync-skill.sh`, который его зеркалил, удалены — они
расходились молча, и забытый прогон синхронизации давал дрейф, который замечали через недели.
Имя `.gigacode/` осталось за единственным деревом.

Автоматически собирается одно — зеркало команд и субагентов `.gigacode/{commands,agents}`. Оно нужно
для установки через `git clone` без Node: пути к скриптам в нём уже развёрнуты из `{{SKILL_DIR}}`.
Источник — `extras/{commands,agents}/` внутри каждого навыка, править надо только его:

```bash
node tools/build-example-extras.mjs
node tools/build-example-extras.mjs --check   # ждёт «зеркало актуально: N файлов»
```

Правка `extras/` без пересборки роняет `node --test tools/test-install-extras.mjs`.

## Как гонять тесты

У навыка два набора с разными задачами, и зелёными должны быть оба.

```bash
# 1. Юнит-тесты новой логики (как у остальных навыков семейства)
cd .gigacode/skills/pvm-guru && python3 -m unittest discover -s tests

# 2. Офлайн-контракт CLI-инструментов и инварианты семейства
python3 .gigacode/skills/pvm-guru/tests/smoke/run_smoke.py
python3 .gigacode/skills/pvm-guru/scripts/_test_swtr_env.py
```

- **`tests/test_*.py`** — юнит-тесты: разбор, форматирование, границы. Сеть в них не мокируется, а не трогается вовсе: тестируются чистые функции и подменённый сетевой слой.
- **`tests/smoke/run_smoke.py`** — контракт CLI: happy-path сабкоманд, dry-run, единый error-contract, маскирование секретов, инварианты семейства.

Смоук-сьют работает офлайн, stdlib-only, без сети: `urllib.request.urlopen` подменяется фикстурами `tests/smoke/fixtures/*.json` (анонимные ответы API — без реальных данных сотрудников и токенов).

- `run_smoke.py` проверяет: happy-path каждой сабкоманды (валидный JSON + ключевые поля); dry-run write-команд (`create` / `comment` / `update` без `--execute`) не делают сетевого вызова; единый error-contract на 401 и timeout для каждого из 8 инструментов; секрет из тела 401 замаскирован; `--help` каждого скрипта завершается кодом 0.
- Из чекаута `run_smoke.py` дополнительно проверяет **все навыки семейства рядом**, а не только `pvm-guru`: `description` в frontmatter не длиннее 500 символов (за этим порогом близкие навыки перестают различаться при роутинге — см. «Арбитраж триггеров» в `docs/guru-family.md`), `name` совпадает с именем каталога, в поставляемых файлах нет литерала `.gigacode/skills/` вне блока «Каталог навыка», а команды адресуют скрипты через `{{SKILL_DIR}}`. Реестр семейства — константа `FAMILY` в `scripts/guru_catalog.py`, и карта ролей `references/onboarding-roles.md` не имеет права называть навык, которого в реестре нет.
- `_test_swtr_env.py` проверяет разбор env-файла, схемы Basic/Bearer и их порядок по системам, `redact_secrets()` / `safe_headers_for_log()`, корректную передачу паролей со спецсимволами.

**Вывод скриптов — только то, что переживёт Windows.** Перенаправленный stdout там кодируется ANSI-страницей (`cp1251`), не UTF-8, поэтому `_swtr_common` на импорте зовёт `force_utf8_stdio()`: он один закрывает все скрипты каталога, и в новом скрипте про него нельзя забыть. Отдельно `guru_doctor.py` держится чистого ASCII — это проверяет `WindowsOutputTest.test_doctor_prints_only_ascii`. Диагностика запускается именно тогда, когда всё сломано, и не имеет права падать на собственном оформлении. Файлы всегда писать с явным `encoding="utf-8"`.

Оба прогона должны быть зелёными **перед** синхронизацией и коммитом.

## Как добавить сценарий

1. Реализовать логику в подходящем скрипте, сохранив JSON-контракт (`ok` / `command`, ошибки — через `emit_error()`).
2. Добавить сценарий в `references/usage-scenarios.md` (единый источник правды) с новым номером и блоком **«Фразы пользователя»**.
3. Отразить строку в таблице роутинга `SKILL.md` и в таблице «Что сказать агенту → что получите» в `README.md` — с той же нумерацией.
4. Добавить smoke-кейс и, при необходимости, фикстуру в `tests/smoke/`; логику разбора и форматирования покрыть юнит-тестом в `tests/test_<тема>.py`.
5. Если правились `extras/` — пересобрать зеркало `node tools/build-example-extras.mjs`; убедиться, что оба набора тестов зелёные.

## Как добавить бэкенд (систему)

1. В `_swtr_common.build_authorization()` — новая ветка `system=...`: порядок схем авторизации, семейство env-переменных, изоляция секрета по хосту (секрет одной системы никогда не уходит на хост другой).
2. Базовый URL — через `base_url_for()`; probe для диагностики — в `guru_doctor.py` (лёгкий read-эндпойнт, например с `limit=1`), с записью в probe-матрицу.
3. Задокументировать систему в `references/backends-and-auth.md` — **единственном** источнике auth-матрицы (не дублировать её в README / SKILL.md).
4. Новый CLI-инструмент строить поверх `_swtr_common` (HTTP, авторизация, редакция, `emit_error`) — без копирования сетевого кода.

## Правила секретов

- Секрет одной системы **никогда** не уходит на хост другой: `portal` и `sberworks` изолированы; SWTR-секреты не переиспользуются для SourceControl (только с явным `SOURCE_CONTROL_REUSE_SWTR_AUTH=1`). `Confluence-PAT ≠ Jira-PAT ≠ SWTR-секрет`.
- Никогда не печатать секрет: тексты ошибок — через `redact_secrets()`, заголовки в логах — через `safe_headers_for_log()`.
- Не хранить реальные токены в репозитории; `~/.swtr-review.env` не коммитить; фикстуры в `tests/smoke/fixtures/` — анонимные, без реальных данных сотрудников и токенов.
- **403 ≠ спецсимволы.** Пароли со спецсимволами (`} % # * …`) передаются корректно (покрыто `scripts/_test_swtr_env.py`), «экранирование» не решение никогда. Под 403 у портала живут три разных отказа, и путать их дорого: HTML-страница шлюза — устаревший пароль (`stale_password`); JSON от бэкенда на чтении — Basic запрещён, вот здесь PAT (`forbidden`); JSON от бэкенда на записи — не хватает прав в пространстве, и PAT не поможет (`no_write_permission`). Разбор — `references/backends-and-auth.md`.

## Контракт навыка

Раздел про написание **нового навыка семейства**, а не про `pvm-guru`. Он
фиксирует то, что по факту сложилось в S1–S12: писать его раньше значило бы
угадывать.

### Обязательные секции `SKILL.md`

| Секция | Зачем |
|---|---|
| **Назначение** | зачем навык нужен и чего он не делает — граница с соседом держится строкой, а не надеждой на описание |
| **Вход** | что принимает и что делает, если входа нет: спрашивает или отказывается |
| **Выход** | **конкретный путь артефакта** либо явное «файлов не создаёт». «Результат в ответе» не годится: следующий навык должен знать, где искать |
| **Каталог навыка (`$SKILL_DIR`)** | только если есть скрипты; перебор семи мест установки, путь абсолютный. У навыка без скриптов секции быть не должно |
| **Ворота и критерии готовности** | когда навык имеет право сказать «готово» и когда обязан остановиться |
| **Границы** | что не трогает ни при каких условиях |
| **Смежные навыки** | куда отправлять запрос, пришедший не по адресу |
| **Блок `metadata` во frontmatter** | `version`, `author`, `contributors` — кто написал навык и какая копия у пользователя на руках. Не топ-левел: `metadata` — санкционированный спекой Agent Skills свободный блок, а неизвестные ключи верхнего уровня хост вправе отвергнуть |

Скелет с пояснениями — `.gigacode/skills/example-skill/SKILL.md`.
Наличие секций проверяет `node tools/check-skill-contract.mjs`.

### Версия и авторство

```yaml
metadata:
  version: "1.4"
  author: Max Savinov <savinov.m.y@sbertech.ru>
  contributors: []
```

**Версия — счётчик для диагностики, а не контракт совместимости.** Она отвечает
на один вопрос: та ли копия навыка у пользователя, на которую он жалуется. Минор
двигается на любую содержательную правку, и думать над этим не надо. Мажор
двигается, только когда сломан контракт навыка: Вход, Выход, имя, формат
артефакта или набор триггеров. Патча нет намеренно — третий уровень требует
решать «баг это или улучшение» на каждой правке, а такой цены диагностика не
стоит.

**Автор заморожен, контрибьюторы только растут.** `author` вычисляется один раз
при заведении навыка и дальше не пересчитывается: `git log --follow` работает
для файла, но не для каталога, и первое же переименование обрежет историю —
автором стал бы тот, кто делал переезд. Контрибьюторы, наоборот, пересчитываются
из `git log` каждый прогон, но **только на добавление**: гейт проверяет «в файле
есть все, кого знает git», а не равенство. Усечённая история тогда даёт видимую
недостачу, а не молчаливое стирание чужой заслуги.

**У чужого навыка автор берётся из витрины `metadata.md`, а не из git.** Все
привозные навыки приехали одним импортным коммитом, и по git автором каждого
числится импортёр. Витрина главнее: она называет того, кто навык написал. Наша
роль в таком навыке — `maintainer`, а правки в форке идут в `contributors`.
Связь односторонняя: **автор течёт из витрины во frontmatter, никогда обратно.**
Витринную карточку чужого навыка мы не переписываем — она принадлежит автору.

Заполняет и сверяет всё это `node tools/check-skill-metadata.mjs`:

```bash
node tools/check-skill-metadata.mjs           # гейт: что не так
node tools/check-skill-metadata.mjs --write    # дописать авторство и контрибьюторов
```

Гейт сравнивает с `git merge-base HEAD main`. На ветке это ловит работу целиком;
на самой `main` вырождается в сравнение с `HEAD`, то есть видит только
незакоммиченное — гейт авторский, его место перед коммитом.

### Чек-лист автора навыка

Навык не считается готовым, пока не закрыты все девять пунктов:

1. **Строка в `docs/guru-family.md`** — вход, выход, формат, триггеры, когда использовать. Проверяет `node tools/check-family-matrix.mjs`.
2. **Запись в реестре `FAMILY_INFO`** (`pvm-guru/scripts/guru_catalog.py`) — назначение, вход, выход, роль-владелец. Без неё `/guru-start` о навыке не узнает. Запись добавляется, только когда каталог навыка реально существует: строка про несуществующий навык отправит человека ставить пустоту.
3. **Правило арбитража** — если триггеры пересекаются с чужими, слово разбирается в «Правиле 1», признак входа — в «Правиле 0». Проверяет `node tools/check-arbitration.mjs`.
4. **Тесты** — `tests/test_*.py`, прогон `python3 -m unittest discover -s tests`. Скрипт, который пишет на диск, обязан иметь тест «без `--execute` файл не изменён».
5. **`description` ≤ 500 символов** и **`name` совпадает с именем каталога** — оба проверяет смоук-сьют.
6. **Плейсхолдер в командах — только `{{SKILL_DIR}}`.** Больше `install.mjs` не подставляет ничего; любой другой уедет к пользователю литералом. Соседний навык адресуется как `{{SKILL_DIR}}/../<имя>`.
7. **Пересборка зеркала** — `node tools/build-example-extras.mjs`, затем `--check` в том же прогоне.
8. **Приёмка установленного снимка** — `node tools/check-installed-snapshot.mjs`. Раскладывает команды и субагентов во временный `HOME` тем же `install.mjs` и смотрит на то, что реально легло: неподставленные плейсхолдеры, относительные `references/…`, ссылки на `docs/` репозитория, литеральный `$SKILL_DIR`, битые `{{SKILL_DIR}}/…`. В чекауте всё это выглядит исправным — ломается только у установленного пользователя, поэтому проверять надо после раскладки, а не до.
9. **Версия сбамплена, авторство на месте** — `node tools/check-skill-metadata.mjs`. Правил навык — подвинь минор; гейт сверяет версию с `merge-base` и краснеет, если файл изменён, а число прежнее. Он же дописывает контрибьюторов из `git log` по `--write` и следит, чтобы автор во frontmatter не разошёлся с витриной `metadata.md`. Правила — в разделе «Версия и авторство» выше.

### Eval: проверить, что навык выбирается

`description` — это не документация, а сигнал выбора. Проверять его надо
поведением, а не чтением.

Заведите в `SKILL.md` раздел «Как проверить, что навык выбирается» с двумя
списками фраз:

- **должен сработать** — 3–5 формулировок, как их скажет живой человек, а не как написано в заголовке навыка;
- **не должен перехватывать** — 2–3 фразы соседнего навыка, с объяснением, почему они не его.

Второй список важнее первого. Навык, который не сработал, пользователь позовёт
руками; навык, который срабатывает лишний раз, шумит на **каждом** запросе и
портит выбор для всего набора.

Прогон простой: сказать агенту фразу из первого списка и посмотреть, какой навык
он взял; потом из второго. Расхождение чинится в `description` — уточнением
триггеров, — либо правилом арбитража в `docs/guru-family.md`, если конфликт
принципиальный.
