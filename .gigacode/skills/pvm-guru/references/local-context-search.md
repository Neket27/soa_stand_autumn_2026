# Локальный и удаленный поиск по контексту

## Локальный поиск

Локальный поиск использовать, когда пользователь явно передал пути к документации или коду. Не сканировать произвольные директории без разрешения пользователя.

Поддерживаемые директивы в пользовательском запросе:

```text
@local-docs /workspace/product/docs
@local-code /workspace/product/service-a
@local-search авторизация аудит журналирование миграция
```

Команда:

```bash
python3 $SKILL_DIR/scripts/local_context_search.py --paths /workspace/product/docs /workspace/product/service-a --query 'CRPV-35498' --query 'журналирование аудит миграция'
```

Что искать:

- коды задач и wiki-страниц;
- названия компонентов, модулей и сервисов;
- API path, события, очереди, топики, таблицы, индексы;
- метрики, алерты, логи, аудит;
- feature flags, статусы, роли и права;
- термины из проблемы и критериев приемки.

## SourceControl поиск

SourceControl использовать, когда пользователь дал tenant/project/repo/ref или просит найти реализацию в удаленном репозитории.

Если repo не указан, сначала показать кандидатов:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --query 'CostCalculator sizing' --repo-query 'cost calculator'
```

Если repo указан, искать по коду:

```bash
python3 $SKILL_DIR/scripts/source_control_search.py search --tenant 22f434fb-0355-42a1-ac25-011074a20a2d --project pvm --repo pvm-backend --ref master --query 'CostCalculator' --query 'sizing' --limit 30
```

## Как формулировать вывод

Разделять уровни уверенности:

- «Найдено в локальной документации» — когда есть совпадения в конкретных файлах.
- «Найдено в SourceControl» — когда есть совпадения в конкретном repo/ref.
- «Не найдено в проверенной области» — когда поиск выполнен, но совпадений нет.
- «Не проверялось» — когда пользователь не дал пути, tenant/project/repo/ref или секреты.

Нельзя писать «в коде этого нет», если проверена только часть кодовой базы.
