#!/usr/bin/env python3
"""Offline inventory of the GURU skill family.

Answers one question for the onboarding command (`/guru-start`): what is
actually installed on this machine, and what is missing. It never opens a
socket and never writes a file — it prints one JSON object to stdout.

The layout it walks mirrors `install.mjs` (the bootstrap step that lays out
commands and subagents): the shared `~/.agents/skills` plus each known host's
own skills directory. Commands and subagents come from the manifest that
`install.mjs` writes, which is the authoritative record of what was laid out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _swtr_common import emit_error  # noqa: E402

# --------------------------------------------------------------------------
# The single source of truth for what "the GURU family" is. `onboarding-roles.md`
# names skills from this list and a smoke test enforces the containment, so a
# renamed skill breaks the suite instead of the user's first session.
# --------------------------------------------------------------------------
FAMILY_REPO = "ssh://git@portal.works.prod.sbt:7998/sbtai/PVM-GURU.git"

# Реестр ведётся записями, а не голыми именами: без «что на входе / что на выходе»
# карта ролей и /guru-start не могут объяснить человеку, зачем ему навык, а
# передача между навыками превращается в угадывание. `FAMILY` выводится из ключей,
# поэтому список и описания разойтись не могут.
FAMILY_INFO: Dict[str, Dict[str, str]] = {
    "pvm-guru": {
        "purpose": "доступ к задачам, вики, коду и TMS плюс ревью документов",
        "input": "код задачи, ссылка, файл, ключевые слова",
        "output": "JSON источников и отчёт ревью в docs/ai/",
        "owner": "аналитик, разработчик, лид",
    },
    "guru-openspec": {
        "purpose": "разбор и валидация артефактов OpenSpec, трассировка требований",
        "input": "каталог openspec/changes/<change-id>",
        "output": "вердикт L1, матрица покрытия, change-id",
        "owner": "аналитик, разработчик",
    },
    "guru-context": {
        "purpose": "контекст-пак проекта: интервью, подключение к config.yaml, журнал провалов",
        "input": "проект и ответы владельца",
        "output": "openspec/context/*.md и запись в config.yaml",
        "owner": "лид",
    },
    "guru-brainstorm": {
        "purpose": "проработка идеи до proposal и требований",
        "input": "идея, задача трекера, контекст",
        "output": "proposal.md и дельта-спека",
        "owner": "аналитик",
    },
    "guru-design": {
        "purpose": "мост между требованиями и кодом: разведка, привязка REQ к классам",
        "input": "change-id с готовыми требованиями",
        "output": "design.md и порядок rollout",
        "owner": "разработчик, архитектор",
    },
    "plan": {
        "purpose": "нарезка работы на исполняемые задачи",
        "input": "change-id либо описание работы",
        "output": "tasks.md изменения или docs/plans/YYYYMMDD-<slug>.md",
        "owner": "разработчик",
    },
    "exec": {
        "purpose": "исполнение задач субагентами с ревью, верификацией L2 и drift",
        "input": "tasks.md изменения или план",
        "output": "коммиты в feature-ветке и отчёт",
        "owner": "разработчик",
    },
    "guru-multirepo": {
        "purpose": "кросс-репо фича: реестр репозиториев, разнесение плана, покрытие и архивация",
        "input": "store продукта и запрос про фичу",
        "output": "per-repo changes, вердикт L1-L3, архив в правильном порядке",
        "owner": "лид, архитектор",
    },
    "guru-crpv-decomposer": {
        "purpose": "декомпозиция эпика CRPV и историй в задачи трекера",
        "input": "код эпика или истории",
        "output": "созданные задачи и связи в SWTR",
        "owner": "аналитик, лид",
    },
    "prd-requirement-decomposer": {
        "purpose": "разбор постановки в атомарные требования",
        "input": "PRD, CR, постановка",
        "output": "requirements.csv",
        "owner": "аналитик",
    },
    "user-story-invest-analyzer": {
        "purpose": "проверка пользовательской истории по INVEST",
        "input": "текст истории",
        "output": "разбор по шести критериям",
        "owner": "аналитик",
    },
    "estimation-service": {
        "purpose": "оценка трудозатрат и декомпозиция по фазам",
        "input": "постановка или список работ",
        "output": "оценка в чел-днях",
        "owner": "лид",
    },
    "agent-task-rewriter": {
        "purpose": "нормализация сырого тикета под кодинг-агента",
        "input": "тикет как есть",
        "output": "постановка, пригодная для агента",
        "owner": "разработчик",
    },
    "guru-pr-review": {
        "purpose": "ревью Pull Request: diff, архитектура, тесты, scope creep",
        "input": "ссылка вида …/pulls/<id>",
        "output": "отчёт ревью в docs/ai/",
        "owner": "разработчик, лид",
    },
    # Процессные навыки: задают способ работы, данными не владеют -- нет скриптов,
    # сети и корпоративных доступов. Стоят в реестре наравне с остальными, потому
    # что карта ролей их называет и /guru-start обязан знать, установлены ли они.
    "guru-dialectic": {
        "purpose": "проверка утверждения аргументами за и против",
        "input": "тезис",
        "output": "разбор двух позиций и вывод",
        "owner": "любая роль",
    },
    "guru-root-cause-investigator": {
        "purpose": "поиск причины ошибки, падения сборки, деградации",
        "input": "симптом и доступ к логам или коду",
        "output": "причина с доказательствами",
        "owner": "разработчик",
    },
    "guru-writing-style": {
        "purpose": "стиль текста наружу: комментарии, описания PR, замечания ревью",
        "input": "черновик текста",
        "output": "переписанный текст без канцелярита",
        "owner": "любая роль",
    },
}

FAMILY: Tuple[str, ...] = tuple(FAMILY_INFO)


# Хосты перечисляются явно, потому что `npx skills` без --agent ставит скилл во
# ВСЕ известные ему агенты (~75), стоит детекту не найти на машине ни одного: у
# свежей установки каталога ~/.config/opencode ещё нет. Тогда
# он засоряет $HOME каталогами чужих агентов и печатает по строке `Failed to
# install … does not support global skill installation` на каждый скилл для Eve и
# PromptScript -- единственных двух, кто глобальную установку не умеет. Список
# именно через пробел: его парсер --agent набирает имена до следующего флага, а
# "a,b,c" считает одним именем хоста и обрывает установку с `Invalid agents`.
INSTALL_HOSTS = "opencode"


def install_hint(name: str) -> str:
    """The exact command that installs one family skill."""
    return "npx skills add %s --skill %s -g -y --agent %s" % (FAMILY_REPO, name, INSTALL_HOSTS)


# --------------------------------------------------------------------------
# Host layout — kept in step with HOSTS in install.mjs. `detect` is the marker of
# an installed host, и это каталог КОНФИГА хоста, а не каталог скиллов: при штатной
# установке (`npx skills add -g`) скиллы лежат в общем ~/.agents/skills, и своего
# skills у хоста может не быть вовсе. Детект по каталогу скиллов пропускал бы
# ровно ту установку, ради которой /guru-start и существует — свежую.
# --------------------------------------------------------------------------
HOSTS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "opencode",
        "label": "OpenCode",
        "skills": (".config", "opencode", "skills"),
        "detect": (".config", "opencode"),
        "commands": ((".config", "opencode", "commands"), (".config", "opencode", "command")),
        "agents": ((".config", "opencode", "agents"), (".config", "opencode", "agent")),
    },
    {
        "id": "gigacode",
        "label": "GigaCode",
        "skills": (".gigacode", "skills"),
        "detect": (".gigacode",),
        "commands": ((".gigacode", "commands"),),
        # install.mjs раскладывает агентов и сюда, конвертируя их под формат
        # GigaCode (см. renderAgent в install.mjs).
        "agents": ((".gigacode", "agents"),),
    },
)

SHARED_SKILLS_DIR = (".agents", "skills")
MANIFEST_REL = (".config", "pvm-guru", "extras-manifest.json")

_BLOCK_INDICATORS = (">", "|", ">-", "|-", ">+", "|+")


def pick_dir(home: Path, variants: Sequence[Sequence[str]]) -> Path:
    """First existing variant, else the first one — same rule as install.mjs."""
    for variant in variants:
        candidate = home.joinpath(*variant)
        if candidate.exists():
            return candidate
    return home.joinpath(*variants[0])


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_frontmatter(path: Path) -> Dict[str, str]:
    """Top-level scalars of a YAML frontmatter block.

    Deliberately minimal: the skill's scripts are stdlib-only, so there is no
    YAML parser. It covers the three shapes SKILL.md and command files actually
    use — a plain scalar, a quoted scalar, and a folded block (`>` / `|`) whose
    continuation lines are indented. Nested mappings are collapsed to text and
    ignored; only `name` and `description` are ever read by callers.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: Dict[str, str] = {}
    key = ""
    block: List[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            if key:
                fields[key] = " ".join(block).strip()
            raw_key, _, rest = line.partition(":")
            key = raw_key.strip()
            block = []
            rest = rest.strip()
            if rest and rest not in _BLOCK_INDICATORS:
                fields[key] = _unquote(rest)
                key = ""
        elif key and line.strip():
            block.append(line.strip())
    if key:
        fields[key] = " ".join(block).strip()
    return fields


def discover_skills(
    home: Path,
    skills_root: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every skill directory visible to any known host, merged by skill name.

    A skill installed globally lives in the shared directory and is reported
    under the pseudo-host ``shared`` — it is not a host, but without it a
    ``npx skills … -g`` install would read as "not installed".

    Returns ``(skills, notes)``; notes explain what could not be read.
    """
    roots: List[Tuple[str, Path]] = [
        ("shared", skills_root if skills_root is not None else home.joinpath(*SHARED_SKILLS_DIR)),
    ]
    for host in HOSTS:
        roots.append((host["id"], home.joinpath(*host["skills"])))

    found: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    copies: Dict[str, List[Path]] = {}
    for host_id, root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            # is_dir() above only stats the path (needs +x on its parents); listing
            # it is a separate syscall that can still fail -- foreign owner, stale
            # mount, broken symlink. One bad root must not abort every other root.
            notes.append(
                "хост %s: не удалось прочитать содержимое каталога %s" % (host_id, root))
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            record = found.setdefault(entry.name, {
                "name": entry.name,
                "description": "",
                "family": entry.name in FAMILY,
                "hosts": [],
            })
            if host_id not in record["hosts"]:
                record["hosts"].append(host_id)
            copies.setdefault(entry.name, []).append(entry)
            if record["description"]:
                continue
            meta = read_frontmatter(entry / "SKILL.md")
            if meta.get("description"):
                record["description"] = meta["description"]
            else:
                notes.append(
                    "навык %s: не удалось прочитать описание из %s" % (entry.name, entry / "SKILL.md"))
    notes.extend(stale_copy_notes(copies))
    return [found[name] for name in sorted(found)], notes


def _skill_bytes(path: Path) -> Optional[bytes]:
    try:
        return (path / "SKILL.md").read_bytes()
    except OSError:
        return None


def stale_copy_notes(copies: Dict[str, List[Path]]) -> List[str]:
    """Notes about a skill installed twice, in different versions.

    A host may keep its own copy of a skill next to the shared one (GigaCode does:
    its loader reads `~/.gigacode/skills` before `~/.agents/skills`). `npx skills
    update` refreshes only the shared directory, so such a copy silently freezes and
    the host keeps running the version it was installed with — a whole onboarding was
    once answered from a week-old skill this way. Two copies are compared by their
    SKILL.md bytes: identical copies are a harmless duplicate and stay silent.
    """
    notes: List[str] = []
    for name in sorted(copies):
        paths = copies[name]
        if len(paths) < 2:
            continue
        seen: List[Path] = []
        for path in paths:
            # Симлинк на уже учтённый каталог — это та же копия, а не вторая версия.
            if any(_same_dir(path, other) for other in seen):
                continue
            seen.append(path)
        if len(seen) < 2:
            continue
        first = _skill_bytes(seen[0])
        divergent = [path for path in seen[1:] if _skill_bytes(path) != first]
        if not divergent:
            continue
        notes.append(
            "навык %s установлен дважды и версии разные: %s. Хост читает свою копию раньше общей, "
            "а `npx skills update` обновляет только общую — обновите или удалите лишнюю копию "
            "(например, замените её симлинком на %s)" % (
                name,
                ", ".join(str(path) for path in seen),
                seen[0],
            ))
    return notes


def _same_dir(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _contains_run(parts: Sequence[str], variant: Sequence[str]) -> bool:
    """Whether `variant` appears as a contiguous run inside `parts`."""
    width = len(variant)
    variant = tuple(variant)
    return any(tuple(parts[i:i + width]) == variant for i in range(len(parts) - width + 1))


def host_for_path(target: Path) -> str:
    """Host id a laid-out extras file belongs to ("" when unrecognized).

    Derived from HOSTS' own `commands` and `agents` variants instead of a
    separate hardcoded chain, so a host added to HOSTS is recognized here too
    without a matching edit -- that drift is exactly what let a laid-out
    command (and, until this fix, every laid-out subagent) silently disappear
    from a record's `hosts` list before this fix. A host whose `agents` is
    None has no agents directory laid out for it and is matched by `commands`
    only; both target hosts (opencode, gigacode) currently do have one.

    The manifest holds absolute paths written under whatever HOME
    `install.mjs` ran under, not the HOME this process runs under (tests use
    `/home/u/...`), so a host can't be matched by joining its segments onto
    the current home and comparing paths. Instead each host is matched by its
    `commands`/`agents` segments appearing as a contiguous run inside the
    target's own path parts, wherever they fall.
    """
    parts = target.parts
    for host in HOSTS:
        variants = list(host["commands"])
        if host["agents"] is not None:
            variants += list(host["agents"])
        if any(_contains_run(parts, variant) for variant in variants):
            return host["id"]
    return ""


def read_extras(
    manifest_path: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Commands and subagents laid out by install.mjs, read from its manifest.

    The manifest is the authoritative record of what was laid out and from
    which skill, so this never guesses by scanning host directories. A missing
    or damaged manifest is not an error — it means the bootstrap step has not
    run (or ran from another checkout), and that is reported in the notes.

    Returns ``(commands, agents, notes)``.
    """
    if not manifest_path.is_file():
        return [], [], [
            "манифест %s не найден: команды и субагенты не раскладывались — "
            "выполните bootstrap-шаг `node ~/.agents/skills/pvm-guru/install.mjs`" % manifest_path]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [], [], ["манифест %s нечитаем (%s): состав команд не определён" % (manifest_path, exc)]

    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return [], [], ["манифест %s без секции files: состав команд не определён" % manifest_path]

    buckets: Dict[str, Dict[str, Dict[str, Any]]] = {"commands": {}, "agents": {}}
    for target, entry in sorted(files.items()):
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in buckets:
            continue
        path = Path(target)
        record = buckets[kind].setdefault(path.stem, {
            "name": path.stem,
            "description": read_frontmatter(path).get("description", ""),
            "skill": entry.get("skill", ""),
            "hosts": [],
        })
        host_id = host_for_path(path)
        if host_id and host_id not in record["hosts"]:
            record["hosts"].append(host_id)

    return (
        [buckets["commands"][name] for name in sorted(buckets["commands"])],
        [buckets["agents"][name] for name in sorted(buckets["agents"])],
        [],
    )


def build_catalog(
    home: Path,
    skills_root: Optional[Path] = None,
    manifest: Optional[Path] = None,
) -> Dict[str, Any]:
    """The whole inventory as one JSON-ready object."""
    notes: List[str] = []

    hosts: List[Dict[str, Any]] = []
    for host in HOSTS:
        if not home.joinpath(*host["detect"]).is_dir():
            continue
        hosts.append({
            "id": host["id"],
            "label": host["label"],
            "skillsDir": str(home.joinpath(*host["skills"])),
            "commandsDir": str(pick_dir(home, host["commands"])),
        })
    if not hosts:
        notes.append(
            "не найден ни один известный хост (OpenCode, GigaCode) в %s" % home)

    skills, skill_notes = discover_skills(home, skills_root)
    notes.extend(skill_notes)

    commands, agents, extras_notes = read_extras(
        manifest if manifest is not None else home.joinpath(*MANIFEST_REL))
    notes.extend(extras_notes)

    installed = {skill["name"] for skill in skills}
    missing_core = [
        {"name": name, "install": install_hint(name)}
        for name in FAMILY if name not in installed
    ]

    return {
        "ok": True,
        "command": "catalog",
        "hosts": hosts,
        "skills": skills,
        "commands": commands,
        "agents": agents,
        "missingCore": missing_core,
        "notes": notes,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Офлайн-инвентаризация установленных навыков семейства GURU. "
                    "Печатает один JSON-объект в stdout, в сеть не ходит, файлов не пишет.")
    parser.add_argument("--home", default=None,
                        help="Домашний каталог (по умолчанию — текущий пользователь)")
    parser.add_argument("--skills-root", default=None,
                        help="Общий каталог навыков вместо ~/.agents/skills")
    parser.add_argument("--manifest", default=None,
                        help="Манифест install.mjs вместо ~/.config/pvm-guru/extras-manifest.json")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        catalog = build_catalog(
            Path(args.home).expanduser() if args.home else Path.home(),
            Path(args.skills_root).expanduser() if args.skills_root else None,
            Path(args.manifest).expanduser() if args.manifest else None,
        )
    except OSError as exc:
        return emit_error("catalog", exc)
    print(json.dumps(catalog, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
