---
description: Прогнать полный ревью‑пайплайн (implementation → quality → simplification → testing → documentation) на текущем git diff
agent: build
subtask: true
---

Собери контекст изменений:

git status:
!`git status --porcelain`

diff stat:
!`git diff --stat`

diff:
!`git diff`

Дальше:
1) Вызови субагентов implementation, quality, simplification, testing, documentation (через tool task) и передай им: цель запроса + данные выше.
2) Сконсолидируй **только проблемы** в один список.
3) Ничего не редактируй в коде в рамках этого /review — это режим ревью.