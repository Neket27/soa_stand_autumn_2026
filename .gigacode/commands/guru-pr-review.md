---
name: guru-pr-review
description: Сформировать markdown-отчет ревью SourceControl Pull Request
---

> Пути к скриптам ниже подставляет bootstrap-шаг `install.mjs`. Если вместо пути
> в тексте остался плейсхолдер в двойных фигурных скобках — шаг не выполнялся: определи каталог навыка по правилу
> «Каталог навыка» из его `SKILL.md` и подставь сам, путь не выдумывай.

Сформируй отчет `guru-pr-review` по SourceControl Pull Request, следуя workflow из skill `guru-pr-review`.

Сначала возьми логин текущего пользователя из профиля умолчаний соседнего навыка `pvm-guru`: с ним helper считает комментарии пользователя и не поднимает заново то, что тот сам уже закрыл.

!`python3 ".gigacode/skills/guru-pr-review/../pvm-guru/scripts/guru_profile.py" show`

Скрипт офлайн, печатает один JSON. Нужен ключ `profile.login`. Профиля нет, `status` не `ok`, ключ пуст или скрипт соседнего навыка не найден — запускай helper без `--profile-login`: логин возьмётся из окружения, а если нет и там — останется неизвестным, поведение прежнее.

Дальше выполни helper из skills/guru-pr-review/scripts/guru-pr-review.py, передав аргументы команды и логин из профиля, если он есть. Профильный логин едет своим флагом `--profile-login`, и helper берёт его только тогда, когда логин не назван ни `--login`, ни переменной окружения — порядок семейства «явное → окружение → профиль» ломать подстановкой в `--login` нельзя. Helper только собирает baseline SourceControl data и создает начальный markdown-файл. Запусти его сам, подставив логин; исполняемым блоком `` !`…` ``, как соседние команды семейства, этот вызов держать нельзя: такой блок выполняется до того, как ты подставишь значение, и `--profile-login '<логин из профиля>'` ушёл бы в helper буквально — отчёт молча посчитал бы, что своих комментариев у пользователя нет.

```bash
python3 ".gigacode/skills/guru-pr-review/scripts/guru-pr-review.py" "$ARGUMENTS" --profile-login '<логин из профиля>'
```

Использованный логин и его источник helper печатает полями `login` и `login_source` (`explicit`, `env`, `profile`): взятый из профиля назови одной фразой, как всякое применённое умолчание.

Дальше не останавливайся на выводе helper. Прочитай `report_path`, продолжи полный review workflow из `SKILL.md`: discussion history, merge/status limitations, Full/Quick mode, scope creep, subagent deep review where useful, findings, open questions, draft review comment. Обнови markdown-файл по финальному шаблону skill.

Если helper завершился ошибкой, покажи stderr и не придумывай данные.
