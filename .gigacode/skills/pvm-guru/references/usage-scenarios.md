# Сценарии использования

Этот навык покрывает не только ревью постановок, но и быстрый поиск знаний по SWTR Task Tracker, SWTR Wiki, локальной документации, локальному коду, SourceControl, а также Confluence и Jira на хосте `sberworks`.

Нумерация сценариев (1-26 плюс 9a/9b/9c, 21a, 21b и 21c) едина со `SKILL.md`. В каждом сценарии есть блок **«Фразы пользователя»** — примеры естественных запросов для роутинга агентом.

> **Tenant в примерах.** Во всех SourceControl-примерах используется один и тот же tenant `22f434fb-0355-42a1-ac25-011074a20a2d` как плейсхолдер — **подставьте свой tenant** (UUID вашего пространства SourceControl).

> **Пространство по умолчанию — из профиля.** В трекерных выборках (§2, §3, §5, §11, §12) без `--space` область
> сужается до пространств профиля: сначала `task_tracker.primary_space`, затем `task_tracker.spaces`. Что именно
> взято, скрипт печатает структурно в `defaults_applied` и повторяет готовой строкой в `notes` — её и надо
> пересказать пользователю, а не сочинять свою. Профиль пуст или отключён — выборка идёт без ограничения по
> пространству, как до появления профиля. «Найди везде», «по всем пространствам», «без моих умолчаний» —
> это флаг `--no-profile`, и о том, что умолчания не применялись, надо сказать одной строкой. Правила целиком —
> раздел «Профиль пользователя и умолчания» в `SKILL.md`; сам профиль — §25.

## 1. Что за задача?

**Фразы пользователя:**
- «что за задача CRPV-35498»
- «покажи задачу PVM-53979»
- «дай карточку и описание по ссылке на SWTR»

Когда пользователь спрашивает «что за задача CRPV-35498», нужно получить карточку задачи, атрибуты и wiki-страницу.

**Голый код — это всегда Task Tracker.** Запрос «дай инфу по задаче PVM-52489» без
названия системы и без ссылки обрабатывается здесь, а не в Jira (§15): проекты `PVM`
и `CRPV` существуют в обеих системах, и код сам по себе систему не определяет.
Если юнит не найден — сказать «в Task Tracker не найдено», задать один вопрос
«посмотреть в Jira sberworks?» и дождаться ответа. См. «Приоритет систем» в `SKILL.md`.

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py get CRPV-35498
```

Скрипт печатает один JSON-объект в stdout с полями:

- `code` — распознанный код юнита;
- `summary` — нормализованная карточка (code, summary, description, space, suit, createdAt, updatedAt, attributes);
- `unit` — сырой ответ `/rest/api/unit/v2/{code}`;
- `wiki` — ответы wiki API v2/v1, если они доступны.

Если на входе только название, а не код, команда `get` делает lookup и возвращает кандидатов в JSON (`mode: "lookup"`):

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py get 'метрики PVM для сайзинга через CostCalculator' --space CRPV
```

Если найдено несколько задач и выбор влияет на вывод, нужно показать кандидатов и спросить, какую задачу анализировать.

## 2. Дай все задачи, где в тексте или атрибутах есть нужные слова

**Фразы пользователя:**
- «найди все задачи, где упоминается CostCalculator»
- «дай задачи и wiki про сайзинг LGDB»
- «поищи по ключевым словам в проекте CRPV»

Быстрый поиск по summary, description, code, wiki и выбранным атрибутам:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py search 'CostCalculator LGDB COTE сайзинг' --space CRPV --common-attributes --limit 20
```

JSON-вывод содержит поля `command, query, spaces, suits, attributeCodes, units, wiki`. `units` — ответ `POST /rest/api/unit/v3/find`, `wiki` — ответ wiki API поиска страниц.

Пространство не названо — область выборки берётся из профиля (врезка «Пространство по умолчанию» в начале файла): что именно взято, видно в `defaults_applied` и строкой в `notes`. «Найди везде» — это `--no-profile`.

Если известны коды атрибутов, лучше передать их явно:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py search 'сайзинг' --space CRPV --attribute-code solution --attribute-code component --attribute-code label --limit 50
```

## 3. Подсчитать число задач (count)

**Фразы пользователя:**
- «сколько задач упоминают Tarasenkova.O.O»
- «посчитай, сколько задач затрагивает CostCalculator»
- «дай только счетчик, без выгрузки задач»

Когда нужен только счетчик «сколько задач упоминают слово/код/имя пользователя», использовать `count` поверх `/rest/api/unit/v3/count/composite` без выгрузки тел задач:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py count "Tarasenkova.O.O" --space PVM
```

JSON-вывод: `{command, query, spaces, suits, endpoint, request, matched, response}`. Запрос строится с OR по `code/summary/description` (оператор `like`). Расширить фильтр можно через `--extra-or` и `--extra-and` (JSON-массивы `QLPropertyFilter`) и через `--tql-filter-id` (повторяемый, используется поле `tqlFilterIds` запроса).

Пространство не названо — область выборки берётся из профиля (врезка «Пространство по умолчанию» в начале файла): что именно взято, видно в `defaults_applied` и строкой в `notes`. «Найди везде» — это `--no-profile`.

## 4. Оцени документ по критериям качества

**Фразы пользователя:**
- «проведи ревью постановки CRPV-35498»
- «оцени качество этого PRD»
- «разбери постановку и скажи, готова ли она к разработке»

Вход может быть номером задачи, ссылкой, локальным файлом или строкой названия.

Для номера задачи:

```bash
python3 $SKILL_DIR/scripts/fetch_swtr_sources.py CRPV-35498
```

Для локального файла:

```bash
python3 $SKILL_DIR/scripts/fetch_swtr_sources.py docs/input/prd-costcalculator.md
```

`fetch_swtr_sources.py` печатает JSON `{ok, request, source}`. Поле `source` для SWTR содержит `summary, description, attributes, unit, wiki, wikiTexts`; для локального файла — `text` и `parsedJson`. После сбора источника агент проверяет документ по `review-checklist.md` и формирует отчет по `output-template.md`. Это единственное место, где навык создает файл на диске — финальный отчет сохраняет агент в `docs/ai/<slug>/<YYYY-MM-DD>-review.md`.

Если пользователь просит актуальность относительно кода, дополнительно выполнить локальный или SourceControl-поиск.

## 5. Найди похожие задачи

**Фразы пользователя:**
- «найди похожие задачи на CRPV-35498»
- «есть ли дубли этой постановки»
- «какие задачи близки по смыслу к тексту про сайзинг»

Для задачи:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py similar CRPV-35498 --space CRPV --limit 20
```

Для произвольного текста:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py similar 'CostCalculator сайзинг LGDB COTE метрики' --space CRPV --term CostCalculator --term сайзинг --term LGDB --term COTE
```

Скрипт извлекает значимые термины, выполняет несколько поисков, агрегирует выдачу и ранжирует задачи локально.

Пространство не названо — область выборки берётся из профиля (врезка «Пространство по умолчанию» в начале файла): что именно взято, видно в `defaults_applied` и строкой в `notes`. «Найди везде» — это `--no-profile`.

## 6. Расскажи, какие есть репозитории в проекте

**Фразы пользователя:**
- «какие есть репозитории в проекте pvm»
- «покажи список репозиториев SourceControl»
- «найди репозиторий про cost calculator»

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --limit 50
```

Для поиска репозитория по теме:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py repos --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query cost --limit 20
```

Если пользователь не указал tenant/project, спрашивать не сразу: скрипт подставит их сам — сначала из переменных окружения (`SOURCE_CONTROL_TENANT`/`SC_TENANT`, `SOURCE_CONTROL_PROJECT`/`SC_PROJECT`), затем из профиля (`source_control.tenant`, `source_control.project`), и назовёт источник в `defaults_applied`. Спрашивать — когда пусто и там: тогда скрипт сам отказывает словами argparse (`the following arguments are required: --tenant, --project`) и кодом возврата 2. Разово обойти профиль — `--no-profile`.

В выдаче `repos` репозитории из профиля (`source_control.repos`), которые встретились в листинге, перечислены полем `profileRepoMatches` — показывать их первыми, остальных кандидатов после. Поля нет вовсе, когда список в профиле пуст или чтение отключено `--no-profile`.

## 7. Расскажи, как реализована функция

**Фразы пользователя:**
- «как реализован CostCalculator в коде»
- «найди в SourceControl, где считается сайзинг»
- «покажи реализацию LGDB и COTE в репозитории»

Если репозиторий и ветка известны:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo abyss --ref master --query 'CostCalculator' --query 'sizing' --limit 30
```

Если репозиторий не указан, сначала показать кандидатов:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query 'CostCalculator sizing' --repo-query 'cost calculator'
```

После выдачи кандидатов первыми предложить репозитории из профиля — те из них, что попали в `candidates`, перечислены полем `profileRepoMatches`, так же как в выдаче `repos`, — и спросить, какой репозиторий и ветку смотреть, если нельзя надёжно выбрать автоматически. Сам по себе `--repo` из профиля не подставляется: выбор остаётся за человеком, а если в профиле пусто, вопрос задаётся как раньше.

## 8. Проанализируй wiki-документ и скажи, что по коду есть на эту тему

**Фразы пользователя:**
- «сверь эту wiki с кодом»
- «что из постановки уже реализовано в репозитории»
- «покажи расхождения между документом и кодом»

Последовательность:

1. Получить wiki/задачу:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py get CRPV-35498
```

2. Выделить из полей `summary`, `description`, `attributes`, `wiki`, `wikiTexts` JSON-вывода термины: компоненты, метрики, API, таблицы, события, интеграции, feature flags.
3. Проверить код:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo abyss --ref master --query 'LGDB' --query 'COTE' --query 'CostCalculator'
```

4. Ответить в структуре: «что заявлено в документе», «что найдено в коде», «что не найдено», «риски и вопросы».

## 9. Создай задачу в проекте X

**Фразы пользователя:**
- «создай задачу в проекте PVM про метрики сайзинга»
- «заведи таск на доработку CostCalculator»
- «подготовь черновик задачи, я потом подтвержу создание»

По умолчанию безопаснее сначала создать dry-run:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit task --space PVM --summary 'Добавить метрики сайзинга для CostCalculator' --description 'Нужно добавить метрики LGDB и COTE для снижения погрешности сайзинга.' --attribute priority=mid
```

Если пользователь явно просит создать задачу сейчас и все обязательные поля известны:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit task --space PVM --summary 'Добавить метрики сайзинга для CostCalculator' --description 'Нужно добавить метрики LGDB и COTE для снижения погрешности сайзинга.' --attribute priority=mid --execute
```

Ответ Task Tracker печатается в JSON-выводе под ключом `response`.

**Пропущенные поля берутся из профиля.** `--suit`, `--space`, `--product` и `--component` при пропуске подставляются из профиля (`task_tracker.default_suit`, `task_tracker.primary_space`, `task_tracker.product`, `task_tracker.components`); явно переданное значение всегда сильнее. Что подставлено — видно в `defaults_applied` и в dry-run payload: это write-путь, поэтому **назови взятые из профиля значения человеку до `--execute`**, а не после создания задачи. На ветке `--body-file` из профиля берётся только `--suit`: тип стоит в URL, а не в теле, поэтому в само тело не подставляется ничего — им распоряжается вызывающий. Подставленный тип называй так же, как остальные умолчания, либо передавай `--suit` явно. Отключить подстановку — `--no-profile`; тогда тип по умолчанию снова `task`, а пространство спрашивается, как раньше.

**Про `workflow_status` думать не нужно.** Suit с workflow требует этот атрибут и без него отвечает `HTTP 570`, поэтому скрипт подставляет `workflow_status={"command":"NEW"}` сам, когда атрибут не передан явно. Подстановка видна в dry-run payload — это и есть то, что уйдёт на сервер. Явно переданное значение не перетирается, а `--body-file` не трогается вовсе: там телом распоряжается вызывающий. Отключить подстановку — `--no-default-workflow-status`; тогда suit с workflow вернёт `HTTP 570`, и это ожидаемо, а не поломка. Матрица живых ответов API — в `references/task-tracker-operations.md`.

## 9a. Какие задачи связаны с этой

**Фразы пользователя:**
- «какие задачи связаны с CRPV-155159», «покажи связи задачи»
- «найди связанные тикеты», «что зависит от этой задачи»
- «свяжи задачи X и Y», «поставь связь реализована в / состоит из»

Чтение связей — read-only, безопасно:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py link find --unit CRPV-155159
```

В поле `links` каждая связь приходит расшифрованной: `relation` — формулировка от лица запрошенного юнита («depends on», «is part of»), `unit` и `unitSummary` — второй конец связи с заголовком. Сырой ответ API остаётся в `response`. Отвечая пользователю, показывай `relation` + `unit` + `unitSummary`, а не коды типов.

**В живых данных типы любые.** У реального юнита это `dependend`, `integration`, `partof` и другие из каталога — правило «только русские типы» относится к созданию связи, а не к чтению. Не ставь `--filter-type realized_in` «на всякий случай»: связи есть, а выдача окажется пустой.

Если справочник типов или заголовки не получены, команда всё равно отдаёт связи и пишет причину в `notes` — это не ошибка. Полностью сырая выдача — `--no-resolve`.

Создание и удаление связи — write-action, без `--execute` печатается только payload:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py link create --source CRPV-155159 --destination PVM-2 --type realized_in --execute
```

Создаём только русскими типами (`realized_in` = «реализована в», `decomposition` = «состоит из»). Направление — в `references/task-tracker-operations.md`.

## 9b. Типы задач пространства (suits)

**Фразы пользователя:**
- «какие типы задач (suits) в пространстве PVM»
- «какие бывают suit в этом space»

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py suits PVM
```

## 9c. Каталог типов связей

**Фразы пользователя:**
- «какие бывают типы связей»
- «что значит тип связи dependend»

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py link types
```

Отдаёт по каждому типу `code`, `name` и формулировки для обоих направлений (`sourceName` / `destinationName`). Нужен, когда надо объяснить незнакомый код типа или выбрать тип перед созданием связи.

## 10. Комментарии юнита

**Фразы пользователя:**
- «покажи комментарии по задаче PVM-119134»
- «какие открытые вопросы обсуждали в юните»
- «что писали в обсуждении по этой задаче»

Если пользователь просит посмотреть комментарии, обсуждение, открытые вопросы, уточнения или контекст согласования по задаче, использовать только безопасный read-only метод Task Tracker API через `scripts/swtr_unit_comments.py`:

```bash
python3 $SKILL_DIR/scripts/swtr_unit_comments.py PVM-119134
```

Скрипт вызывает `POST /rest/api/unit-comment/v1/find` с телом `{"unitCode": "PVM-119134", "page": {"page": 0, "size": 100}}` (поле `page` обязательно по контракту API) и печатает JSON `{ok, unitCode, fetchedAt, request, response, normalized}`. В `normalized.comments` лежит человекочитаемая выдача (текст комментария, автор, статус). Комментарии использовать как дополнительный источник: фиксировать открытые вопросы, уточнения и противоречия, но не считать комментарий заменой Description/wiki/атрибутов без явного подтверждения. Подробные правила — в `references/unit-comments.md`.

## 11. Покажи задачи, назначенные на меня / сотрудника X

**Фразы пользователя:**
- «какие задачи назначены на меня»
- «покажи незавершенные задачи vasiakuzyakin»
- «что назначено на команду в проекте PVM»

Вывод задач по `assigned_to` (тип `user`). На вход даётся **логин**, а фильтр на стороне SWTR
сравнивает `externalId` учётки с учётом регистра, поэтому скрипт сам сопоставляет одно с другим
(`savinov.m.y` → `Savinov.M.Y`) через справочник пользователей и показывает результат в поле
`assigneeValues`. Запрос всегда идёт на TQL-эндпоинт: атрибутный фильтр обычного `find` по
пользователю не находит ничего и молча отдаёт пустой список. Поддерживается фильтр по статусу.

Один сотрудник, все статусы:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py assigned --user vasiakuzyakin --space PVM --status all --limit 50
```

Только незавершенные:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py assigned --user vasiakuzyakin --space PVM --status in-progress
```

Группа сотрудников и явный workflow_status:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py assigned --user "vasiakuzyakin,maxisavinov" --space PVM --status open
```

«На меня»:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py assigned --me --space PVM --status all
```

Логин для `--me` разрешается в порядке `SWTR_LOGIN` → профиль (`login`) → `USER` → логин Basic-креда `SWTR_BASIC_AUTH`. Источник виден структурно в `defaults_applied` (`env`, `profile`, `os_user`, `credential`) и отдельной строкой в `notes` — её и пересказывать. Ни один источник не дал логина — команда отказывается и называет способы его задать. Обойти профиль — `--no-profile` или явный `--user LOGIN`.

Пространство не названо — область выборки берётся из профиля (врезка «Пространство по умолчанию» в начале файла): что именно взято, видно в `defaults_applied` и строкой в `notes`. «Найди везде» — это `--no-profile`.

JSON-вывод содержит `command, users, assigneeValues, status, spaces, suits, endpoint, tql, request, notes, response`. `response` — сырой ответ `find/tql` с найденными юнитами. Если пусто: сначала посмотреть
`assigneeValues` и `notes` — там видно, нашёлся ли логин в справочнике; затем повторить с `--status all`
и проверить значения `workflow_status`. Если логин не резолвится, передать `externalId` явно:
`--user Familia.I.O`.

**Известное ограничение фильтра по статусу.** `--status in-progress` перечисляет «done»-статусы
списком кодов, а коды уникальны для каждой схемы workflow: в пространстве с другой схемой фильтр
отработает не так. Переносимая форма — `workflow_status NOT IN statusCategory("done")`
(`references/tql.md`); функция проверена вживую 13.08.2026, команда переедет на неё на этапе 3.
Пока расхождение объясняется пользователю, а не выдаётся за точный результат.

## 12. Покажи задачи, выполненные сотрудником / группой за неделю

**Фразы пользователя:**
- «что закрыл vasiakuzyakin за неделю»
- «покажи, что сделала команда за последние 14 дней»
- «выполненные задачи с 30 апреля»

Окно по умолчанию 7 дней. Можно задать `--days` или явную дату `--since YYYY-MM-DD` — скрипт достроит её
до `YYYY-MM-DDT00:00:00Z`, потому что голую дату TQL отвергает как некорректный `date_time`.
Пользователь резолвится так же, как в сценарии 11: логин → `externalId`, результат в `assigneeValues`.

Один сотрудник, окно 7 дней:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py done-weekly --user vasiakuzyakin --space PVM
```

Группа сотрудников и явная дата:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py done-weekly --user vasiakuzyakin --user maxisavinov --space PVM --since 2026-04-30
```

«Я за 14 дней» с переопределением «done» статусов:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py done-weekly --me --space PVM --days 14 --done-status done --done-status closed
```

JSON-вывод содержит `command, users, since, doneStatus, spaces, endpoint, tql, notes, response`. В `notes` фиксируются период и явные ограничения (UNCONFIRMED для `workflow_status` и `updatedAt`); `response` — сырой ответ `find/tql`.

Логин для `--me` разрешается так же, как в сценарии 11: `SWTR_LOGIN` → профиль → `USER` → логин Basic-креда, источник — в `defaults_applied` и в `notes`. Пространство не названо — область выборки берётся из профиля (врезка «Пространство по умолчанию» в начале файла): что именно взято, видно в `defaults_applied` и строкой в `notes`. «Найди везде» — это `--no-profile`.

**Почему выборка шире, чем «выполнено», и почему это не лечится.** Признак завершения здесь —
`updatedAt >= since` вместе со списком «done»-статусов, поэтому в отчёт попадают задачи, закрытые
раньше окна, но отредактированные внутри него. Напрашивающееся решение — честная «Дата завершения»
`resolved_at` — **не работает**: 13.08.2026 проверено, что атрибут принимается синтаксически, но
пуст во всех проверенных пространствах (`references/tql.md`). Значит, ограничение остаётся, и его
надо проговаривать в ответе, а не выдавать отчёт за точный список закрытого за период.

Что действительно улучшится на этапе 3 — список «done»-статусов: он переедет на
`statusCategory("done")`, проверенную вживую.

## 13. Test Management System (TMS)

**Фразы пользователя:**
- «покажи тест-планы в пространстве TMS»
- «какие тест-кейсы в цикле TMS-27»
- «дай прогон тест-кейса TMS-9»
- «заведи тест-кейс на эту проверку», «создай ТК с шагами»

Сценарии плагина (`swtr_task_tool.py tms <kind>`) read-only. **Сам тест-кейс при этом создаётся и правится** — общими командами `create`/`update`, см. §13a. Подробное описание endpoint-ов — `references/test-management-system.md`.

Доступные пространства TMS:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms acceptable-spaces
```

Список тест-планов в пространстве:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms test-plans --space TMS --limit 20
```

Тест-планы, привязанные к юниту:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms test-plans --space TMS --linked-to TMS-5
```

Тест-циклы, привязанные к плану:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms plan-cycles --plan TMS-25
```

Тест-кейсы, привязанные к циклу:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms cycle-cases --cycle TMS-27
```

Иерархия папок (с прогрессом или без):

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms folder-root --space TMS --type TEST_CASE
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms folder-root-units --space TMS --type TEST_CASE --linked-to TMS-5
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms folder-hierarchy --folder TMS_test_case --with-progress
```

Тестовые данные и параметры кейса — а **шаги** лежат в атрибуте `test_step` и читаются обычным `get`:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms test-case --unit TMS-100
python3 $SKILL_DIR/scripts/swtr_task_tool.py get PVM-172631
```

Шаги прогона тест-кейса (read-only):

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms run-info --run TMS-9
```

Интеграционные юниты TMS:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms integration
```

JSON-вывод содержит `command, kind, endpoint, data`. `data` для большинства сценариев — `{request, response}`, для GET-эндпойнтов — сразу ответ API. Любые TMS write-action endpoint-ы (`/run/create`, `/run/{unit_id}/status`, `/safe_data/*`, `/files/upload`, `/files/delete`, `/folder/create|move|rename`, `/link`, `/link/delete`) скриптом не вызываются и требуют отдельного запроса пользователя.

## 13a. Создать или изменить тест-кейс (write)

**Фразы пользователя:**
- «создай тест-кейс», «заведи ТК на этот сценарий», «нарежь проверку в тест-кейс с шагами»
- «поправь шаги у PVM-177308», «допиши предусловие в кейс»

Тест-кейс — обычный юнит, поэтому команда обычная: `create --suit test_case`. Dry-run по умолчанию.

```bash
# что уйдёт в трекер — без записи
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit test_case --space PVM \
  --summary 'Вход по валидным кредам' \
  --attribute 'folder=R7.2/Источники/Теги' \
  --attribute type_of_testing=new --attribute test_type=ft_type \
  --precondition 'Есть учётка тестового пользователя' \
  --step 'Открыть страницу входа||Отображается форма логина' \
  --step 'Ввести креды и нажать «Войти»|user / pass|Пользователь авторизован'

# запись
… --execute

# заменить шаги у существующего кейса
python3 $SKILL_DIR/scripts/swtr_task_tool.py update PVM-177308 \
  --step 'Открыть страницу входа||Форма логина' --execute
```

Какие поля обязательны и какие коды они принимают — из живой схемы, а не из памяти:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py attributes test_case   # mandatoryCodes и values
python3 $SKILL_DIR/scripts/swtr_task_tool.py tms folder-root --space PVM --type TEST_CASE
```

**Что команда делает за пользователя.** Папка резолвится по имени или хвосту пути и превращается в
код (видно в `resolvedFields` ещё до записи); предусловие и части шагов уезжают строкой с документом
ProseMirror; шаги оборачиваются в `{"testStepList": […]}`. Эти три формы отличаются от того, в каком
виде поля ЧИТАЮТСЯ, и отдать прочитанное обратно — значит получить `HTTP 500` без имени поля либо
`200` с молча пустыми шагами. Таблица «что послали → что ответил трекер», границы и живые проверки —
`references/test-management-system.md`, раздел «Создание тест-кейса через API».

**Куда шаги не пишутся.** Шаги — атрибут `test_step`. Тело вики кейса (`wiki_page_body`, сценарий 24) — другой атрибут: таблица, записанная туда, шагами не станет, а пустое тело у кейса не значит, что шаги хранятся там. Просьба «добавь шаги» — всегда `--step`/`--steps-file`; отказ команды чинится вызовом, а не правкой скриптов навыка.

**Границы.** Дубли не проверяются; связь с требованием и добавление в тест-цикл ставятся отдельно
(`link create`); удаления юнита в API нет — ошибочный кейс переводится в
`--attribute test_case_status=removed`.

## 14. Confluence на sberworks

**Фразы пользователя:**
- «открой страницу Confluence по pageId 1337065629»
- «найди в wiki sberworks про мониторинг»
- «покажи страницу sberworks по этой ссылке»

Confluence на хосте `sberworks` — это второй вики (отдельный от SWTR Wiki), со своим PAT (Bearer). Базовый URL по умолчанию `https://sberworks.ru/wiki`.

Сценарий доступен только по явному признаку sberworks (пользователь назвал Confluence/sberworks либо дал ссылку `sberworks.ru`). Все команды требуют `--confirm-sberworks` — без флага скрипт откажется работать.

Открыть страницу по числовому `pageId` или по URL (id извлекается из `?pageId=` или `/pages/{id}`):

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py get --id 1337065629 --confirm-sberworks
python3 $SKILL_DIR/scripts/confluence_tool.py get "https://sberworks.ru/wiki/pages/viewpage.action?pageId=1337065629" --confirm-sberworks
```

JSON-вывод: `{command, id, title, type, space, version, ancestors, body, url}`, где `body` — XHTML storage-формат.

Поиск по тексту (строится `text ~ "..."`) или по готовому CQL:

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py search 'сайзинг CostCalculator' --space PVM --limit 25 --confirm-sberworks
python3 $SKILL_DIR/scripts/confluence_tool.py search --cql 'space = "PVM" and title ~ "мониторинг" order by lastmodified desc' --confirm-sberworks
```

Обновление страницы — write-action. Без `--execute` печатается намерение (dry-run, читается только текущая версия), с `--execute` выполняется `PUT` с версией `current+1`. `--storage-file` содержит готовое XHTML-тело (storage-формат; голый `&` → `&amp;`):

```bash
python3 $SKILL_DIR/scripts/confluence_tool.py update --id 1337065629 --storage-file /tmp/body.xhtml --message 'правка' --execute --confirm-sberworks
```

Подробно — `references/confluence-api.md`.

## 15. Jira на sberworks

**Фразы пользователя:**
- «что за задача Jira PVM-52489»
- «найди в Jira sberworks по тексту про мониторинг»
- «покажи комментарии задачи Jira PVM-52489»

Jira на `sberworks` — второй трекер (отдельный от SWTR Task Tracker), со своим PAT (отличается от Confluence-PAT). Базовый URL по умолчанию `https://sberworks.ru/jira`.

Сценарий доступен только по явному признаку sberworks (пользователь назвал Jira/sberworks либо дал ссылку `sberworks.ru`). Голый код `PVM-…`/`CRPV-…` таким признаком НЕ является — это §1, Task Tracker. Все команды требуют `--confirm-sberworks` — без флага скрипт откажется работать.

Карточка задачи, поиск, похожие, комментарии:

```bash
python3 $SKILL_DIR/scripts/jira_tool.py get PVM-52489 --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py search 'мониторинг бизнес-процессов' --project PVM --limit 30 --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py similar PVM-52489 --project PVM --limit 20 --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py comments PVM-52489 --confirm-sberworks
```

`get` возвращает нормализованный JSON `{command, key, type, status, priority, summary, assignee, reporter, created, updated, labels, components, parent, subtasks, links, comments_total, description, rendered_description}`. `similar` извлекает термины из `summary` и ранжирует по их пересечению (`score`); короткие аббревиатуры-омонимы шумят — сужать `--project`.

Комментарий, создание и редактирование задачи — write-action (dry-run без `--execute`, реальный POST/PUT с `--execute`). Тело комментария — wiki-разметка Jira DC (`*жирный*`, `||A||B||`), не HTML. `edit` меняет поля через `--set <id>=<value>` (скаляр), `--field <id>=<value>` (labels-поле, повторяемый) или `--component <name>` (замена набора компонентов):

```bash
python3 $SKILL_DIR/scripts/jira_tool.py comment PVM-52489 --body-file /tmp/comment.txt --execute --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py create --project PVM --type Task --summary 'Добавить метрики сайзинга' --description-file /tmp/desc.txt --execute --confirm-sberworks
python3 $SKILL_DIR/scripts/jira_tool.py edit CRPV-152289 --set customfield_30901=Platform_V_backlog --execute --confirm-sberworks
```

Подробно — `references/jira-api.md`.

## 16. Диагностика доступа (guru_doctor)

**Фразы пользователя:**
- «проверь доступ к SWTR / SourceControl / Confluence / Jira»
- «не работает авторизация, продиагностируй»
- «почему 403 при обращении к sberworks»

`guru_doctor.py` — безопасная самодиагностика по всем **четырём** системам. Секрет никогда не печатается: выводятся только «отпечаток» креда (длина и классы символов), выбранная схема авторизации и HTTP-статус живого probe. Каждая система проверяется на своём хосте своим лёгким endpoint-ом.

По умолчанию (без `--system`) проверяются все четыре системы за один запуск:

```bash
python3 $SKILL_DIR/scripts/guru_doctor.py               # все 4 системы
```

Отдельно — через `--system`:

```bash
python3 $SKILL_DIR/scripts/guru_doctor.py --system source_control
python3 $SKILL_DIR/scripts/guru_doctor.py --system confluence
python3 $SKILL_DIR/scripts/guru_doctor.py --system jira
```

Probe-эндпоинты по умолчанию (осмысленный `200` требует конкретного ресурса; каждый бьёт в API своей системы на своём хосте):

| `--system` | хост | probe-путь по умолчанию | вердикт |
|---|---|---|---|
| `task_tracker` | portal (`SWTR_BASE_URL`) | `GET /rest/api/unit/v2/{SWTR_PROBE_UNIT\|CRPV-35498}` | по коду `200` |
| `source_control` | portal (`SOURCE_CONTROL_BASE_URL`) | `GET /repos/{SOURCE_CONTROL_PROBE_TENANT\|22f434fb-…}/{SOURCE_CONTROL_PROBE_PROJECT\|pvm}?limit=1` | по коду `200` |
| `confluence` | sberworks (`CONFLUENCE_BASE_URL`) | `GET /rest/api/user/current` | по личности в теле (`type=known` + непустой `username`/`userKey`) |
| `jira` | sberworks (`JIRA_BASE_URL`) | `GET /rest/api/2/myself` | по наличию `name`/`key` в теле |

Опции: `--base-url` и `--probe-path` переопределяют цель probe; `--timeout` (по умолчанию 20 c) задаёт таймаут; `--no-network` пропускает живой запрос (проверяет только конфигурацию и «отпечаток» креда).

Последним печатается блок `profile state`: состояние профиля умолчаний (`present` / `missing` / `corrupt` / `unsupported`), путь к файлу, момент сохранения и заполненность логина и основного пространства. Сам логин не печатается. На строку `SUMMARY` и код возврата блок не влияет — это отдельный диагноз: без профиля скрипты работают как раньше. С `--no-network` он печатается (файл локальный), в режиме одной системы (`--system`) — нет.

Как читать вердикт:

- **200** — авторизация работает.
- **401** — креды неверны (опечатка в логине/пароле или PAT).
- **403** — кред корректно сформирован, но сервер его **запрещает**. Это НЕ про спецсимволы (доктор это доказывает round-trip-проверкой). У портала под этим кодом живут **три разных отказа**, и советовать PAT на первый же 403 нельзя: `stale_password` (HTML-страница шлюза — устаревший доменный пароль, самый частый случай), `forbidden` (JSON от бэкенда на чтении — вот здесь нужен PAT) и `no_write_permission` (JSON на записи — не хватает прав в пространстве, PAT не поможет). Доктор различает их сам; разбор — `references/backends-and-auth.md`. Для sberworks — проверить, что PAT именно этого приложения (Confluence ≠ Jira).
- **network/TLS error** — сервер недоступен; для корпоративного TLS-прокси временно задать `*_INSECURE_SKIP_TLS_VERIFY=1` и повторить.

Полная матрица бэкендов, хостов и переменных — `references/backends-and-auth.md`.

## 17. Онбординг нового пользователя (guru-start)

**Фразы пользователя:**
- «я только что поставил guru, с чего начать»
- «что мне даст guru как аналитику / разработчику / тестировщику»
- «покажи, что вообще умеет этот набор навыков»

Команда `/guru-start` ведёт разговор: спрашивает производственную роль, показывает
подобранную под неё карту возможностей и заканчивается прогнанным сценарием на
данных самого пользователя. Карта строится как пересечение
`references/onboarding-roles.md` и того, что реально установлено.

Инвентаризацию даёт `guru_catalog.py` — офлайн, без сети, один JSON в stdout:

```bash
python3 $SKILL_DIR/scripts/guru_catalog.py
```

| Поле | Что внутри |
|---|---|
| `hosts` | найденные хосты (OpenCode, Claude Code, Codex, GigaCode) и их каталоги |
| `skills` | установленные навыки: имя, описание из frontmatter, признак `family`, где найден |
| `commands` / `agents` | что разложил `install.mjs` (по его манифесту `~/.config/pvm-guru/extras-manifest.json`) |
| `missingCore` | навыки семейства, которых нет, с готовой командой установки |
| `notes` | что не удалось прочитать и почему |

Опции `--home`, `--skills-root`, `--manifest` переопределяют пути (нужны тестам).

Отдельный шаг разговора — **умолчания**. Роль команда сначала читает из уже сохранённого профиля
(`guru_profile.py show`, офлайн) и переспрашивает её списком, только когда там пусто. Дальше, с согласия
пользователя, идут вопросы по одному: логин, основное пространство, пространства в поле зрения, тип задачи
по умолчанию, проект и tenant SourceControl — но не больше четырёх за весь шаг, и не заданное в эту квоту
не переспрашивается. Варианты ответов даёт `guru_profile.py suggest` —
единственный сетевой вызов онбординга, только на чтение: список пространств и одна страница назначенных
задач, у каждой подсказки назван источник. Отказ трекера она отдаёт строками в `notes`, а не ошибкой:
тогда вопросы задаются свободным вводом, и шаг не прерывается.

```bash
python3 $SKILL_DIR/scripts/guru_profile.py show
python3 $SKILL_DIR/scripts/guru_profile.py suggest
python3 $SKILL_DIR/scripts/guru_profile.py set role=analyst login=familia.i.o task_tracker.primary_space=PVM
```

Сохранение — один `set` в конце шага, всеми ключами сразу, вместе с ролью. Отказ от шага принимается с первого
раза: профиль не создаётся, а способ настроить это позже (`/guru_profile`) называется в итоге. Что умеет сам
профиль и как его потом править — §25.

В онбординге запрещены записи в корпоративные системы (`create`, `link create`,
декомпозиция CRPV с `--execute`) и запуск `exec` — он делает коммиты. `plan` и
ревью документа допустимы с разрешения пользователя: они пишут только локальный файл.
**Единственная запись самого онбординга — тоже локальная и по согласию**: профиль умолчаний
`~/.config/pvm-guru/profile.json`, который пишет `guru_profile.py`.

## 18. Заготовка proposal из задачи трекера

**Фразы пользователя:**
- «сделай заготовку proposal по PVM-160042»
- «заведи change по этой задаче»
- «собери change-id и черновик из задачи»

```bash
python3 $SKILL_DIR/scripts/openspec_seed.py PVM-160042
python3 $SKILL_DIR/scripts/openspec_seed.py --source-file собранный-источник.json
```

Скрипт собирает источник теми же функциями, что и `fetch_swtr_sources.py` (описание и тело вики приходят текстом, а не документом ProseMirror), и раскладывает его поля по **нативному** скелету OpenSpec: `## Why`, `## What Changes`, `## Capabilities`, `## Impact`. Идентификатор изменения собирает соседний `guru-openspec changeid` — кириллица транслитерируется, код задачи остаётся в верхнем регистре: `PVM-160042-dobavit-srok-deystviya-scheta`.

**Заготовка — это не proposal.** Постановка в трекере почти никогда не содержит всех разделов, поэтому:

- раздел, которого в источнике нет, идёт **пустым** с меткой `[🔴 Взято с потолка (<40%)]` и попадает в `questions`;
- `## Capabilities` из задачи не выводится никогда — это всегда вопрос: список kebab-case имён задаёт человек, и каждое имя порождает `specs/<name>/spec.md`;
- 🟢 скрипт не ставит ни одному разделу: пересказ источника — не прочитанный код.

Правдоподобный текст на месте отсутствующих данных не пишется. Это ровно та болезнь автогенерации, против которой заведены метки уверенности: заполненный раздел выглядит готовым и до ревью не доживает как вопрос.

**Критерии приёмки в proposal не идут** — нативного раздела под них нет. Скрипт отдаёт их отдельным полем `acceptance_criteria`: их место — сценарии `#### Scenario:` в дельта-спеке, которую пишет `guru-brainstorm`.

Файл скрипт не создаёт: готовый черновик лежит в поле `markdown`, записывает его агент. Дальше — `guru-brainstorm` (требования и дельта), затем `guru-design`, `plan`, `exec`.

## 19. Создать Pull Request

**Фразы пользователя:**
- «создай PR», «открой пулл-реквест по этой ветке»
- «оформи PR со ссылкой на задачу»
- «какие PR сейчас открыты в репозитории»

```bash
python3 $SKILL_DIR/scripts/source_control_pr.py list --tenant <tenant> --project <project> --repo <repo>

python3 $SKILL_DIR/scripts/source_control_pr.py create \
  --tenant <tenant> --project <project> --repo <repo> \
  --head feature/PVM-160042-add-expiry --base master \
  --title 'Срок действия счёта в модели и выдаче' \
  --summary 'Счета получают срок жизни, просроченные уходят из выдачи.' \
  --change-id PVM-160042-add-expiry --task PVM-160042 \
  --task-url https://portal.works.prod.sbt/swtr/units/all/unit/PVM-160042 \
  --requirement REQ-001 --requirement REQ-002
```

**`--tenant` и `--project` можно не называть.** Оба резолвятся в порядке «явное → окружение (`SOURCE_CONTROL_TENANT`/`SC_TENANT`, `SOURCE_CONTROL_PROJECT`/`SC_PROJECT`) → профиль (`source_control.tenant`, `source_control.project`)», и источник виден в `defaults_applied`. Пусто везде — прежний отказ словами самого argparse (`the following arguments are required: --tenant, --project, --repo`) и код возврата 2: пользователь без профиля не заметит, что аргумент перестал быть обязательным. `--repo` из профиля не подставляется никогда. Так как это публикация наружу, **tenant и проект надо назвать человеку в dry-run** — включая случай, когда они пришли из профиля: PR не должен уехать в проект, о котором в запросе не было ни слова. Разово обойти профиль — `--no-profile`.

**Ветку сначала надо опубликовать.** `exec` работает только локально — `git push` у него запрещён конфигом. PR создаётся по ветке на сервере, и пока её там нет, API отвечает отказом «нет такой ветки». Между концом `exec` и этим сценарием стоит отдельный человеческий шаг:

```bash
git push -u origin feature/PVM-160042-add-expiry
```

Сам его не выполняй: публикация чужой работы делается с ведома человека. Скажи, что нужно сделать, и дождись подтверждения.

**Граница с `guru-pr-review`.** Создание PR — здесь. Ревью уже созданного PR — навык `guru-pr-review`: любая ссылка вида `…/pulls/<id>` во входе означает его, даже если сказано просто «ревью».

**Публикация наружу — самая опасная операция набора**, поэтому:

- **dry-run по умолчанию.** Без `--execute` печатается тело будущего запроса и ни одного сетевого вызова записи не делается. Это и есть способ проверить, что уйдёт наружу, до того как оно уйдёт.
- **Дубли проверяются до любой записи.** Открытый PR из той же исходной ветки — отказ со ссылкой на существующий: два PR из одной ветки означают расходящееся ревью, а не два намерения.
- **Совпадение веток** `--head` и `--base` отклоняется без сетевого вызова вообще.
- **Создающий запрос не повторяется сам.** Общий HTTP-helper по умолчанию ретраит три раза; для `POST`/`PUT`/`PATCH`/`DELETE` повторы выключены. Если сервер PR создал, а ответ не дошёл, повтор создал бы второй PR — а проверка дублей отрабатывает один раз, до первого запроса, и от этого не спасает.
- **После неоднозначного отказа идёт сверка.** Команда не гадает, а перечитывает список открытых PR: PR появился — так и говорит («повторять не нужно», со ссылкой), не появился — говорит, что можно повторить.

**Ревьюеры.** Берутся из `SOURCE_CONTROL_DEFAULT_REVIEWERS` в `~/.swtr-review.env` (список через запятую), явный `--reviewer` их перекрывает. Переменной нет — никто не подставляется, и команда об этом говорит: назначить ревьюеров вручную дешевле, чем разбираться, почему ревью ушло не туда. Назначение идёт **отдельным запросом** после создания: API не принимает ревьюеров в теле PR. Если этот запрос упал, PR остаётся созданным, а в предупреждениях появляется, что ревьюеров надо назначить руками.

**Описание PR** собирается по шаблону: суть одним абзацем, `change-id`, ссылка на задачу, закрытые требования, связанные PR других репозиториев. Длинное автоописание никто не читает — в шаблон идёт только то, чего ревьюер не увидит из diff. Формулировки — по навыку `guru-writing-style`, без канцелярита.

## 20. Перевести задачу в другой статус

**Фразы пользователя:**
- «переведи задачу в resolved», «закрой задачу», «верни в работу»
- «какой сейчас статус и куда из него можно уйти»
- «почему смена статуса даёт 500 / 570»

```bash
# состояние автомата: текущий статус и что доступно прямо сейчас
python3 $SKILL_DIR/scripts/swtr_task_tool.py status get STS-452231

# маршрут до целевого статуса; без --execute ничего не отправляется
python3 $SKILL_DIR/scripts/swtr_task_tool.py status set STS-452231 --to RESOLVED

# пройти маршрут
python3 $SKILL_DIR/scripts/swtr_task_tool.py status set STS-452231 --to RESOLVED --execute

# ровно один переход по коду или имени действия (аварийный люк)
python3 $SKILL_DIR/scripts/swtr_task_tool.py status set STS-452231 --action 'Закрыть' --execute
```

**Статус нельзя присвоить.** Это проекция конечного автомата: меняется только выполнением действия, доступного из текущего статуса. Попытка записать `workflow_status` значением уходит в обновление юнита и получает `HTTP 500 «Что-то пошло не так»` — ошибка, по которой причину не видно. Полный разбор ручек, коды отказов и живой пример графа — в `references/task-tracker-operations.md`, раздел «Смена статуса юнита».

**Цель редко достигается одним переходом.** `status set` строит кратчайший маршрут по графу схемы и печатает его целиком (`plan`, `hops`): из `CANCELLED` в `RESOLVED` в space `STS` — три перехода. Дальше `STATUS_MAX_HOPS` (6) маршрут не ищется: цель в семь переходов почти всегда выбрана неверно.

**Что делает команда, чего не сделает ручной запрос:**

- **Коды действий резолвятся в рантайме.** Они генерируются для каждой схемы workflow, поэтому в другом space те же по смыслу действия имеют другие коды. Хардкод здесь ломается при первом же переходе в соседнем пространстве.
- **Состояние перечитывается перед каждым шагом.** Действие могло стать недоступным, пока шёл маршрут. Проверка локальная, поэтому вместо `570` от трекера приходит внятная ошибка со списком доступного.
- **`actionsExcludedByCondition` отделено от «действия нет».** Закрытое условием действие есть в схеме, но требует прав или заполненных полей — и заполнять их надо ДО смены статуса.
- **Повторов на запись нет.** Переход не идемпотентен: повтор увёл бы юнит на статус дальше по графу.
- **Идемпотентность цели.** Юнит уже в целевом статусе — команда говорит это и ничего не отправляет.

**Порядок с заполнением полей.** Поля резолюции (`Resolution_appsec`, `triage_result` и прочие, которых требует переход) заполнять до `status set`, отдельным обновлением юнита. Условие перехода проверяется в момент перехода.

## 21. Составь TQL-запрос

**Фразы пользователя:**
- «напиши TQL», «как это выразить в TQL», «дай запрос для фильтра»
- «почему мой TQL находит не то»
- «покажи задачи, где ... » — если условие не выражается обычным `find`

Синтаксис целиком — `references/tql.md`. Порядок работы:

1. Выбрать инструмент: простые условия (пространство, тип, значение атрибута) — обычный `find`;
   атрибуты типа `user`, диапазоны дат, `NOT IN`, категории статусов, обход связей — TQL.
2. Собрать запрос **с одним типом связки**: только AND либо только OR. Скобки движок отвергает
   явно (`HTTP 400`), а вот смешение AND и OR без скобок проходит с `200` и неверной выдачей —
   это и есть главная причина «находит не то».
   Перечисление вариантов по одному полю выражать через `IN`, а не через OR.
3. Сверить операторы по таблице атрибутов: текстовые поля не принимают `=`, даты не принимают `IN`,
   `version` и `folder` не комбинируются с `AND`/`OR`.
4. Статусы задавать категорией — `statusCategory("done")`, — а не списком кодов: коды уникальны
   для каждой схемы workflow и в соседнем пространстве не сработают.
5. Выполнить и **показать использованный TQL пользователю**: он воспроизводим и его можно
   поправить.

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py tql 'space = "PVM" AND workflow_status not in statusCategory("done")' --limit 20

# нужные атрибуты в выдаче
python3 $SKILL_DIR/scripts/swtr_task_tool.py tql 'assigned_to = currentUser()' --return-attribute workflow_status --return-attribute priority
```

**Команда проверяет форму запроса до отправки.** Смешение AND и OR и скобки логической группы
отклоняются локально, с кодом возврата 1 и объяснением в `error`; сеть при этом не трогается.
Так и задумано: скобки бэкенд отверг бы сам, а вот смешение он принимает и молча возвращает
неверную выборку — показать её пользователю хуже, чем отказать. Аварийный люк — `--force`:
валидатор тоже может ошибиться, и запрет должен обходиться явно.

JSON-вывод: `{command, endpoint, tql, warnings, request, response}`. `warnings` непусто только
при `--force`.

⚠️ **`swtr_unit_search.py --body-file` для TQL не годится и молча обманет.** Скрипт отправляет тело
в `POST /rest/api/unit/v3/find`, а не в `/find/tql`; ключ `query` этот endpoint не понимает и
**игнорирует без ошибки** — вернётся правдоподобная, но нефильтрованная выдача. Проверено вживую
13.08.2026. Использовать его только с настоящим `QLRequestDto` (`filters`, `attributes`, `page`).

Функции языка проверены на живом стенде 13.08.2026 и работают: `statusCategory`, `linkedUnitsOf`,
`linkedUnitsOfRecursive`, `currentUser`, `now`/`startOf…`, `hasWorkLogs`, `hasComments`,
`hasCommentCount`. Отдельной сабкоманды `tql` с валидатором пока нет — она появится на этапе 3.

Две ошибки, которые дают именно функции, и обе лечатся сужением, а не повтором:

- `слишком объемный набор данных` у `hasWorkLogs` / `hasComments` — аргумент слишком широкий;
  внешний `AND` не помогает, сужать надо внутри скобок функции;
- пустая выдача у `linkedUnitsOf` — почти всегда перепутан конец связи: имя берётся со стороны
  искомого юнита («Является частью» — дети, «Состоит из» — родители).

## 21a. Разведка: пространства, фильтры, версии, схема процесса

**Фразы пользователя:**
- «какие вообще есть пространства», «найди пространство про мониторинг»
- «какие у меня сохранённые фильтры»
- «какие версии заведены в CRPV»
- «какие статусы и переходы у эпика в этом пространстве»

Четыре read-only команды, отвечающие на вопрос «что вообще есть», до того как строить фильтр:

```bash
# все доступные пространства; необязательный аргумент — подстрока по коду и названию
python3 $SKILL_DIR/scripts/swtr_task_tool.py spaces
python3 $SKILL_DIR/scripts/swtr_task_tool.py spaces monitor

# сохранённые фильтры текущего пользователя (искать внутри фильтра — swtr_unit_search.py --filter)
python3 $SKILL_DIR/scripts/swtr_task_tool.py filters

# версии пространства; архивные скрыты, вернуть их — --include-archived
python3 $SKILL_DIR/scripts/swtr_task_tool.py versions CRPV --limit 50

# схема процесса по паре «тип задачи + пространство», БЕЗ существующего юнита
python3 $SKILL_DIR/scripts/swtr_task_tool.py workflow --suit epic_crpv --space CRPV
```

`workflow` отвечает на вопрос, который раньше требовал живого юнита: какие статусы бывают у этого
типа задач в этом пространстве и какими действиями они связаны. В выдаче `statuses` — список
`{code, name, category}` (категория `pause`/`progress`/`done`), `transitions` —
`{action, actionName, from, to}`; у перехода создания `from` пуст, и это норма. Коды действий
уникальны для схемы — переносить их в другое пространство нельзя (`references/workflow-and-approvals.md`).

Состояние **конкретного** юнита и маршрут перехода — это по-прежнему `status get` / `status set`
(§20): `workflow` показывает карту, а не положение на ней.

## 21b. Заполнить версии, компонент и код продукта при создании

**Фразы пользователя:**
- «создай задачу с Fix Version 8.2.70 и компонентом VLOG»
- «какие вообще есть версии в этом проекте», «какие компоненты доступны»
- «какой код атрибута у Fix Version для эпика»

Поля задаются **именами** — коды справочника и код атрибута навык подставляет сам:

```bash
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit task --space PVM \
  --summary 'Название задачи' \
  --fix-version 'Platform V Monitor 8.2.70' \
  --affects-version 'Platform V Monitor 6.3.120' \
  --component VLOG --product OPM
```

Без `--execute` это dry-run: в выводе видно и готовое тело запроса, и поле `resolvedFields` —
во что превратилось каждое имя. Проверять подстановку до записи, а не после.

**Три вещи, которые команда делает за пользователя.**

1. **Код атрибута зависит от типа задачи**: «Fix Version» у `task` и `story` — это `fix_version`,
   у эпика CRPV — `fix_version_s`. Код выбирается из реальной схемы типа, а не угадывается.
2. **Форма значения зависит от типа поля**: код продукта уходит голой строкой, версии и компоненты —
   списком объектов `[{"code": …}]`. Список голых строк те же поля отвергают ошибкой сервера.
3. **Справочники привязаны к пространству**: одно имя в разных пространствах имеет разные коды,
   поэтому резолв всегда идёт в справочнике целевого space.

Неизвестное имя — отказ **до** записи, с подсказкой похожих значений. Неоднозначное имя команда не
разрешает молча: перечисляет кандидатов и просит передать код.

Посмотреть, что вообще доступно:

```bash
# атрибуты типа задачи; namedFields сразу показывает коды под человеческие имена полей
python3 $SKILL_DIR/scripts/swtr_task_tool.py attributes task --query version

# справочники пространства
python3 $SKILL_DIR/scripts/swtr_task_tool.py directory --space PVM --kind versions --query 8.2
python3 $SKILL_DIR/scripts/swtr_task_tool.py directory --space PVM --kind components
python3 $SKILL_DIR/scripts/swtr_task_tool.py directory --space PVM --kind product --suit task
```

**Изменить эти поля у уже созданной задачи навык пока не может** — команды нет, и подходящая ручка
API не найдена. Поля заполняются в момент создания; если задача уже есть, правка идёт руками в UI.

## 21c. Изменить поля уже созданной задачи

**Фразы пользователя:**
- «проставь Fix Version у PVM-175440», «поменяй компонент в задаче»
- «заполни версии у всех задач релиза»

```bash
# сначала дифф: что было и что станет, PATCH не уходит
python3 $SKILL_DIR/scripts/swtr_task_tool.py update PVM-175440 \
  --fix-version 'Platform V Monitor 8.2.70' --component VLOG --product OPM

# запись
python3 $SKILL_DIR/scripts/swtr_task_tool.py update PVM-175440 \
  --fix-version 'Platform V Monitor 8.2.70' --component VLOG --product OPM --execute
```

Endpoint — `PATCH /rest/api/unit/v2/update/{code}`, тело `{"attributes": {…}}`. Форма пути
неочевидна: **глагол стоит перед кодом**, привычный `/unit/v2/{code}/update` отвечает 404.

Тип задачи и пространство команда берёт из самого юнита — от них зависят и код атрибута, и
справочник значений. Те же флаги, что у создания (§21b), и тот же резолв имён.

**Что команда делает, чтобы правка не вышла боком.**

- **Дифф до записи.** Без `--execute` печатается «было → станет» по каждому полю. Мультизначное
  поле **перезаписывается целиком**: прежние значения не дополняются, а заменяются, и увидеть это
  надо до отправки.
- **Статус этим путём не меняется.** `--attribute workflow_status=…` отклоняется со ссылкой на
  `status set` (§20): статус — проекция автомата, запись значением даёт `HTTP 500` без объяснения.
- **Запись не повторяется** при таймауте: повтор перетёр бы поле второй раз.
- **Обратное чтение** после записи: в `readBack` видно, что реально лежит в задаче.
- Неизвестное значение справочника — отказ до записи, с подсказкой похожих или образцом доступных.

Название и описание меняются флагами `--summary` и `--description`: это базовые поля, они уходят
на верхнем уровне тела, а не в `attributes`.

Формы значений по типам полей и две ловушки патча (невалидный код таксономии проходит с `200` и не
сохраняется; патч валидирует обязательные поля всего юнита) — `references/task-tracker-operations.md`.

## 22. Дай ссылку на создание задачи с предзаполнением

**Фразы пользователя:**
- «дай ссылку, я сам создам», «подготовь форму создания»
- «создай задачу» — когда прав на запись нет или создание не согласовано

Вместо `create` собрать ссылку на форму создания с заполненными полями: пространство, тип задачи,
заголовок обязательны; описание, метки и любой атрибут по коду — опционально. Формат параметров и
кодирование пробелов и переносов — `references/task-tracker-operations.md`, раздел «Создание
задачи». Запись выполняет пользователь, навык не отправляет ничего.

Это же обходной путь, когда `create` упал на обязательных полях пространства: форма покажет их
сама.

## 23. Выгрузи страницу вики в PDF

**Фразы пользователя:**
- «сохрани страницу в PDF», «выгрузи вики в pdf», «нужен PDF документа»

Сервис печати отдельный от Task Tracker: `POST /ssd/tools/wikipdf/print-page-as-pdf`, Basic-авторизация,
в теле — ссылка на страницу, учётные данные и параметры печати. Детали — `references/wiki-model.md`.

Команды навыка нет, и подставлять чужие креды нельзя: агент отдаёт пользователю готовую команду с
плейсхолдерами логина и пароля, а выполняет её пользователь. UNCONFIRMED: на нашем стенде не
проверялось.

## 24. Создать или изменить страницу вики

**Фразы пользователя:**
- «заведи страницу вики», «создай дочернюю страницу», «выложи это в вики»
- «обнови текст страницы», «замени содержимое PVM-…», «перенеси отчёт в вики»

Страница пишется из markdown-файла: конвертер собирает документ редактора (заголовки, списки,
чек-листы, таблицы, блоки кода, жирный/курсив/зачёркнутый/моноширинный, ссылки).

```bash
# создать дочернюю страницу с текстом
python3 $SKILL_DIR/scripts/swtr_task_tool.py create --suit wiki_page --space PVM \
  --summary 'Название страницы' --wiki-body отчёт.md --parent PVM-175549 --execute

# заменить текст существующей страницы (без --execute печатается дифф)
python3 $SKILL_DIR/scripts/swtr_task_tool.py update PVM-175549 --wiki-body отчёт.md --execute

# текст из потока: удобно, когда файл создавать незачем
printf '# Заголовок\n\nтекст\n' | python3 $SKILL_DIR/scripts/swtr_task_tool.py \
  create --suit wiki_page --space PVM --summary 'Название' --wiki-body - --execute
```

**Три вещи, на которых это ломается** (проверено на живом трекере 14.08.2026):

- **Тело — строка, а не объект.** `wiki_page_body` имеет тип `wiki_page_wisiwig`, и трекер ждёт
  СТРОКУ с JSON документа внутри. Объект даёт `HTTP 500`, голый текст без разметки — тоже.
  Флаг `--wiki-body` и сырой `--attribute wiki_page_body=…` приводятся к строке автоматически.
- **Родитель задаётся отдельно.** В теле создания поля «родитель» нет: дерево держится на связи
  `wiki_page_hierarchy`, где **источник — родитель**, а назначение — новая страница. Без `--parent`
  страница создаётся, но в дереве вики её не видно; команда предупреждает об этом в выводе.
- **Правка перетирает страницу целиком** и создаёт новую версию в истории. Побочные эффекты
  сохранения (сброс согласования, «Актуализировать тест-кейсы») — `references/wiki-model.md`.

Уже собранный документ ProseMirror (виджеты задач, картинки, макросы) проходит насквозь: если файл
содержит JSON с `"type": "doc"`, конвертер его не трогает.

## 25. Профиль умолчаний: показать, изменить, сбросить

**Фразы пользователя:**
- «покажи мой профиль», «какие у меня умолчания», «что guru про меня помнит»
- «сделай основным пространством CRPV», «запомни мой логин», «запомни, что я работаю в PVM и CRPV»
- «убери продукт из профиля», «сбрось профиль», «забудь мои умолчания»

Профиль — локальный файл `~/.config/pvm-guru/profile.json`: логин, роль, пространства трекера,
тип задачи, продукт и компоненты, tenant/проект/репозитории SourceControl, ключ проекта Jira
и пространства Confluence. Из него скрипты навыка берут значения, когда пользователь не назвал их
явно; правила применения — раздел «Профиль пользователя и умолчания» в `SKILL.md`. Разговорная
обёртка — команда `/guru_profile`; первый сбор идёт шагом умолчаний в `/guru-start`.

```bash
# показать карту (офлайн, в сеть не ходит)
python3 $SKILL_DIR/scripts/guru_profile.py show

# изменить один или несколько ключей за один запуск; списки — через запятую
python3 $SKILL_DIR/scripts/guru_profile.py set task_tracker.primary_space=CRPV task_tracker.spaces=PVM,CRPV

# убрать ключ (то же делает пустое значение в set: task_tracker.product=)
python3 $SKILL_DIR/scripts/guru_profile.py unset task_tracker.product

# удалить файл профиля целиком — только после явного «да» пользователя
python3 $SKILL_DIR/scripts/guru_profile.py reset

# подсказки из данных самого пользователя: единственная подкоманда с сетью, только на чтение
python3 $SKILL_DIR/scripts/guru_profile.py suggest
```

Ключи профиля (точечные имена — то, что понимает скрипт): `login`, `role`,
`task_tracker.primary_space`, `task_tracker.spaces`, `task_tracker.default_suit`,
`task_tracker.product`, `task_tracker.components`, `source_control.tenant`,
`source_control.project`, `source_control.repos`, `sberworks.jira_project`,
`sberworks.confluence_space`. Значения не выдумывать и по смыслу не «нормализовать»:
в профиль идёт то, что назвал человек или что вернул `suggest` с указанием происхождения.

Как читать `show`: поле `status` — `ok` (профиль есть), `missing` (файла нет), `corrupt`
(файл не проходит проверку, причина в `reason`), `unsupported` (версия схемы новее установленного
навыка). Все три не-`ok` статуса означают для работы одно: вести себя как без профиля. Но
`corrupt` не запирает правку: когда в `show` пришло поле `issues`, файл разобран и негодны только
названные ключи — `set`/`unset` сохранят годные, применят правку и выбросят негодные значения,
вернув их в `dropped_invalid`. Испорченный штамп (`version`, `updated_at`) приходит отдельным
полем `broken_stamp` и в `issues` не попадает: его выставляет само семейство, задавать его
человеку нечем, и любая запись профиля — включая `set`/`unset` самого штампа, который отказом
больше не оборачивается, — перепишет его сама (`restamped` в ответе). Отказ на записи остаётся
у `unsupported` и у файла, который не разбирается как JSON-объект (в `show` нет ни `issues`, ни
`broken_stamp`): там правка руками по пути из `path` либо `reset`.

`suggest` строит подсказки по одной странице выборки. Если в его `notes` есть об этом пометка,
её надо назвать вместе с подсказкой: доли и порядок пространств посчитаны по этой странице
в порядке трекера, а не по всем задачам пользователя.

Код возврата `1` и JSON `{"ok": false, …}` в stderr означают, что **ничего не сохранено**: причина
в поле `error`, разбор по ключам — в `errors`. Неудачную запись за удавшуюся не выдавать.

Секретов в профиле не бывает: ключ, в имени которого есть `TOKEN`, `PASSWORD`, `AUTH`, `COOKIE`
или `SECRET`, отклоняется на записи и удаляется из файла при ближайшем сохранении. Пароли и токены
живут в `~/.swtr-review.env` и настраиваются через `/guru_doctor`.

**Что этим сценарием не перехватывать.** Слово «профиль» и глагол «настрой» заняты соседями:

| Фраза пользователя | Куда идти | Почему не сюда |
|---|---|---|
| «настрой контекст», «настрой гуру» | **сначала один вопрос с двумя вариантами** | фраза читается и как правка профиля, и как контекст-пак проекта: спроси, что настраиваем — личные умолчания (`/guru_profile`) или контекст проекта для агента (`guru-context`), — и жди ответа |
| «контекст-пак», `openspec/context/`, «агент не знает про наш продукт» | `guru-context` | ключ назван прямо: это контекст проекта для агента, а не умолчания человека |
| «настрой доступы», «не работает авторизация», 401/403 | `/guru_doctor` | это креды в `~/.swtr-review.env`; профиль секретов не хранит |
| «наборы навыков», «какие навыки включены», «профиль навыков» | таблица наборов в `README.md` | это состав активных навыков, а не значения умолчаний |

Если из фразы неясно, о каком «профиле» речь, — один уточняющий вопрос с двумя вариантами,
а не догадка.

**«Запомни это» без названного ключа — тоже вопрос, а не запись.** Правкой профиля просьба
становится, когда в ней названы и ключ (логин, роль, пространство, тип задачи, продукт,
компоненты, tenant/проект/репозиторий SourceControl, проект Jira, пространство Confluence),
и намерение запомнить («по умолчанию», «запомни», «сделай основным»). Ключа нет вовсе —
«запомни это», «сохрани, как сейчас» — спроси, какое именно умолчание записать, назвав
ключи профиля из списка выше, и **до ответа не сохраняй ничего**: за «это» стоит догадка, а
неверно записанное умолчание тихо поедет во все последующие вызовы.

## 26. Источники постановки со связанными страницами

**Фразы пользователя:**
- «собери источники по PVM-160042»
- «прочитай задачу со всеми связанными страницами»
- «что ещё надо прочитать, чтобы написать спеку по CRPV-35498»

```bash
python3 $SKILL_DIR/scripts/spec_sources.py PVM-160042
python3 $SKILL_DIR/scripts/spec_sources.py PVM-160042 --with-comments --depth 2 --max-linked 20
python3 $SKILL_DIR/scripts/spec_sources.py PVM-160042 --max-linked 20 --link-type dependend
```

Скрипт читает саму задачу теми же функциями, что и `fetch_swtr_sources.py`, и
дочитывает то, что с ней **названо связанным**: связи трекера (`link find`) и коды
юнитов, упомянутые в тексте — в теме, описании, атрибутах, теле вики, а с
`--with-comments` ещё и в комментариях. Ссылка на карточку юнита — тот же юнит,
что и голый код: она превращается в код и дочитывается штатно.

**Связанное — это названное, а не похожее.** Догадок по смыслу здесь нет: похожие
задачи ищет `swtr_task_tool.py similar` (сценарий 5), и смешивать эти два списка
нельзя — похожая задача не является источником требований.

**Кодоподобные строки в трекер не уходят.** `ISO-8601`, `UTF-8`, `HTTP-404`,
`Q1-2026` совпадают с формой кода юнита, и без проверки каждая становилась бы
отдельным чтением и строкой в «Не прочитано». Существование упомянутых кодов
проверяется ОДНИМ запросом на весь список, поэтому его цена не зависит от их
числа. Коды из связей проверять не нужно: они пришли от самого трекера.

**Внешние ссылки собираются со всех прочитанных источников**, а не только с
корневой задачи: ссылка на Confluence чаще стоит в связанной странице. На живом
эпике CRPV-110079 это разница между 2 и 22 найденными ссылками.

**Гейт sberworks действует и на обходе.** Ссылку на Confluence или Jira скрипт
только показывает — в поле `external`, где у каждой записи одна форма
`{url, system, key, readCommand}`. `readCommand` — готовая команда
(`confluence_tool.py get … --confirm-sberworks`); у чужого хоста она `null`,
потому что читать его навык не умеет. Сходить по ссылке скрипт не имеет права
даже когда она названа прямо в постановке: решение идти на sberworks остаётся
явным действием человека («Приоритет систем»).

Система определяется по настроенному адресу (`base_url_for`), а не по подстроке:
на sberworks Confluence и Jira живут на ОДНОМ хосте и различаются префиксом пути
(`/wiki` против `/jira`).

**Непрочитанное не исчезает.** Юнит, который не отдался, попадает в `skipped` с
причиной; связи сверх `--max-linked` — туда же с пометкой предела и заметкой в
`notes`; непрочитанная страница ответа (`hasNext`) тоже отмечается в `notes`.
Предел считает ПОПЫТКИ, а не удачи: иначе недоступные юниты его не тратили бы и
не ограничивали число запросов.

Отказ канала (связи, комментарии) не роняет чтение самой задачи: она прочитана и
полезна сама по себе, а причина уезжает в `notes`. **Исключение — 401/403:** он
кладёт все каналы разом, поэтому пробрасывается в единый error-contract, а не
превращается в «связей нет». Когда связи не прочитались, это видно в поле
`linksUnread`, и пакет говорит «часть источников не прочитана», а не «связанных
страниц не найдено»: это разные факты.

Вывод — один JSON: `{ok, command, request, root, source, comments, links, linked,
external, skipped, notes, markdown}`. Поле `source` — корневой юнит в форме
`fetch_swtr_sources.py`, поэтому вывод годится как `--source-file` для
`openspec_seed.py`:

```bash
python3 $SKILL_DIR/scripts/spec_sources.py PVM-160042 > /tmp/sources.json
python3 $SKILL_DIR/scripts/openspec_seed.py --source-file /tmp/sources.json
```

Файл скрипт не создаёт: готовый пакет лежит в поле `markdown`, записывает его
агент. Дальше — сценарий 18 (заготовка proposal), `guru-brainstorm` (требования и
дельта-спека) либо ревью документа (сценарий 4).

**Что этим сценарием не перехватывать:**

| Фраза пользователя | Куда идти | Почему не сюда |
|---|---|---|
| «что за задача PVM-…», «покажи задачу» | сценарий 1 (`get`) | нужна карточка, а не пакет источников: обход связей тут лишняя работа и лишний контекст |
| «проведи ревью постановки» | сценарий 4 (`fetch_swtr_sources` → анализ) | ревью оценивает КАЧЕСТВО одного документа, а не собирает материал |
| «найди похожие задачи» | сценарий 5 (`similar`) | похожесть — догадка по смыслу; здесь только названные связи |
| «декомпозируй эпик CRPV» | `guru-crpv-decomposer` | там свой реестр каналов контекста и своя методика нарезки; этот скрипт даёт сырьё, а не план |
