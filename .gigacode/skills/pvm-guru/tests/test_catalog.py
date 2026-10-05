"""Реестр семейства: полон, описан и не ссылается на несуществующее."""

import re
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
SKILLS_ROOT = SKILL.parent
sys.path.insert(0, str(SCRIPTS))

import guru_catalog as gc  # noqa: E402

# Навыки, которые лежат рядом, но семейством не являются: чужие или служебные.
# Список ведётся руками, чтобы новый навык нельзя было забыть молча.
NOT_FAMILY = {
    "architecture-diagram-skill", "atomic", "example-skill", "frontend-component-analyzer",
    "frontend-design", "get_zephyr_testcase", "gigacode-skill-manager", "git-commit-message",
    "mcp-atlassian", "python-docs", "spring-boot-unit-tests",
    # Самостоятельный навык: чужие запросы не перехватывает и в цепочки семейства
    # не встаёт. Тот же список исключений держит tools/check-family-matrix.mjs.
    "abyss-release",
}


class RegistryTest(unittest.TestCase):
    def test_family_derives_from_info(self):
        """Список и описания не могут разойтись: один выводится из другого."""
        self.assertEqual(gc.FAMILY, tuple(gc.FAMILY_INFO))

    def test_pvm_guru_leads(self):
        self.assertEqual(gc.FAMILY[0], "pvm-guru")

    def test_no_duplicates(self):
        self.assertEqual(len(set(gc.FAMILY)), len(gc.FAMILY))

    def test_every_entry_is_described(self):
        for name, info in gc.FAMILY_INFO.items():
            for field in ("purpose", "input", "output", "owner"):
                self.assertTrue(info.get(field), f"{name}: не заполнено поле {field}")

    def test_every_family_skill_exists_on_disk(self):
        """Запись о навыке, которого ещё нет, отправит человека ставить пустоту."""
        for name in gc.FAMILY:
            self.assertTrue(
                (SKILLS_ROOT / name / "SKILL.md").is_file(),
                f"{name} есть в реестре, но каталога навыка нет",
            )

    def test_every_neighbour_is_classified(self):
        """Навык рядом либо в семействе, либо явно вынесен за него."""
        present = {p.parent.name for p in SKILLS_ROOT.glob("*/SKILL.md")}
        unclassified = sorted(present - set(gc.FAMILY) - NOT_FAMILY)
        self.assertEqual(
            unclassified, [],
            "навыки не отнесены ни к семейству, ни к исключениям: добавь в FAMILY_INFO "
            "или в NOT_FAMILY этого теста",
        )

    def test_openspec_chain_is_in_the_registry(self):
        for name in ("guru-openspec", "guru-context", "guru-design", "plan", "exec"):
            self.assertIn(name, gc.FAMILY, f"{name} не в реестре — /guru-start о нём не узнает")

    def test_install_hint_names_the_skill(self):
        for name in gc.FAMILY:
            hint = gc.install_hint(name)
            self.assertIn(f"--skill {name}", hint)
            self.assertIn(gc.FAMILY_REPO, hint)


class RolesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (SKILL / "references" / "onboarding-roles.md").read_text(encoding="utf-8")

    def test_no_dead_links(self):
        """Ссылка на файл, которого нет в репозитории, — обещание, которое не выполнить."""
        for link in re.findall(r"\]\((?!https?://)([^)#]+)\)", self.text):
            candidate = (SKILL / link) if not link.startswith("/") else Path(link)
            repo_root = SKILL.parents[2]
            self.assertTrue(
                candidate.exists() or (repo_root / link).exists(),
                f"битая ссылка: {link}",
            )

    def test_rollout_document_is_not_referenced_as_a_file(self):
        self.assertNotIn("docs/rollout/", self.text)

    def test_chains_named_for_three_roles(self):
        for chain in ("Цепочка на изменение", "Цепочка на готовом изменении", "С чего начать команде"):
            self.assertIn(chain, self.text, f"нет цепочки: {chain}")

    def test_new_skills_reachable_from_roles(self):
        for name in ("guru-design", "guru-context", "guru-openspec"):
            self.assertIn(f"`{name}`", self.text, f"{name} не назван ни в одной роли")

    def test_every_named_skill_belongs_to_the_registry(self):
        allowed = set(gc.FAMILY) | {"python-docs"}
        named = set(re.findall(r"`([a-z][a-z0-9-]{3,})`", self.text))
        cli_tokens = {t for t in named if "_" in t or "." in t or "/" in t}
        unknown = sorted(named - allowed - cli_tokens - {"similar", "skills", "assigned"})
        self.assertEqual(unknown, [], "карта ролей называет навыки вне реестра")


class ScenarioTest(unittest.TestCase):
    """Новые сценарии присутствуют и в таблице роутинга, и в справочнике."""

    @classmethod
    def setUpClass(cls):
        cls.skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        cls.scenarios = (SKILL / "references" / "usage-scenarios.md").read_text(encoding="utf-8")

    def test_routing_rows(self):
        self.assertIn("| 18 |", self.skill)
        self.assertIn("| 19 |", self.skill)

    def test_reference_sections(self):
        self.assertIn("## 18. Заготовка proposal", self.scenarios)
        self.assertIn("## 19. Создать Pull Request", self.scenarios)

    def test_every_routing_script_exists(self):
        for name in sorted(set(re.findall(r"`([a-z_]+\.py)", self.skill))):
            self.assertTrue((SCRIPTS / name).is_file(), f"роутинг ссылается на {name}, файла нет")

    def test_write_scenario_marked_as_such(self):
        row = next(line for line in self.skill.splitlines() if line.startswith("| 19 |"))
        self.assertIn("--execute", row, "сценарий записи не помечен как write")


if __name__ == "__main__":
    unittest.main()
