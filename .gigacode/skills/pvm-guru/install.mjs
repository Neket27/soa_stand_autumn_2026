#!/usr/bin/env node
// Раскладывает commands и agents скиллов семейства PVM-GURU по каталогам
// установленных хостов. Запуск:
//   node ~/.agents/skills/pvm-guru/install.mjs [--dry-run] [--force] [--prune] [--agent <list>]

import { createHash } from 'node:crypto'
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { spawnSync } from 'node:child_process'

import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Каталог дерева навыков в источнике: как было до слияния копий и как стало. */
export const LOCK_PATH_WAS = '.gigacode.example/skills/'
export const LOCK_PATH_NOW = '.gigacode/skills/'

/** Имена скиллов, лежащих рядом с этим файлом: каталог с SKILL.md внутри. */
export function skillNames(skillsRoot) {
  if (!existsSync(skillsRoot)) return new Set()
  return new Set(
    readdirSync(skillsRoot, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && existsSync(join(skillsRoot, entry.name, 'SKILL.md')))
      .map((entry) => entry.name),
  )
}

export function skillLockPath(home, env = process.env) {
  // Ровно то же разрешение, что у менеджера скиллов (getSkillLockPath в его CLI):
  // при заданном XDG_STATE_HOME учётная запись лежит там, и только там. Мы читали
  // её всегда из ~/.agents и на такой машине чинили бы не тот файл — молча, потому
  // что отсутствие файла для нас штатная ветка.
  const xdgStateHome = env?.XDG_STATE_HOME
  if (xdgStateHome) return join(xdgStateHome, 'skills', '.skill-lock.json')
  return join(home, '.agents', '.skill-lock.json')
}

/**
 * Починить учётную запись менеджера скиллов после переезда дерева навыков.
 *
 * Менеджер ищет навык в свежем клоне по пути, записанному при установке
 * (`entry.skillPath`), и при несовпадении молча пропускает запись. Наблюдаемо это
 * выглядит как зелёное «всё актуально» при устаревшем содержимом, а вдобавок
 * навык попадает в список удалённых из источника — с предложением снести
 * локальную копию. После переименования `.gigacode.example` в `.gigacode` под это
 * попадает каждая запись каждой установки, поэтому путь чинится здесь: install.mjs
 * запускают после любой установки и любого обновления.
 *
 * Правится только префикс и только у записей нашего дерева. Признак «нашего» —
 * пара: имя скилла, который лежит рядом с этим файлом, И тот же адрес источника,
 * что у якорной записи. Одного имени мало: имена вроде `plan` или `exec` не наши
 * собственные, и чужой набор с такой же раскладкой пути мы бы переписали. Одного
 * адреса тоже мало: репозиторий форкают и переносят, поэтому адрес не зашит в код,
 * а берётся из самой учётной записи — у записи `pvm-guru`. Якорь только один: в
 * учётной записи без нашей записи любой источник чужой, и угадывать нечем.
 *
 * Любое поле, кроме `skillPath`, не трогается: учётную запись ведёт менеджер.
 */
export function repairSkillLock(home, options = {}) {
  const { dryRun = false, known = null, env = process.env } = options
  const path = skillLockPath(home, env)
  if (!existsSync(path)) return { repaired: [], skipped: 'учётной записи нет' }

  let lock
  try {
    lock = JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    // Битую запись чинит менеджер при следующей установке, а установку ронять
    // из-за неё нельзя: команды разложить мы всё равно можем.
    return { repaired: [], skipped: 'учётная запись не разобрана' }
  }

  const skills = lock?.skills
  if (!skills || typeof skills !== 'object') return { repaired: [], skipped: 'в учётной записи нет скиллов' }

  const candidates = Object.entries(skills).filter(([name, entry]) => {
    if (!entry || typeof entry.skillPath !== 'string') return false
    if (!entry.skillPath.startsWith(LOCK_PATH_WAS)) return false
    return !known || known.has(name)
  })
  if (!candidates.length) return { repaired: [], skipped: null }

  // Якорь только один — запись `pvm-guru`: этот файл лежит внутри неё, и её адрес
  // источника заведомо наш. Догадываться по единственному источнику кандидатов
  // нельзя: в учётной записи без нашей записи этот единственный источник — чужой.
  const source = skills['pvm-guru']?.source ?? skills['pvm-guru']?.sourceUrl
  if (source == null) {
    return { repaired: [], skipped: 'в учётной записи нет записи pvm-guru — определять наши записи нечем' }
  }

  const repaired = []
  for (const [name, entry] of candidates) {
    if ((entry.source ?? entry.sourceUrl) !== source) continue
    entry.skillPath = LOCK_PATH_NOW + entry.skillPath.slice(LOCK_PATH_WAS.length)
    repaired.push(name)
  }

  if (!repaired.length) return { repaired: [], skipped: null }
  if (!dryRun) writeFileSync(path, `${JSON.stringify(lock, null, 2)}\n`)
  return { repaired, skipped: null, path }
}

/**
 * Метка фоновой задачи обновления, которую заводили прежние версии установщика.
 *
 * Самой подсистемы обновления в репозитории больше нет — обновление идёт штатной
 * командой менеджера скиллов. Но задача, заведённая старой версией, живёт на
 * машине пользователя и переживает удаление кода: launchd подхватывает plist при
 * каждом входе в систему, планировщик Windows держит запись у себя. Снять её
 * может только тот, кто про неё знает, поэтому снятие остаётся здесь и после
 * сноса — иначе у людей навсегда осталась бы задача, зовущая несуществующий файл.
 */
const LEGACY_UPDATE_TASK = 'guru.skills.update'

function runTask(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8' })
  return {
    ok: !result.error && result.status === 0,
    status: result.status ?? null,
    error: result.error ? result.error.message : null,
  }
}

/** Чем спрашивать и чем снимать задачу на этой системе. */
function legacyTaskAdapter(home, platform) {
  if (platform === 'darwin') {
    const label = `gui/${process.getuid?.() ?? 501}/${LEGACY_UPDATE_TASK}`
    const domain = `gui/${process.getuid?.() ?? 501}`
    return {
      path: join(home, 'Library', 'LaunchAgents', `${LEGACY_UPDATE_TASK}.plist`),
      query: (run) => run('launchctl', ['print', label]),
      // Пробник домена: отвечает ли launchctl вообще. Его успех превращает отказ по
      // самой службе в доказательство того, что службы нет.
      probe: (run) => run('launchctl', ['print', domain]),
      remove: (run) => run('launchctl', ['bootout', label]),
    }
  }
  if (platform === 'win32') {
    return {
      path: join(home, '.config', 'pvm-guru', 'scheduled-task.xml'),
      query: (run) => run('schtasks', ['/Query', '/TN', LEGACY_UPDATE_TASK]),
      // То же самое: `/Query` без имени перечисляет задачи и доказывает, что сам
      // планировщик на связи.
      probe: (run) => run('schtasks', ['/Query']),
      remove: (run) => {
        // `/Delete` убирает задачу из расписания, но НЕ прерывает уже запущенную ею
        // программу. Результат `/End` не проверяется: «нечего останавливать» — штатный
        // отказ, а снятие оценивается по `/Delete`.
        run('schtasks', ['/End', '/TN', LEGACY_UPDATE_TASK])
        return run('schtasks', ['/Delete', '/TN', LEGACY_UPDATE_TASK, '/F'])
      },
    }
  }
  return null
}

function why(result) {
  return result.error ?? (result.status == null ? 'ответ не разобран' : `код ${result.status}`)
}

/**
 * Снять фоновую задачу обновления, заведённую прежними версиями.
 *
 * Возвращает `{removed, path, cleared, reason}`.
 *
 * `removed` — тронули ли мы что-нибудь. `cleared` — есть ли ПОЛОЖИТЕЛЬНОЕ
 * свидетельство, что задачи в планировщике больше нет; только по нему можно сносить
 * шим и клон источника, потому что живая задача продолжала бы на них ходить.
 *
 * Свидетельство считается положительным ровно в двух случаях: планировщик не знает
 * задачи по имени И отвечает на запрос о себе самом; либо задача была найдена,
 * команда снятия отработала без ошибки И повторный запрос её уже не находит.
 * Лежащий рядом файл задачи на это не влияет ни в ту, ни в другую сторону.
 *
 * Запрос о самом планировщике и есть то, что превращает отказ по имени в
 * доказательство отсутствия: домен `gui/<uid>` на macOS, `schtasks /Query` без имени
 * на Windows. Ответил он — значит и про задачу ответил честно. Не ответил — наличие
 * задачи не проверено, и это `cleared: false`. Разбирать текст ответа мы не можем:
 * он локализован, а на Windows ещё и кодируется ANSI-страницей.
 *
 * Файл задачи убирается в любом случае: иначе она вернулась бы при следующем входе
 * в систему. Но снятой она от этого не считается — загруженная задача переживает
 * удаление файла, а задача Windows вообще живёт независимо от импортного XML.
 *
 * Обе системы ходят через общую таблицу: пока ветки были написаны раздельно, эта
 * самая проверка разошлась в них одинаково неверно.
 */
export function removeLegacyUpdateTask(home, options = {}) {
  const { platform = process.platform, run = runTask } = options
  const task = legacyTaskAdapter(home, platform)
  if (!task) return { removed: false, path: null, cleared: true, reason: null }

  const first = task.query(run)
  const hadFile = existsSync(task.path)

  if (first.ok) {
    const removal = task.remove(run)
    if (hadFile) rmSync(task.path, { force: true })
    const after = task.query(run)
    if (removal.ok && !after.ok) return { removed: true, path: task.path, cleared: true, reason: null }
    return {
      removed: true,
      path: task.path,
      cleared: false,
      reason: removal.ok ? 'задача всё ещё отвечает на запрос' : `снятие отказало (${why(removal)})`,
    }
  }

  // Планировщик задачу не нашёл. Сам по себе этот ответ ничего не доказывает: тем же
  // ненулевым кодом он отвечает и на собственную недоступность. Отсутствие файла
  // задачи различия не добавляет — загруженная задача переживает удаление файла, а
  // задача Windows вообще живёт независимо от импортного XML. Поэтому спрашиваем
  // планировщик о нём самом: отвечает он — значит и про задачу ответил честно.
  const probe = task.probe(run)
  if (hadFile) rmSync(task.path, { force: true })
  if (probe.ok) return { removed: hadFile, path: task.path, cleared: true, reason: null }
  return {
    removed: hadFile,
    path: task.path,
    cleared: false,
    reason: `планировщик недоступен (${why(probe)}) — наличие задачи не проверено`,
  }
}

/**
 * Хосты, которым мы больше не раскладываем команды, но раскладывали раньше.
 *
 * Просто убрать их из HOSTS мало: `planActions` подчищает устаревшие записи только
 * внутри каталогов текущих хостов, поэтому файлы, разложенные прежними версиями в
 * ~/.claude/commands и ~/.codex/prompts, остались бы навсегда — вместе с записями в
 * манифесте, где у них по-прежнему стоим владельцем мы. Каталоги перечислены здесь,
 * попадают в зону уборки наравне с текущими, и разложенное когда-то снимается при
 * первой же установке.
 */
export const RETIRED_HOST_DIRS = [
  ['.claude', 'commands'],
  ['.codex', 'prompts'],
]

/** Каталоги снятых хостов в конкретном доме. */
export function retiredDirs(home) {
  return RETIRED_HOST_DIRS.map((parts) => join(home, ...parts))
}

/**
 * Всё, что снесённая подсистема обновления оставляла на машине: шим, клон источника,
 * файл состояния, журнал, замок и файл намерения. Задачу снимает
 * `removeLegacyUpdateTask` — она отдельно, потому что живёт в планировщике ОС.
 *
 * Хранилище вытесненного (`~/.agents/.guru-cache/trash`) в список не входит: туда
 * попадали каталоги скиллов вместе с правками, сделанными руками, и молча стирать их
 * при обычной установке нельзя. Оно лежит рядом с клоном, но снаружи него, поэтому
 * удаление клона его не задевает.
 */
export function legacyUpdaterPaths(home) {
  const base = join(home, '.config', 'pvm-guru')
  return [
    join(home, '.agents', 'bin', 'guru-update'),
    join(home, '.agents', 'bin', 'guru-update.cmd'),
    join(home, '.agents', '.guru-cache', 'PVM-GURU'),
    join(base, 'updater.json'),
    join(base, 'update.log'),
    join(base, 'update.lock'),
    join(base, 'update-intent.json'),
  ]
}

export function removeLegacyUpdaterFiles(home) {
  const intentPath = join(home, '.config', 'pvm-guru', 'update-intent.json')
  const targets = legacyUpdaterPaths(home).filter((path) => path !== intentPath)
  const removed = []
  for (const target of targets) {
    if (!existsSync(target)) continue
    rmSync(target, { recursive: true, force: true })
    removed.push(target)
  }

  // Файл намерения означает применение, оборванное на середине: часть склада могла
  // остаться в прежней версии, часть в новой. Стереть его молча значит спрятать этот
  // факт, а довести применение уже нечем — кода, который это умел, больше нет.
  const warnings = []
  if (existsSync(intentPath)) {
    warnings.push(
      `осталось незавершённое применение прежнего обновлятора: ${intentPath} — ` +
        'состав склада мог остаться смешанным, проверьте его и удалите файл руками',
    )
  }

  return { removed, warnings }
}

// Целевые хосты — OpenCode и GigaCode, и только они. Claude Code и Codex отсюда убраны
// сознательно: команды и субагентов туда никто не раскладывает, пользователь ставит навыки
// в них сам. Это сужение раскладки, а не отказ от поддержки — сами навыки на этих хостах
// работают, и описания их поведения там остаются в документации.
export const HOSTS = [
  {
    id: 'opencode',
    label: 'OpenCode',
    skillsDir: ['.config', 'opencode', 'skills'],
    // Каталог конфига, а не skills: `npx skills add … -g` кладёт скиллы в общий ~/.agents/skills,
    // откуда OpenCode их и читает, поэтому ~/.config/opencode/skills у него может не появиться
    // никогда. Детект по skills пропускал такой хост, и команды не раскладывались. Свой каталог
    // конфига OpenCode создаёт всегда — и на Windows тоже: его globalConfigPath это
    // XDG_CONFIG_HOME || ~/.config, без ветки на APPDATA.
    detectDir: ['.config', 'opencode'],
    commands: [['.config', 'opencode', 'commands'], ['.config', 'opencode', 'command']],
    agents: [['.config', 'opencode', 'agents'], ['.config', 'opencode', 'agent']],
  },
  {
    id: 'gigacode',
    label: 'GigaCode',
    skillsDir: ['.gigacode', 'skills'],
    // GigaCode читает скиллы и из общего ~/.agents/skills (в его бандле
    // SKILL_PROVIDER_CONFIG_DIRS = [".gigacode", ".agents"]), поэтому собственного каталога
    // skills у него может не быть — детектим по каталогу конфига самого CLI.
    detectDir: ['.gigacode'],
    commands: [['.gigacode', 'commands']],
    // Его загрузчик команд глобит `**/*.md` в этом каталоге, то есть формат наш.
    agents: [['.gigacode', 'agents']],
    // Субагентов GigaCode поддерживает (в бандле AGENT_CONFIG_DIR = "agents"), но читает их
    // своим разборщиком: требует `name`, не знает `permission` и ограничивает субагента
    // массивом `tools`. Файл без `name` он отбрасывает молча (parseSubagentContent бросает,
    // loadSubagentFromDir делает continue), поэтому агенты идут сюда через renderAgent.
    agentFormat: 'gigacode',
  },
]

const KINDS = ['commands', 'agents']

function pickDir(home, variants) {
  for (const variant of variants) {
    const path = join(home, ...variant)
    if (existsSync(path)) return path
  }
  return join(home, ...variants[0])
}

// realpathSync бросает на несуществующем пути — для сравнения путей это не ошибка, а сигнал
// «этот кандидат не может быть тем же каталогом, что и разрешённый путь», поэтому просто
// возвращаем путь как есть и даём сравнению строк сработать штатно (оно не совпадёт).
export function realpathOrSelf(path) {
  try {
    return realpathSync(path)
  } catch {
    return path
  }
}

export function detectHosts(home, only = null) {
  const wanted = only && only.length ? new Set(only) : null
  const hosts = []
  for (const host of HOSTS) {
    if (wanted && !wanted.has(host.id)) continue
    // Признак установленного хоста — его каталог конфига (detectDir), а не каталог скиллов:
    // скиллы живут в общем ~/.agents/skills, и своего skills у хоста может не быть вовсе.
    // skillsDir остаётся запасным вариантом для хостов без detectDir.
    if (!existsSync(join(home, ...(host.detectDir ?? host.skillsDir)))) continue
    hosts.push({
      id: host.id,
      label: host.label,
      commandsDir: pickDir(home, host.commands),
      agentsDir: host.agents ? pickDir(home, host.agents) : null,
      agentFormat: host.agentFormat ?? 'opencode',
    })
  }
  return hosts
}

// Плейсхолдер каталога навыка в файлах команд. Команда — это обычный prompt-файл: хост
// отдаёт её модели текстом как есть, никакой подстановки на его стороне нет. Поэтому путь
// к скриптам навыка подставляется здесь, в момент раскладки, — иначе команда несёт путь,
// который верен только в чекауте PVM-GURU, а у пользователя (агент запущен в его проекте)
// не существует.
export const SKILL_DIR_PLACEHOLDER = '{{SKILL_DIR}}'

export function renderCommand(content, skillDir) {
  return content.split(SKILL_DIR_PLACEHOLDER).join(skillDir)
}

// --- формат агентов ----------------------------------------------------------
// Файлы агентов написаны в формате OpenCode. У GigaCode свой разборщик субагентов
// (packages/core/dist/src/subagents/subagent-manager.js в его бандле): `name` обязателен,
// `mode` и `permission` он не читает вовсе, температура живёт в `modelConfig.temp`, а доступ
// к инструментам задаётся массивом `tools`. Файл без `name` он отбрасывает молча — parse
// бросает, а вызывающий делает `continue`, — поэтому под него агент пересобирается, а не
// копируется: скопированный лежал бы на диске и не работал, без единого сообщения.

const FRONTMATTER_RE = /^---\n([\s\S]*?)\n---\n([\s\S]*)$/

// Имена инструментов — из tool-names.js бандла GigaCode. Чтение есть у каждого субагента:
// без него он бесполезен, а право прочитать — это не право что-то изменить. Всё остальное
// включается только явным `allow` в permission.
const GIGACODE_BASE_TOOLS = ['read_file', 'grep_search', 'glob', 'list_directory', 'todo_write']

const GIGACODE_TOOLS_BY_PERMISSION = {
  edit: ['edit', 'write_file'],
  bash: ['run_shell_command'],
  webfetch: ['web_fetch'],
  websearch: ['web_search'],
  task: ['agent'],
  skill: ['skill'],
}

// OpenCode разбирает свой frontmatter мягко, поэтому в наших агентах description живёт сырым
// текстом — с незакавыченным двоеточием («…в свежем контексте: реализация → тесты»), а кое-где
// уже в кавычках. Для YAML первое — вложенный маппинг и ошибка разбора, а ошибку разбора
// GigaCode проглатывает молча. Значит, скаляр надо привести к валидному виду ровно один раз:
// снять кавычки, если они есть, и закавычить заново с экранированием.
function yamlScalar(raw) {
  const value = raw.trim()
  const inner =
    value.length >= 2 && value.startsWith('"') && value.endsWith('"')
      ? value.slice(1, -1).replace(/\\([\\"])/g, '$1')
      : value.length >= 2 && value.startsWith("'") && value.endsWith("'")
        ? value.slice(1, -1).replace(/''/g, "'")
        : value
  return `"${inner.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

// Возвращает индекс первой строки после блока permission.
function readPermissionBlock(lines, start, permission) {
  let key = null
  let i = start
  for (; i < lines.length; i += 1) {
    if (!/^[ \t]+\S/.test(lines[i])) break
    const top = /^ {2}(?:"([^"]*)"|([\w-]+)):[ \t]*(\S*)[ \t]*$/.exec(lines[i])
    if (top) {
      key = top[1] ?? top[2]
      permission[key] = top[3] === 'allow'
      continue
    }
    // Вложенные записи вроде `"git push*": deny` — правила для одного и того же инструмента.
    // В GigaCode инструмент либо есть у субагента целиком, либо его нет: списка правил на
    // команду там не предусмотрено, поэтому хотя бы один `allow` включает инструмент.
    const nested = /^ {4,}(?:"([^"]*)"|([\w-]+)):[ \t]*(\S*)[ \t]*$/.exec(lines[i])
    if (nested && key) permission[key] = permission[key] || nested[3] === 'allow'
  }
  return i
}

// null означает «этот агент этому хосту не подходит» — раскладывать его нельзя.
export function renderAgent(content, format, file) {
  if (format !== 'gigacode') return content

  const match = FRONTMATTER_RE.exec(content.replace(/\r\n/g, '\n'))
  if (!match) return null
  const [, frontmatter, body] = match

  const lines = frontmatter.split('\n')
  const fields = new Map()
  const permission = {}
  for (let i = 0; i < lines.length; i += 1) {
    const entry = /^([A-Za-z_][\w-]*):[ \t]*(.*)$/.exec(lines[i])
    if (!entry) continue
    if (entry[1] === 'permission') {
      i = readPermissionBlock(lines, i + 1, permission) - 1
      continue
    }
    fields.set(entry[1], entry[2])
  }

  // `mode: primary` в OpenCode — оркестратор, а не субагент. Ключа `mode` у GigaCode нет, и
  // такой файл в agents/ стал бы обычным вызываемым субагентом. Оркестратор приходит на хост
  // командой в ~/.gigacode/commands/, поэтому здесь ему не место.
  if (fields.get('mode') === 'primary') return null

  const tools = [...GIGACODE_BASE_TOOLS]
  for (const [key, allowed] of Object.entries(permission)) {
    if (!allowed) continue
    for (const tool of GIGACODE_TOOLS_BY_PERMISSION[key] ?? []) {
      if (!tools.includes(tool)) tools.push(tool)
    }
  }

  const out = [`name: ${basename(file, '.md')}`]
  if (fields.has('description')) out.push(`description: ${yamlScalar(fields.get('description'))}`)
  if (fields.has('color')) out.push(`color: ${fields.get('color')}`)
  if (fields.get('temperature')) out.push('modelConfig:', `  temp: ${fields.get('temperature')}`)
  // Пустой `tools` у GigaCode означает «наследовать все инструменты», а не «никаких»,
  // поэтому список всегда непустой — в нём как минимум чтение.
  out.push('tools:', ...tools.map((tool) => `  - ${tool}`))

  return `---\n${out.join('\n')}\n---\n${body}`
}

// extras лежат как <skillDir>/extras/<kind>/<file>, поэтому каталог навыка выводится из пути
// файла, если источник собран не через collectSources (так делают тесты и внешние вызовы).
export function skillDirFor(src) {
  return src.skillDir ?? dirname(dirname(dirname(src.path)))
}

export function collectSources(skillsRoot) {
  const sources = []
  for (const skill of readdirSync(skillsRoot)) {
    if (skill.startsWith('.')) continue
    for (const kind of KINDS) {
      const dir = join(skillsRoot, skill, 'extras', kind)
      if (!existsSync(dir)) continue
      for (const file of readdirSync(dir)) {
        if (!file.endsWith('.md')) continue
        sources.push({ skill, kind, file, path: join(dir, file), skillDir: join(skillsRoot, skill) })
      }
    }
  }
  return sources.sort((a, b) =>
    `${a.skill}/${a.kind}/${a.file}`.localeCompare(`${b.skill}/${b.kind}/${b.file}`),
  )
}

export function findDuplicates(sources) {
  const seen = new Map()
  const duplicates = []
  for (const source of sources) {
    const key = `${source.kind}/${source.file}`
    if (seen.has(key)) duplicates.push({ key, skills: [seen.get(key), source.skill] })
    else seen.set(key, source.skill)
  }
  return duplicates
}

export const MANIFEST_REL = ['.config', 'pvm-guru', 'extras-manifest.json']

export function sha256(content) {
  return createHash('sha256').update(content).digest('hex')
}

export function readManifest(path) {
  if (!existsSync(path)) return { version: 1, files: {} }
  try {
    const data = JSON.parse(readFileSync(path, 'utf8'))
    return data && data.files && typeof data.files === 'object' ? data : { version: 1, files: {} }
  } catch {
    return { version: 1, files: {} }
  }
}

export function writeManifest(path, manifest) {
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, `${JSON.stringify(manifest, null, 2)}\n`)
}

export function planActions({ sources, hosts, manifest, force = false, allowRemove = true, retired = [] }) {
  const actions = { add: [], update: [], remove: [], skip: [], entries: {} }
  const expected = new Set()
  const scopedDirs = new Set()
  for (const host of hosts) {
    scopedDirs.add(host.commandsDir)
    if (host.agentsDir) scopedDirs.add(host.agentsDir)
  }
  for (const dir of retired) scopedDirs.add(dir)

  for (const host of hosts) {
    for (const src of sources) {
      const dir = src.kind === 'commands' ? host.commandsDir : host.agentsDir
      if (!dir) {
        actions.skip.push({ reason: 'unsupported', target: null, src, host: host.id })
        continue
      }

      // Хэш считается по отрендеренному тексту, а не по исходнику: на диск ложится именно он,
      // и сверка «файл правили вручную» должна сравнивать одно и то же с одним и тем же.
      // Рендер зависит от хоста, поэтому у одного и того же агента хэши на разных хостах разные.
      // Подстановка каталога навыка нужна обоим видам: агенты тоже несут {{SKILL_DIR}}, потому
      // что стартуют в своём cwd и относительный путь к скриптам у них ломается. Формат агента
      // приводится к хостовому уже поверх подставленного текста.
      const rendered = renderCommand(readFileSync(src.path, 'utf8'), skillDirFor(src))
      const content =
        src.kind === 'commands'
          ? rendered
          : renderAgent(rendered, host.agentFormat ?? 'opencode', src.file)
      // Пропуск по формату файла, а не по хосту: у хоста каталог агентов есть, но конкретный
      // агент ему не подходит. Причина отдельная от `unsupported`, где каталога агентов у хоста
      // нет вовсе.
      if (content === null) {
        actions.skip.push({ reason: 'host-format', target: null, src, host: host.id })
        continue
      }

      const target = join(dir, src.file)
      expected.add(target)
      const hash = sha256(content)
      const entry = { skill: src.skill, kind: src.kind, sha256: hash }
      const record = manifest.files[target]

      if (!existsSync(target)) {
        actions.add.push({ target, src, host: host.id, content })
        actions.entries[target] = entry
        continue
      }

      const onDisk = sha256(readFileSync(target))

      if (!record) {
        if (!force) {
          actions.skip.push({ reason: 'conflict', target, src, host: host.id })
          continue
        }
        actions.update.push({ target, src, host: host.id, content })
        actions.entries[target] = entry
        continue
      }

      if (onDisk !== record.sha256 && !force) {
        actions.skip.push({ reason: 'modified', target, src, host: host.id })
        actions.entries[target] = record
        continue
      }

      actions.entries[target] = entry
      if (onDisk === hash) continue

      actions.update.push({ target, src, host: host.id, content })
    }
  }

  for (const [target, record] of Object.entries(manifest.files)) {
    if (expected.has(target)) continue
    if (allowRemove && scopedDirs.has(dirname(target))) {
      actions.remove.push({ target, skill: record.skill })
    } else {
      actions.entries[target] = record
    }
  }

  return actions
}

export function applyActions(actions, { manifestPath, dryRun = false }) {
  if (dryRun) return

  for (const action of [...actions.add, ...actions.update]) {
    mkdirSync(dirname(action.target), { recursive: true })
    // Не copyFileSync: на диск идёт текст с подставленным каталогом навыка, см. renderCommand.
    writeFileSync(action.target, action.content ?? readFileSync(action.src.path, 'utf8'))
  }

  for (const action of actions.remove) {
    if (existsSync(action.target)) rmSync(action.target)
  }

  writeManifest(manifestPath, { version: 1, files: actions.entries })
}

// Про conflict скрипт знает ровно одно: файл на месте, а записи о нём в манифесте нет. Чей он —
// установлен прошлой версией скрипта, скопирован руками или действительно чужой — отсюда не
// видно, поэтому и сообщение не должно утверждать больше, чем известно. Формулировка «чужой
// файл» сбивала: под неё попадали наши же агенты, разложенные до того, как скрипт научился
// раскладывать их на этот хост.
const SKIP_REASONS = {
  conflict: 'файл на месте, но не из нашего манифеста (прошлая установка или чужой)',
  modified: 'файл изменён вручную',
}

// Про эти пропуски в сводке не пишем: они не про состояние диска, а про то, что этому хосту
// такой файл не полагается. `unsupported` проговаривается отдельно, строкой ниже в `main`.
const SILENT_SKIP_REASONS = new Set(['unsupported', 'host-format'])

export function formatSummary(actions) {
  const lines = []
  const section = (title, items, format) => {
    if (!items.length) return
    lines.push(`${title}: ${items.length}`)
    for (const item of items) lines.push(`  ${format(item)}`)
  }

  section('added', actions.add, (a) => a.target)
  section('updated', actions.update, (a) => a.target)
  section('removed', actions.remove, (a) => a.target)

  const skipped = actions.skip.filter((s) => !SILENT_SKIP_REASONS.has(s.reason))
  section('skipped', skipped, (s) => `${s.target} — ${SKIP_REASONS[s.reason]}`)

  if (!lines.length) lines.push('всё уже на месте, изменений нет')
  if (skipped.length) lines.push('', 'Перезаписать пропущенные: запустите install.mjs с флагом --force')

  return lines.join('\n')
}

const USAGE = `Раскладывает команды и субагентов скиллов PVM-GURU по установленным хостам.

  node install.mjs [флаги]

  --dry-run          показать план, ничего не менять
  --force            перезаписать чужие и вручную изменённые файлы
  --prune            удалить всё, что скрипт когда-либо установил
  --agent <список>   ограничить хосты: opencode,gigacode
  --help             эта справка`

export function parseArgs(argv) {
  // Неизвестный флаг — ошибка, а не игнорирование: `--updater` и `--no-updater`
  // из прежних версий сюда ещё приходят из чужих скриптов и памяти людей, и молча
  // проглоченный флаг создал бы уверенность, что регулярное обновление включено.
  const options = { dryRun: false, force: false, prune: false, agents: null, help: false }
  const knownHosts = HOSTS.map((h) => h.id)
  const knownHostsSet = new Set(knownHosts)

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--dry-run') options.dryRun = true
    else if (arg === '--force') options.force = true
    else if (arg === '--prune') options.prune = true
    else if (arg === '--help' || arg === '-h') options.help = true
    else if (arg === '--agent') {
      const value = argv[i + 1]
      // Значение, начинающееся с "--", на самом деле не значение, а следующий флаг:
      // это тот же случай, что и полное отсутствие значения.
      if (!value || value.startsWith('--')) throw new Error('флаг --agent требует список хостов')
      const list = value.split(',').map((s) => s.trim()).filter(Boolean)
      if (!list.length) throw new Error('флаг --agent требует список хостов')
      const unknown = list.filter((id) => !knownHostsSet.has(id))
      if (unknown.length) {
        throw new Error(
          `неизвестный хост в --agent: ${unknown.join(', ')} (допустимые: ${knownHosts.join(', ')})`,
        )
      }
      options.agents = list
      i += 1
    } else throw new Error(`неизвестный флаг: ${arg}`)
  }

  return options
}

export function main(argv = [], options = {}) {
  const {
    home = homedir(),
    selfDir = dirname(fileURLToPath(import.meta.url)),
    log = console.log,
    err = console.error,
    // Исполнитель команд планировщика. Подменяется тестами: набор не имеет права
    // регистрировать задачи в launchd машины, на которой его запустили.
    run = undefined,
    // Окружение для разрешения путей. Подменяется тестами по той же причине, что и
    // `home`: учётную запись менеджера при заданном XDG_STATE_HOME ищут не под домом,
    // и без подмены прогон правил бы настоящий файл разработчика мимо песочницы.
    env = process.env,
  } = options

  let opts
  try {
    opts = parseArgs(argv)
  } catch (error) {
    err(error.message)
    err(USAGE)
    return 1
  }

  if (opts.help) {
    log(USAGE)
    return 0
  }

  // Наследие снесённой подсистемы обновления и починка учётной записи менеджера
  // делаются РАНЬШЕ всего остального и не зависят ни от найденных хостов, ни от
  // содержимого манифеста. Так и было задумано, но стояло это ниже — и до правки
  // мимо проходили два живых случая: пользователь, у которого не стоит ни OpenCode,
  // ни GigaCode (`main` возвращал 1 на детекте хостов), и `--prune` при пустом
  // манифесте (возвращал 0 на первой же проверке). У обоих задача планировщика
  // оставалась заведённой.
  //
  // Путь в учётной записи под `--prune` не чинится намеренно (см. ветку ниже):
  // это снос установки, и приводить в порядок запись о том, что сейчас будет
  // удалено, незачем.
  if (!opts.dryRun) {
    const task = removeLegacyUpdateTask(home, { run })
    // Успех печатается только при подтверждённом снятии: иначе вывод одновременно
    // обещал бы «снята» и предупреждал, что она могла остаться.
    if (task.removed && task.cleared) {
      log(`снята регулярная задача обновления, заведённая прежней версией: ${task.path}`)
    }
    if (task.cleared) {
      const legacy = removeLegacyUpdaterFiles(home)
      if (legacy.removed.length) {
        log(`убрано наследие прежнего обновлятора: ${legacy.removed.length} путей`)
        for (const target of legacy.removed) log(`  ${target}`)
      }
      for (const warning of legacy.warnings) err(warning)
    } else if (legacyUpdaterPaths(home).some((path) => existsSync(path))) {
      // Шим и клон сносятся только когда задачи заведомо нет: живая задача,
      // оставшаяся из-за отказа планировщика, начала бы падать на удалённом шиме.
      // Предупреждение печатается только если этому наследию есть что терять —
      // на машине, где обновлятора никогда не было, оно было бы чистым шумом.
      err(`задача обновления ${LEGACY_UPDATE_TASK} могла остаться заведённой: ${task.reason} — шим и клон источника не тронуты`)
    }
  }

  const skillsRoot = dirname(selfDir)

  if (!opts.prune) {
    // Печатается всегда, когда что-то починили: файл принадлежит менеджеру скиллов,
    // и молча править чужую учётную запись нельзя.
    const lock = repairSkillLock(home, { dryRun: opts.dryRun, known: skillNames(skillsRoot), env })
    if (lock.repaired.length) {
      const tail = opts.dryRun ? ' (--dry-run: не записано)' : ''
      log(`учётная запись менеджера: путь к дереву навыков починен у ${lock.repaired.length} скиллов${tail}`)
    }
  }

  const manifestPath = join(home, ...MANIFEST_REL)
  const manifest = readManifest(manifestPath)

  if (opts.prune) {
    const entries = Object.entries(manifest.files)
    if (!entries.length) {
      log('манифест пуст, удалять нечего')
      return 0
    }

    // Модифицированному вручную файлу здесь даётся та же защита, что и install: без --force
    // ручная правка (skip modified) — повод файл не трогать, запись остаётся в манифесте, иначе
    // он потеряет владельца и перестанет быть виден install.mjs. Это не то же самое, что удаление
    // «источник исчез» в planActions: там правки не проверяются вообще — команда, убранная
    // апстримом, не должна жить дальше в виде отредактированной копии, поэтому файл удаляется
    // безусловно. Здесь же --prune — явная просьба пользователя всё снести, и для неё нужна та же
    // осторожность, что и при обычной установке.
    const removed = []
    const skipped = []
    const kept = {}
    for (const [target, record] of entries) {
      let modified = false
      if (existsSync(target)) {
        try {
          modified = sha256(readFileSync(target)) !== record.sha256
        } catch (error) {
          // Файл нечитаем (например, права доступа) — сверить хэш нельзя, значит нельзя и
          // поручиться, что он не правлен вручную. Тем же способом, что и подтверждённая ручная
          // правка: без --force не трогаем и оставляем запись в манифесте, а не роняем всю команду.
          err(`${target}: не удалось прочитать файл (${error.message}) — считаем изменённым, не трогаем`)
          modified = true
        }
      }
      if (modified && !opts.force) {
        skipped.push(target)
        kept[target] = record
        continue
      }
      removed.push(target)
      if (!opts.dryRun && existsSync(target)) rmSync(target)
    }

    if (!opts.dryRun) writeManifest(manifestPath, { version: 1, files: kept })
    if (opts.dryRun) log('--dry-run: изменения не применялись')
    log(`removed: ${removed.length}`)
    for (const target of removed) log(`  ${target}`)
    if (skipped.length) {
      log(`skipped: ${skipped.length} — файл изменён вручную`)
      for (const target of skipped) log(`  ${target}`)
      log('Удалить и такие файлы: запустите install.mjs --prune с флагом --force')
    }

    return 0
  }

  let sources
  try {
    sources = collectSources(skillsRoot)
  } catch (error) {
    err(`не удалось прочитать каталог скиллов ${skillsRoot}: ${error.message}`)
    return 1
  }

  if (!sources.length) {
    err(`не найдено ни одного каталога extras/ в ${skillsRoot}`)
    return 1
  }

  const duplicates = findDuplicates(sources)
  if (duplicates.length) {
    for (const duplicate of duplicates) {
      err(`дубликат ${duplicate.key} в скиллах: ${duplicate.skills.join(', ')}`)
    }
    return 1
  }

  const hosts = detectHosts(home, opts.agents)
  if (!hosts.length) {
    // Раскладывать некуда — но убрать разложенное снятыми хостами всё равно надо.
    // У пользователя, оставшегося только с Claude Code или Codex, детект пуст, а наши
    // файлы в их каталогах есть; пока уборка стояла за этим выходом, они оставались
    // навсегда, вместе с записями манифеста, где владельцем стоим мы.
    //
    // Удаление здесь безопасно независимо от полноты `sources`, ради которой ниже
    // заведён `allowRemove`: с пустым списком хостов `expected` пуст, а `scopedDirs`
    // состоит ровно из каталогов снятых хостов, и произвести запись в них не может
    // ни один источник. Записи в каталогах действующих хостов в зону не попадают.
    const retiredOnly = planActions({
      sources,
      hosts: [],
      manifest,
      force: opts.force,
      allowRemove: true,
      retired: retiredDirs(home),
    })
    applyActions(retiredOnly, { manifestPath, dryRun: opts.dryRun })
    if (retiredOnly.remove.length) log(formatSummary(retiredOnly))

    // Ненулевой код: раскладка не состоялась, ни одна команда не установлена.
    // Ноль здесь читался вызывающим скриптом как успешная установка, и человек
    // узнавал о том, что команд нет, только когда они не находились.
    err('не найден ни один установленный хост (проверьте ~/.config/opencode, ~/.gigacode)')
    return 1
  }

  // skillsRoot приходит из realpath'нутого import.meta.url (см. комментарий у точки входа внизу
  // файла), globalRoots — из home склейкой. Симлинк в $HOME (например /var → /private/var в
  // песочницах macOS) или на пути к каталогу хоста рассинхронизирует их, хотя это один и тот же
  // каталог, — поэтому обе стороны сравнения приводятся к разрешённому виду перед сравнением.
  const globalRoots = [join(home, '.agents', 'skills'), ...HOSTS.map((h) => join(home, ...h.skillsDir))].map(
    realpathOrSelf,
  )
  const isGlobalRoot = globalRoots.includes(realpathOrSelf(skillsRoot))
  if (!isGlobalRoot) {
    // Раз источники взяты не из общего для всех скиллов корня, набор sources может быть неполным
    // (например, только один скилл — как при запуске из проектного каталога). Фаза удаления в
    // planActions судит об «исчезнувшем» файле по этому набору, поэтому здесь она отключается:
    // иначе можно стереть extras скиллов, установленных откуда-то ещё.
    log(
      `ВНИМАНИЕ: источники взяты из ${skillsRoot}, а раскладка идёт в глобальные каталоги` +
        ' — удаление пропавших записей манифеста пропущено',
    )
  }

  let actions
  try {
    actions = planActions({ sources, hosts, manifest, force: opts.force, allowRemove: isGlobalRoot, retired: retiredDirs(home) })
    applyActions(actions, { manifestPath, dryRun: opts.dryRun })
  } catch (error) {
    err(`ошибка при раскладке: ${error.message}`)
    return 1
  }

  // Ветка защитная: оба целевых хоста каталог агентов задают, поэтому в штатной раскладке
  // сюда не попадают. Сообщение собирается из самого факта, а не из поля записи HOSTS: прежде
  // оно бралось из `agentsSkipReason`, который уехал вместе с Claude Code и Codex, и печаталось
  // бы как `undefined` — ровно тот класс, ради которого рядом стоит тест на печать undefined.
  for (const id of new Set(actions.skip.filter((s) => s.reason === 'unsupported').map((s) => s.host))) {
    log(`${id}: каталог субагентов у хоста не задан — агенты пропущены`)
  }

  log(`хосты: ${hosts.map((h) => h.id).join(', ')}`)
  if (opts.dryRun) log('--dry-run: изменения не применялись')
  log(formatSummary(actions))

  return 0
}

// import.meta.url в ESM всегда отдаёт разрешённый (симлинки раскрыты) путь, а process.argv[1] —
// путь ровно таким, каким его передали в `node`. При штатной установке каталог скилла обычно сам
// является симлинком (например ~/.config/opencode/skills/pvm-guru → ~/.agents/skills/pvm-guru),
// поэтому сравнивать их напрямую нельзя — оба пути нужно сначала привести к разрешённому виду.
const entryArg = process.argv[1]
const entryPath = entryArg && existsSync(entryArg) ? realpathSync(entryArg) : null
if (entryPath === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)))
}
