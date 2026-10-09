"""Dead-code guard: every module-level function and class is used by production code.

A function that no production line names is dead weight that still looks live: it is read,
edited and kept passing in tests, and it silently loses behaviour when nothing exercises
it (commit 6b17d58). The scan finds each module-level function and class in ``counties/``
and ``taxprotest/`` (tests and migrations excluded) whose name no other production line
refers to. Use by a test is no use: that is the point of the guard.

A reference is any of these, found with ``ast`` in production code, ``manage.py`` and
``scripts/``: a ``Name``; an ``Attribute``; a name an ``import`` or ``from ... import``
brings in; or an identifier inside a string constant, so dynamic dispatch, a Celery task
name, a URL or a dotted-path setting all keep their target alive. Migrations count as
references even though they are not scanned for definitions: they name functions in
``default=``, ``upload_to=`` and ``RunPython``, and deleting one of those breaks ``migrate``.
Not references: the definition itself (its decorators, body, docstring and recursion), an
``__all__`` list (a re-export is not a use), and every test module.

Exempt by rule, because Django, Celery or pytest finds them without a reference:
- dunder names;
- a definition decorated with a registration that runs on import (``shared_task``,
  ``app.task``, ``receiver``, template-tag registration, ``admin.register``, a pytest
  fixture); a plain wrapper such as ``functools.wraps``, ``lru_cache``,
  ``contextmanager`` or ``dataclass`` registers nothing and is not exempt;
- a ``Command`` class in a ``management/commands/`` directory;
- a Django model, ``ModelAdmin``, admin inline or ``AppConfig`` class, and a class derived
  from one defined in production code. A ``Meta`` class is nested in its model or form, so
  a scan of module-level definitions never sees it.

Limits, deliberately not worked around:
- Matching is by name across the whole repository, not by import: a dead ``run`` is masked
  by any live ``run`` or by any ``.run`` attribute.
- A function referenced only by another dead function is not reported. Delete the dead
  caller and the scan then reports the callee.
- Only direct children of a module are scanned: a nested function, a method, and a
  definition under a top-level ``if TYPE_CHECKING:`` or ``try:`` are not.
- A name inside a prose string or a docstring of another definition counts as a reference.
- An import counts, so a name re-exported from a package ``__init__`` is referenced even
  when only tests use the re-export.
- A computed name (``getattr(module, f"handle_{kind}")``) creates no reference.

The baseline below lists the dead definitions that predate this guard. It must match the
scan exactly: a new dead definition fails, and so does an entry whose definition was
deleted or came back into use. The list may only shrink.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("counties", "taxprotest")

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

IDENTIFIER = re.compile(r"[A-Za-z_]\w*")

COMMAND_DIRECTORY = ("management", "commands")

# Decorators that register the definition when the module is imported, so nothing has to
# name it. Matched on the dotted name of the decorator or of its call.
REGISTERING_DECORATORS = frozenset(
    {
        "shared_task",  # Celery finds the task by its registered name
        "app.task",  # Celery finds the task by its registered name
        "receiver",  # the signal dispatcher holds the handler
        "register.filter",  # template tag library: the template engine finds it by name
        "register.simple_tag",
        "register.inclusion_tag",
        "register.tag",
        "admin.register",  # the admin site holds the registered ModelAdmin
        "pytest.fixture",  # pytest injects the fixture by name
        "fixture",
    }
)

# Django base classes whose subclasses are loaded by location or by registry, not by name.
FRAMEWORK_BASES = frozenset(
    {
        "Model",  # tables, found through the app registry
        "ModelAdmin",  # registered with the admin site
        "TabularInline",  # attached to a ModelAdmin
        "StackedInline",
        "AppConfig",  # found through INSTALLED_APPS
    }
)


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return "tests" in pure.parts or pure.name.startswith("test_") or pure.name == "conftest.py"


def _is_definition_source(path: str) -> bool:
    """Production code whose definitions the scan requires to be used."""
    pure = PurePosixPath(path)
    return pure.parts[0] in SCANNED_PACKAGES and "migrations" not in pure.parts


def _identifiers(text: str) -> Iterator[str]:
    yield from IDENTIFIER.findall(text)


def _all_exports(tree: ast.Module) -> set[int]:
    """Ids of the nodes inside an ``__all__`` assignment: a re-export is not a use."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, (ast.AugAssign, ast.AnnAssign))
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value = node.value
        if value is not None:
            skipped.update(id(inner) for inner in ast.walk(value))
    return skipped


def _references(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """Every ``(name, line)`` the module refers to, outside ``__all__``."""
    skipped = _all_exports(tree)
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.alias):
            yield from ((name, node.lineno) for name in _identifiers(node.name))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield from ((name, node.lineno) for name in _identifiers(node.value))


def _module_definitions(tree: ast.Module) -> Iterator[Definition]:
    for node in tree.body:
        if isinstance(node, DEFINITIONS):
            yield node


def _span(node: Definition) -> tuple[int, int]:
    """First to last line of the definition, decorators included."""
    first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    return first, node.end_lineno or node.lineno


def _dotted_name(node: ast.expr) -> str:
    """``a.b.c`` for a name or attribute chain, looking through a call: ``@a.b(...)``."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _last_name(node: ast.expr) -> str:
    return _dotted_name(node).rpartition(".")[2]


def _framework_classes(trees: Mapping[str, ast.Module]) -> set[str]:
    """Names of the production classes derived, however deep, from a framework base."""
    classes = [
        node
        for path, tree in trees.items()
        if _is_definition_source(path)
        for node in _module_definitions(tree)
        if isinstance(node, ast.ClassDef)
    ]
    exempt = set(FRAMEWORK_BASES)
    grown = True
    while grown:
        grown = False
        for node in classes:
            if node.name not in exempt and any(_last_name(base) in exempt for base in node.bases):
                exempt.add(node.name)
                grown = True
    return exempt


def _is_exempt(path: str, node: Definition, framework_classes: set[str]) -> bool:
    if node.name.startswith("__") and node.name.endswith("__"):
        return True  # the interpreter calls dunders by protocol, e.g. a module __getattr__
    if any(_dotted_name(decorator) in REGISTERING_DECORATORS for decorator in node.decorator_list):
        return True
    if not isinstance(node, ast.ClassDef):
        return False
    if node.name == "Command" and PurePosixPath(path).parent.parts[-2:] == COMMAND_DIRECTORY:
        return True  # Django finds a management command by file location
    return any(_last_name(base) in framework_classes for base in node.bases)


def orphan_definitions(sources: Mapping[str, str]) -> list[str]:
    """``path::name`` of each definition no production line references.

    ``sources`` maps a posix path relative to the repository to its text. Test modules are
    dropped first, so they are neither references nor candidates.
    """
    trees = {path: ast.parse(source) for path, source in sources.items() if not _is_test_path(path)}
    references: dict[str, list[tuple[str, int]]] = {}
    for path, tree in trees.items():
        for name, line in _references(tree):
            references.setdefault(name, []).append((path, line))

    framework_classes = _framework_classes(trees)
    orphans: list[str] = []
    for path, tree in trees.items():
        if not _is_definition_source(path):
            continue
        for node in _module_definitions(tree):
            if _is_exempt(path, node, framework_classes):
                continue
            first, last = _span(node)
            used = any(
                other != path or not first <= line <= last
                for other, line in references.get(node.name, ())
            )
            if not used:
                orphans.append(f"{path}::{node.name}")
    return sorted(orphans)


def production_sources() -> dict[str, str]:
    """Every Python file the scan reads, keyed by its path relative to the repository."""
    paths = [
        ROOT / "manage.py",
        *sorted((ROOT / "scripts").rglob("*.py")),
        *(path for package in SCANNED_PACKAGES for path in sorted((ROOT / package).rglob("*.py"))),
    ]
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8-sig") for path in paths
    }


# Dead definitions that predate this guard, each with the reason it stays for now. The
# scan must match this list exactly; delete an entry with its definition.
BASELINE: dict[str, str] = {
    "counties/brazos/parsers/pacs.py::parse_entity_line": (
        "No production caller; only test_parsers.py calls it. Delete it with its test."
    ),
    "counties/common/county_registry.py::isolated_registrations": (
        "A test seam kept in a production module; only tests (fake_county.py) call it. "
        "Move it beside the fake county or delete it."
    ),
    "counties/common/tax_evaluation.py::evaluate_tax_impact": (
        "No production caller; only its own __all__ entry and test_tax_evaluation.py name it. "
        "Delete it, its __all__ entry and its tests."
    ),
    "counties/harris/etl_pipeline/extract.py::ArchiveValidationError": (
        "Never raised or caught anywhere, tests included. Delete it."
    ),
    "counties/harris/etl_pipeline/fixtures_aggregator.py::update_building_room_counts": (
        "No production caller; only tests call it. Wire it into the import or delete it "
        "with its tests."
    ),
    "counties/harris/etl_pipeline/transform.py::get_schema": (
        "Never called anywhere, tests included. Delete it."
    ),
    "counties/harris/etl_pipeline/translated_loader.py::load_property_file": (
        "The loader module is itself an orphan (only test_property_persistence.py imports "
        "it). Delete the module and this entry."
    ),
    "counties/harris/management/commands/load_gis_data.py::find_preferred_shapefile": (
        "The command does not call it; only test_load_gis_data.py does. Use it in the "
        "command or delete it with its tests."
    ),
    "counties/harris/tasks_new.py::candidate_cama_years": (
        "Never called anywhere, tests included. Delete it."
    ),
    "taxprotest/runtime_paths.py::resolve_from_base": (
        "Never called anywhere, tests included. Delete it."
    ),
}


class OrphanScannerTests(SimpleTestCase):
    """The scanner reports unreferenced definitions and follows every kind of reference."""

    def test_a_function_and_a_class_nothing_references_are_orphans(self):
        sources = {
            "counties/app/logic.py": "def unused():\n    pass\n\nclass Unused:\n    pass\n",
        }

        self.assertEqual(
            orphan_definitions(sources),
            ["counties/app/logic.py::Unused", "counties/app/logic.py::unused"],
        )

    def test_a_name_an_attribute_and_an_import_each_reference_a_definition(self):
        sources = {
            "counties/app/logic.py": (
                "def by_name():\n    pass\n"
                "def by_attribute():\n    pass\n"
                "def by_import():\n    pass\n"
                "def by_aliased_import():\n    pass\n"
                "def unused():\n    pass\n"
            ),
            "counties/app/views.py": (
                "from counties.app.logic import by_import, by_aliased_import as other\n"
                "from counties.app import logic\n"
                "by_name()\n"
                "logic.by_attribute()\n"
            ),
        }

        self.assertEqual(orphan_definitions(sources), ["counties/app/logic.py::unused"])

    def test_an_identifier_in_a_string_is_a_reference(self):
        sources = {
            "counties/app/logic.py": (
                "def dispatched():\n    pass\n"
                "def scheduled():\n    pass\n"
                "def in_a_dotted_path():\n    pass\n"
                "def unused():\n    pass\n"
            ),
            "taxprotest/settings.py": (
                'HANDLER = getattr(logic, "dispatched")\n'
                'BEAT = {"task": "counties.app.logic.scheduled"}\n'
                'HOOK = "counties.app.logic:in_a_dotted_path"\n'
            ),
        }

        self.assertEqual(orphan_definitions(sources), ["counties/app/logic.py::unused"])

    def test_an_all_list_is_not_a_reference(self):
        sources = {
            "counties/app/logic.py": (
                "def listed():\n    pass\n"
                "def added():\n    pass\n"
                "def annotated():\n    pass\n"
                "class Assigned:\n    pass\n"
            ),
            "counties/app/__init__.py": (
                '__all__ = ["listed", "Assigned"]\n'
                '__all__ += ["added"]\n'
                '__all__: list[str] = ["annotated"]\n'
            ),
        }

        self.assertEqual(
            orphan_definitions(sources),
            [
                "counties/app/logic.py::Assigned",
                "counties/app/logic.py::added",
                "counties/app/logic.py::annotated",
                "counties/app/logic.py::listed",
            ],
        )

    def test_a_test_that_uses_a_definition_does_not_keep_it_alive(self):
        sources = {
            "counties/app/logic.py": "def only_tested():\n    pass\n",
            "counties/app/tests/test_logic.py": (
                "from counties.app.logic import only_tested\nonly_tested()\n"
            ),
            "counties/app/test_stray.py": 'NAME = "only_tested"\n',
            "counties/app/conftest.py": "only_tested()\n",
        }

        self.assertEqual(orphan_definitions(sources), ["counties/app/logic.py::only_tested"])

    def test_tests_and_migrations_are_not_scanned_for_definitions(self):
        sources = {
            "counties/app/tests/test_logic.py": "def helper():\n    pass\n",
            "counties/app/tests/support.py": "class Fake:\n    pass\n",
            "counties/app/conftest.py": "def fixture_like():\n    pass\n",
            "counties/app/migrations/0001_initial.py": "def forwards(apps, schema_editor):\n    pass\n",
            "scripts/tool.py": "def helper():\n    pass\n",
            "manage.py": "def main():\n    pass\n",
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_migrations_manage_and_scripts_reference_what_they_name(self):
        sources = {
            "counties/app/logic.py": (
                "def upload_path():\n    pass\n"
                "def forward():\n    pass\n"
                "def from_script():\n    pass\n"
                "def from_manage():\n    pass\n"
                "def unused():\n    pass\n"
            ),
            "counties/app/migrations/0001_initial.py": (
                "from counties.app.logic import forward\n"
                'FIELD = {"upload_to": "counties.app.logic.upload_path"}\n'
                "RunPython(forward)\n"
            ),
            "scripts/manual/check.py": "from counties.app.logic import from_script\n",
            "manage.py": "from counties.app.logic import from_manage\n",
        }

        self.assertEqual(orphan_definitions(sources), ["counties/app/logic.py::unused"])

    def test_a_use_elsewhere_in_the_same_module_counts(self):
        sources = {
            "counties/app/logic.py": (
                "def helper():\n    pass\n" "def caller():\n    helper()\n" "CALLBACK = caller\n"
            ),
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_the_definition_itself_is_not_a_reference(self):
        sources = {
            "counties/app/logic.py": (
                "import functools\n"
                "def recursive(n):\n"
                '    """Calls recursive until n runs out."""\n'
                "    return recursive(n - 1) if n else 0\n"
                "@functools.cache\n"
                "def cached():\n"
                "    return cached.__name__\n"
                "class Node:\n"
                "    def clone(self):\n"
                "        return Node()\n"
                "    parent: Node | None = None\n"
            ),
        }

        self.assertEqual(
            orphan_definitions(sources),
            [
                "counties/app/logic.py::Node",
                "counties/app/logic.py::cached",
                "counties/app/logic.py::recursive",
            ],
        )

    def test_a_reference_in_another_module_counts_even_on_the_same_lines(self):
        sources = {
            "counties/app/logic.py": "def used():\n    pass\n",
            "counties/app/other.py": "used()\n",
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_a_name_defined_in_one_module_is_masked_by_any_use_of_that_name(self):
        # Matching is by name, not by import: a known limit, recorded so a change is deliberate.
        sources = {
            "counties/a/logic.py": "def run():\n    pass\n",
            "counties/b/logic.py": "def run():\n    pass\n",
            "counties/b/views.py": "from counties.b.logic import run\nrun()\n",
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_only_definitions_directly_in_a_module_are_scanned(self):
        sources = {
            "counties/app/logic.py": (
                "from typing import TYPE_CHECKING\n"
                "class Holder:\n"
                "    def method(self):\n"
                "        def nested():\n"
                "            pass\n"
                "if TYPE_CHECKING:\n"
                "    def conditional():\n"
                "        pass\n"
                "Holder()\n"
            ),
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_an_async_function_is_scanned_like_a_function(self):
        sources = {"counties/app/logic.py": "async def fetch():\n    pass\n"}

        self.assertEqual(orphan_definitions(sources), ["counties/app/logic.py::fetch"])

    def test_dunder_names_are_exempt(self):
        sources = {"counties/app/logic.py": "def __getattr__(name):\n    pass\n"}

        self.assertEqual(orphan_definitions(sources), [])

    def test_definitions_registered_by_a_decorator_are_exempt(self):
        sources = {
            "counties/app/tasks.py": (
                "@shared_task(bind=True)\ndef shared(self):\n    pass\n"
                "@app.task\ndef celery_task():\n    pass\n"
                "@receiver(post_save)\ndef on_save(sender, **kwargs):\n    pass\n"
                "@pytest.fixture\ndef fixture_one():\n    pass\n"
                "@fixture(scope='session')\ndef fixture_two():\n    pass\n"
                "@admin.register(Thing)\nclass ThingAdmin:\n    pass\n"
            ),
            "counties/app/templatetags/app_tags.py": (
                "@register.filter\ndef a_filter(value):\n    pass\n"
                "@register.simple_tag\ndef a_tag():\n    pass\n"
                '@register.inclusion_tag("x.html")\ndef an_inclusion():\n    pass\n'
                '@register.tag("y")\ndef a_block_tag(parser, token):\n    pass\n'
            ),
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_a_plain_wrapper_decorator_registers_nothing_and_is_not_exempt(self):
        sources = {
            "counties/app/logic.py": (
                "@wraps(view)\ndef wrapped():\n    pass\n"
                "@functools.wraps(view)\ndef qualified_wrap():\n    pass\n"
                "@lru_cache(maxsize=None)\ndef cached():\n    pass\n"
                "@contextmanager\ndef managed():\n    yield\n"
                "@dataclass(frozen=True)\nclass Value:\n    pass\n"
                "@other.register_not\ndef lookalike():\n    pass\n"
            ),
        }

        self.assertEqual(
            orphan_definitions(sources),
            [
                "counties/app/logic.py::Value",
                "counties/app/logic.py::cached",
                "counties/app/logic.py::lookalike",
                "counties/app/logic.py::managed",
                "counties/app/logic.py::qualified_wrap",
                "counties/app/logic.py::wrapped",
            ],
        )

    def test_a_command_class_is_exempt_only_under_management_commands(self):
        sources = {
            "counties/app/management/commands/load.py": (
                "class Command(BaseCommand):\n    pass\n"
                "class Helper:\n    pass\n"
                "def helper_function():\n    pass\n"
            ),
            "counties/app/command.py": "class Command:\n    pass\n",
        }

        self.assertEqual(
            orphan_definitions(sources),
            [
                "counties/app/command.py::Command",
                "counties/app/management/commands/load.py::Helper",
                "counties/app/management/commands/load.py::helper_function",
            ],
        )

    def test_django_model_admin_inline_and_app_config_classes_are_exempt(self):
        sources = {
            "counties/app/models.py": (
                "class Parcel(models.Model):\n    pass\n" "class Owner(Model):\n    pass\n"
            ),
            "counties/app/admin.py": (
                "class ParcelAdmin(admin.ModelAdmin):\n    pass\n"
                "class TabularPart(admin.TabularInline):\n    pass\n"
                "class StackedPart(StackedInline):\n    pass\n"
            ),
            "counties/app/apps.py": "class AppConfigForThis(AppConfig):\n    pass\n",
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_a_class_derived_from_an_exempt_class_defined_in_production_is_exempt(self):
        sources = {
            "counties/common/models.py": "class Base(models.Model):\n    pass\n",
            "counties/app/models.py": (
                "from counties.common.models import Base\n"
                "class Concrete(Base):\n    pass\n"
                "class Deeper(Concrete):\n    pass\n"
            ),
        }

        self.assertEqual(orphan_definitions(sources), [])

    def test_other_classes_must_be_referenced_even_when_django_or_typing_types(self):
        sources = {
            "counties/app/logic.py": (
                "class Choice(models.TextChoices):\n    pass\n"
                "class Entry(forms.Form):\n    pass\n"
                "class Port(Protocol):\n    pass\n"
                "class Kind(StrEnum):\n    pass\n"
                "class Failure(Exception):\n    pass\n"
            ),
        }

        self.assertEqual(
            orphan_definitions(sources),
            [
                "counties/app/logic.py::Choice",
                "counties/app/logic.py::Entry",
                "counties/app/logic.py::Failure",
                "counties/app/logic.py::Kind",
                "counties/app/logic.py::Port",
            ],
        )


class NoOrphanDefinitionsTests(SimpleTestCase):
    def test_orphan_definitions_are_exactly_the_baseline(self):
        found = set(orphan_definitions(production_sources()))

        new = sorted(found - BASELINE.keys())
        stale = sorted(BASELINE.keys() - found)
        problems = [
            f"{key.split('::')[0]}: `{key.split('::')[1]}` has no production reference. "
            "Delete it, or reference it from production code "
            "(a test or an __all__ entry does not count)."
            for key in new
        ] + [
            f"{key.split('::')[0]}: `{key.split('::')[1]}` is in BASELINE but is no longer "
            "an orphan. Remove the entry from BASELINE."
            for key in stale
        ]
        if problems:
            self.fail("\n".join(problems))
