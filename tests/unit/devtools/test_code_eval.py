"""Tests for azure_vm.devtools.code_eval."""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import sys
from pathlib import Path

import pytest

from azure_vm.devtools import code_eval
from azure_vm.devtools.code_eval import (
    ALL_CHECKS,
    EXCLUDED_MODULES,
    ROOT_PACKAGE,
    ClassMetrics,
    Smell,
    _check_ast_heuristic,
    _check_bare_except,
    _check_broad_except,
    _check_circular_deps,
    _check_circular_deps_cached,
    _check_duplicated_run_method,
    _check_god_classes,
    _check_high_coupling,
    _check_high_coupling_cached,
    _check_init_exports_internal,
    _check_large_functions,
    _check_module_size,
    _check_module_size_cached,
    _check_mutable_defaults,
    _check_raise_without_from,
    _check_unused_exception_classes,
    _check_unused_exceptions,
    format_report,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class _StubGraph:
    """Minimal stand-in for a grimp import graph."""

    def __init__(self, modules=(), imports=None, imported_by=None, cycles=None):
        self.modules = set(modules)
        self._imports = imports or {}
        self._imported_by = imported_by or {}
        self._cycles = cycles or []

    def find_modules_directly_imported_by(self, module):
        return set(self._imports.get(module, ()))

    def find_modules_that_directly_import(self, module):
        return set(self._imported_by.get(module, ()))

    def find_cycles(self):
        return self._cycles


class _RaisingGraph(_StubGraph):
    def find_cycles(self):
        raise RuntimeError("grimp exploded")


@contextlib.contextmanager
def _fake_azure_vm_package(
    tmp_path: Path, init_source: str, extra_files: dict[str, str] | None = None
):
    """Import a throwaway azure_vm package from tmp_path for one test."""
    prefix = f"{ROOT_PACKAGE}."
    pkg = tmp_path / ROOT_PACKAGE
    pkg.mkdir()
    (pkg / "__init__.py").write_text(init_source)
    for rel, text in (extra_files or {}).items():
        _write(pkg, rel, text)

    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == ROOT_PACKAGE or name.startswith(prefix)
    }
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(tmp_path))
    try:
        yield
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == ROOT_PACKAGE or name.startswith(prefix):
                del sys.modules[name]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_smell_is_a_value_object():
    one = Smell(category="bug", severity="high", file="a.py", line=7, message="boom")
    two = Smell(category="bug", severity="high", file="a.py", line=7, message="boom")
    assert one == two
    assert (one.category, one.severity, one.file, one.line, one.message) == (
        "bug",
        "high",
        "a.py",
        7,
        "boom",
    )


def test_class_metrics_stores_measurements():
    metrics = ClassMetrics(
        name="Thing",
        file="src/azure_vm/thing.py",
        lines=12,
        public_methods=3,
        total_methods=5,
        fan_out=2,
        responsibilities=["a", "b", "c"],
    )
    assert metrics.name == "Thing"
    assert metrics.public_methods == 3
    assert metrics.fan_out == 2
    assert metrics.responsibilities == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# AST heuristics that take a parsed tree directly
# ---------------------------------------------------------------------------


def test_check_bare_except_flags_bare_handler():
    source = "try:\n    pass\nexcept:\n    pass\n"
    smells = _check_bare_except("src/azure_vm/m.py", ast.parse(source), source)
    assert smells == [
        Smell(
            category="bug",
            severity="high",
            file="src/azure_vm/m.py",
            line=3,
            message="Bare except: — catches KeyboardInterrupt and SystemExit",
        )
    ]


def test_check_bare_except_ignores_typed_handlers():
    source = (
        "try:\n"
        "    pass\n"
        "except ValueError:\n"
        "    pass\n"
        "except (TypeError, KeyError):\n"
        "    pass\n"
    )
    assert _check_bare_except("m.py", ast.parse(source), source) == []


def test_check_broad_except_flags_exception_and_baseexception():
    source = (
        "try:\n"
        "    pass\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    pass\n"
        "except BaseException:\n"
        "    pass\n"
    )
    smells = _check_broad_except("m.py", ast.parse(source), source)
    assert [s.line for s in smells] == [3, 7]
    assert [s.message for s in smells] == [
        "Broad except clause catches Exception",
        "Broad except clause catches BaseException",
    ]
    assert {s.category for s in smells} == {"bug"}
    assert {s.severity for s in smells} == {"medium"}


def test_check_broad_except_ignores_narrow_and_tuple_handlers():
    source = (
        "try:\n"
        "    pass\n"
        "except ValueError:\n"
        "    pass\n"
        "try:\n"
        "    pass\n"
        "except (ValueError, Exception):\n"
        "    pass\n"
    )
    assert _check_broad_except("m.py", ast.parse(source), source) == []


def test_check_raise_without_from_flags_raise_in_handler():
    source = "try:\n    pass\nexcept ValueError:\n    raise RuntimeError('x')\n"
    smells = _check_raise_without_from("m.py", ast.parse(source), source)
    assert smells == [
        Smell(
            category="bug",
            severity="medium",
            file="m.py",
            line=4,
            message="Raise inside except without `from` — exception chain is lost",
        )
    ]


def test_check_raise_without_from_accepts_chained_and_bare_raises():
    source = (
        "try:\n"
        "    pass\n"
        "except ValueError as err:\n"
        "    raise RuntimeError('x') from err\n"
        "try:\n"
        "    pass\n"
        "except ValueError:\n"
        "    raise\n"
    )
    assert _check_raise_without_from("m.py", ast.parse(source), source) == []


def test_check_raise_without_from_ignores_raises_outside_handlers():
    source = "def f():\n    raise RuntimeError('x')\n"
    assert _check_raise_without_from("m.py", ast.parse(source), source) == []


def test_check_large_functions_threshold_is_strict():
    source = "def big():\n" + "    pass\n" * 5
    assert _check_large_functions("m.py", ast.parse(source), source, max_loc=6) == []
    smells = _check_large_functions("m.py", ast.parse(source), source, max_loc=5)
    assert smells == [
        Smell(
            category="simplification",
            severity="medium",
            file="m.py",
            line=1,
            message="Function `big()` is 6 lines (max recommended: 5)",
        )
    ]


def test_check_large_functions_covers_async_functions():
    source = "async def fetch():\n" + "    pass\n" * 9
    smells = _check_large_functions("m.py", ast.parse(source), source, max_loc=5)
    assert len(smells) == 1
    assert smells[0].message == "Function `fetch()` is 10 lines (max recommended: 5)"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _check_mutable_defaults requires the default to be an ast.Constant "
        "as well as an ast.List/Dict/Set, which no node can be, so it never fires"
    ),
)
def test_check_mutable_defaults_flags_list_default():
    source = "def f(items=[]):\n    return items\n"
    smells = _check_mutable_defaults("m.py", ast.parse(source), source)
    assert smells == [
        Smell(
            category="bug",
            severity="high",
            file="m.py",
            line=1,
            message="Mutable default argument in `f()`",
        )
    ]


def test_check_mutable_defaults_returns_empty_for_safe_defaults():
    source = "def f(x=None, y=0):\n    return x, y\n"
    assert _check_mutable_defaults("m.py", ast.parse(source), source) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _check_unused_exceptions builds the `defined` set and then "
        "discards it with `return []`, so its documented behaviour is never "
        "implemented and it is not registered in ALL_CHECKS"
    ),
)
def test_check_unused_exceptions_reports_unraised_class():
    source = (
        "class ThingError(Exception):\n    pass\n\n\nclass Helper(dict):\n    pass\n"
    )
    smells = _check_unused_exceptions("m.py", ast.parse(source), source)
    assert smells, "an exception defined and never raised should be reported"


# ---------------------------------------------------------------------------
# Module size (imports and measures the real source)
# ---------------------------------------------------------------------------


def test_check_module_size_flags_module_over_threshold():
    import azure_vm.exceptions as module

    loc = len(inspect.getsource(module).splitlines())
    smells = _check_module_size(["azure_vm.exceptions"], max_loc=loc - 1)
    assert smells == [
        Smell(
            category="smell",
            severity="medium",
            file="azure_vm.exceptions",
            line=1,
            message=(
                f"Module `azure_vm.exceptions` is {loc} lines "
                f"(max recommended: {loc - 1})"
            ),
        )
    ]


def test_check_module_size_accepts_module_at_threshold():
    import azure_vm.exceptions as module

    loc = len(inspect.getsource(module).splitlines())
    assert _check_module_size(["azure_vm.exceptions"], max_loc=loc) == []


def test_check_module_size_skips_excluded_and_unimportable_modules():
    assert _check_module_size(["azure_vm.devtools.code_eval"], max_loc=0) == []
    assert _check_module_size(["not_a_real_module_xyz"], max_loc=0) == []
    assert "azure_vm.devtools.code_eval" in EXCLUDED_MODULES


# ---------------------------------------------------------------------------
# grimp-based heuristics
# ---------------------------------------------------------------------------


def test_check_high_coupling_flags_fan_out_at_four():
    graph = _StubGraph(
        modules=("azure_vm.a", "azure_vm.b", "azure_vm.c", "azure_vm.d", "azure_vm.e"),
        imports={
            "azure_vm.a": (
                "azure_vm.b",
                "azure_vm.c",
                "azure_vm.d",
                "azure_vm.e",
            )
        },
        imported_by={
            "azure_vm.a": ("azure_vm.b",),
            "azure_vm.b": ("azure_vm.a",),
            "azure_vm.c": ("azure_vm.a",),
            "azure_vm.d": ("azure_vm.a",),
            "azure_vm.e": ("azure_vm.a",),
        },
    )
    assert _check_high_coupling(graph) == [
        Smell(
            category="coupling",
            severity="medium",
            file="azure_vm.a",
            line=1,
            message=(
                "High fan-out (4): depends on "
                "['azure_vm.b', 'azure_vm.c', 'azure_vm.d', 'azure_vm.e']. "
                "Consider introducing an intermediary."
            ),
        )
    ]


def test_check_high_coupling_ignores_fan_out_below_four():
    graph = _StubGraph(
        modules=("azure_vm.a", "azure_vm.b", "azure_vm.c", "azure_vm.d"),
        imports={"azure_vm.a": ("azure_vm.b", "azure_vm.c", "azure_vm.d")},
        imported_by={
            "azure_vm.a": ("azure_vm.b",),
            "azure_vm.b": ("azure_vm.a",),
            "azure_vm.c": ("azure_vm.a",),
            "azure_vm.d": ("azure_vm.a",),
        },
    )
    assert _check_high_coupling(graph) == []


def test_check_high_coupling_ignores_external_imports():
    graph = _StubGraph(
        modules=("azure_vm.a", "azure_vm.b"),
        imports={"azure_vm.a": ("os", "sys", "json", "re")},
        imported_by={"azure_vm.a": ("azure_vm.b",), "azure_vm.b": ("azure_vm.a",)},
    )
    assert _check_high_coupling(graph) == []


def test_check_high_coupling_flags_zero_fan_in():
    graph = _StubGraph(
        modules=("azure_vm.a", "azure_vm.b"),
        imported_by={"azure_vm.a": ("azure_vm.b",)},
    )
    assert _check_high_coupling(graph) == [
        Smell(
            category="coupling",
            severity="low",
            file="azure_vm.b",
            line=1,
            message=("Zero fan-in: no other internal module imports azure_vm.b"),
        )
    ]


def test_check_high_coupling_exempts_root_package_from_fan_in():
    graph = _StubGraph(modules=("azure_vm",))
    assert _check_high_coupling(graph) == []


def test_check_high_coupling_skips_excluded_and_foreign_modules():
    graph = _StubGraph(
        modules=("azure_vm.devtools.code_eval", "third_party.pkg"),
        imports={
            "azure_vm.devtools.code_eval": ("azure_vm.a", "azure_vm.b", "azure_vm.c"),
        },
    )
    assert _check_high_coupling(graph) == []


def test_check_circular_deps_reports_each_cycle():
    graph = _StubGraph(
        cycles=[
            ["azure_vm.a", "azure_vm.b", "azure_vm.a"],
            ["azure_vm.c", "azure_vm.c"],
        ]
    )
    assert _check_circular_deps(graph) == [
        Smell(
            category="coupling",
            severity="high",
            file="azure_vm.a",
            line=1,
            message="Circular dependency: azure_vm.a -> azure_vm.b -> azure_vm.a",
        ),
        Smell(
            category="coupling",
            severity="high",
            file="azure_vm.c",
            line=1,
            message="Circular dependency: azure_vm.c -> azure_vm.c",
        ),
    ]


def test_check_circular_deps_swallows_graph_failure():
    assert _check_circular_deps(_RaisingGraph()) == []


def test_cached_helpers_delegate_to_the_graph(monkeypatch):
    graph = _StubGraph(
        modules=("azure_vm.a",),
        imported_by={"azure_vm.a": ("azure_vm.b",)},
    )
    monkeypatch.setattr(code_eval, "_get_graph", lambda: graph)
    assert _check_high_coupling_cached() == []
    assert _check_circular_deps_cached() == []


def test_module_size_cached_filters_modules(monkeypatch):
    graph = _StubGraph(
        modules=(
            "azure_vm.b",
            "azure_vm.a",
            "azure_vm.devtools.quality",
            "third_party.pkg",
        )
    )
    monkeypatch.setattr(code_eval, "_get_graph", lambda: graph)
    captured: dict[str, list[str]] = {}

    def fake_module_size(modules, *, max_loc=250):
        captured["modules"] = list(modules)
        return []

    monkeypatch.setattr(code_eval, "_check_module_size", fake_module_size)
    assert _check_module_size_cached() == []
    assert captured["modules"] == ["azure_vm.a", "azure_vm.b"]


# ---------------------------------------------------------------------------
# Package-level checks
# ---------------------------------------------------------------------------


def test_check_init_exports_internal_flags_private_import(tmp_path):
    with _fake_azure_vm_package(
        tmp_path,
        "from azure_vm._secret import Thing\n",
        {"_secret.py": "Thing = object()\n"},
    ):
        smells = _check_init_exports_internal()
    assert smells == [
        Smell(
            category="smell",
            severity="medium",
            file="azure_vm/__init__.py",
            line=1,
            message=(
                "Public __init__ imports private module `azure_vm._secret` — "
                "leaks internal implementation detail"
            ),
        )
    ]


def test_check_init_exports_internal_accepts_public_imports(tmp_path):
    with _fake_azure_vm_package(
        tmp_path,
        "from azure_vm.client import AzureClient\n",
        {"client.py": "class AzureClient:\n    pass\n"},
    ):
        assert _check_init_exports_internal() == []


def test_check_init_exports_internal_against_real_package():
    assert _check_init_exports_internal() == []


def test_check_duplicated_run_method_against_real_client():
    assert _check_duplicated_run_method() == []


def test_check_duplicated_run_method_flags_client_run(tmp_path):
    client_source = (
        "class AzureClient:\n    def _run(self, command):\n        return command\n"
    )
    with _fake_azure_vm_package(tmp_path, "", {"client.py": client_source}):
        assert _check_duplicated_run_method() == [
            Smell(
                category="simplification",
                severity="medium",
                file="src/azure_vm/client.py",
                line=1,
                message=(
                    "AzureClient defines its own _run() — use run_command() instead"
                ),
            )
        ]


def test_check_unused_exception_classes_flags_unraised_class(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "src/azure_vm/exceptions.py",
        "class FirstError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "class SecondError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "class AzureVmError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Helper(dict):\n"
        "    pass\n",
    )
    _write(
        tmp_path,
        "src/azure_vm/consumer.py",
        "from azure_vm.exceptions import FirstError\n"
        "\n"
        "\n"
        "def go():\n"
        "    raise FirstError('boom')\n",
    )
    assert _check_unused_exception_classes() == [
        Smell(
            category="bug",
            severity="low",
            file="src/azure_vm/exceptions.py",
            line=5,
            message=(
                "Exception `SecondError` is defined but never raised or caught — "
                "dead code, or missing validation that should raise it"
            ),
        )
    ]


def test_check_unused_exception_classes_ignores_devtools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "src/azure_vm/devtools/hidden.py",
        "class HiddenError(Exception):\n    pass\n",
    )
    assert _check_unused_exception_classes() == []


def test_check_unused_exception_classes_scans_subpackages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "src/azure_vm/sub/mod.py",
        "class NestedError(Exception):\n    pass\n",
    )
    assert _check_unused_exception_classes() == [
        Smell(
            category="bug",
            severity="low",
            file="src/azure_vm/sub/mod.py",
            line=1,
            message=(
                "Exception `NestedError` is defined but never raised or caught — "
                "dead code, or missing validation that should raise it"
            ),
        )
    ]


def test_check_unused_exception_classes_without_source_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _check_unused_exception_classes() == []


# ---------------------------------------------------------------------------
# _check_ast_heuristic driver
# ---------------------------------------------------------------------------


def test_check_ast_heuristic_walks_tree_and_passes_parsed_arguments(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    good = _write(
        tmp_path,
        "src/azure_vm/a.py",
        "try:\n    pass\nexcept:\n    pass\n",
    )
    devtools_init = _write(
        tmp_path,
        "src/azure_vm/devtools/__init__.py",
        "try:\n    pass\nexcept:\n    pass\n",
    )
    _write(
        tmp_path,
        "src/azure_vm/devtools/skipped.py",
        "try:\n    pass\nexcept:\n    pass\n",
    )
    seen: list[tuple[str, object, str]] = []

    def recorder(path, tree, source):
        seen.append((path, tree, source))
        return _check_bare_except(path, tree, source)

    smells = _check_ast_heuristic(recorder)
    assert [p for p, _, _ in seen] == [
        "src/azure_vm/a.py",
        "src/azure_vm/devtools/__init__.py",
    ]
    assert all(isinstance(tree, ast.Module) for _, tree, _ in seen)
    assert seen[0][2] == good.read_text()
    assert seen[1][2] == devtools_init.read_text()
    assert [s.file for s in smells] == [
        "src/azure_vm/a.py",
        "src/azure_vm/devtools/__init__.py",
    ]


# ---------------------------------------------------------------------------
# God class detection
# ---------------------------------------------------------------------------


def test_check_god_classes_flags_too_many_public_methods(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = "\n".join(f"    def m{i}(self): ..." for i in range(1, 9))
    _write(tmp_path, "src/azure_vm/big.py", f"class TooPublic:\n{body}\n")
    smells = _check_god_classes()
    assert smells == [
        Smell(
            category="smell",
            severity="medium",
            file="src/azure_vm/big.py",
            line=1,
            message=(
                "God class `TooPublic`: 8 public methods "
                "(m1, m2, m3, m4, m5...). Refactor by extracting cohesive "
                "responsibilities into separate classes."
            ),
        )
    ]


def test_check_god_classes_flags_too_many_total_methods(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = "\n".join(f"    def _m{i}(self): ..." for i in range(13))
    _write(tmp_path, "src/azure_vm/many.py", f"class ManyMethods:\n{body}\n")
    smells = _check_god_classes()
    assert len(smells) == 1
    assert smells[0].message == (
        "God class `ManyMethods`: 13 total methods. Refactor by extracting "
        "cohesive responsibilities into separate classes."
    )


def test_check_god_classes_flags_oversized_class(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = "\n".join(["    _ = 1"] * 260)
    _write(tmp_path, "src/azure_vm/long.py", f"class Big:\n{body}\n")
    smells = _check_god_classes()
    assert len(smells) == 1
    assert smells[0].message == (
        "God class `Big`: 261 lines (max 250). Refactor by extracting cohesive "
        "responsibilities into separate classes."
    )


def test_check_god_classes_reports_every_trigger_in_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    public = "\n".join(f"    def m{i}(self): ..." for i in range(1, 9))
    private = "\n".join(f"    def _p{i}(self): ..." for i in range(5))
    _write(
        tmp_path,
        "src/azure_vm/combined.py",
        f"class Combined:\n{public}\n{private}\n",
    )
    graph = _StubGraph(
        modules=("azure_vm.combined",),
        imports={
            "azure_vm.combined": (
                "azure_vm.b",
                "azure_vm.c",
                "azure_vm.d",
                "azure_vm.e",
                "azure_vm.f",
            )
        },
    )
    monkeypatch.setattr(code_eval, "_get_graph", lambda: graph)
    smells = _check_god_classes()
    assert len(smells) == 1
    assert smells[0].message == (
        "God class `Combined`: 8 public methods (m1, m2, m3, m4, m5...); "
        "fan-out 5 (max 4); 13 total methods. Refactor by extracting cohesive "
        "responsibilities into separate classes."
    )


def test_check_god_classes_skips_devtools_and_syntax_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    public = "\n".join(f"    def m{i}(self): ..." for i in range(1, 9))
    _write(tmp_path, "src/azure_vm/devtools/__init__.py", f"class D:\n{public}\n")
    _write(tmp_path, "src/azure_vm/broken.py", "class Broken(:\n")
    _write(tmp_path, "src/azure_vm/ok.py", f"class Ok:\n{public}\n")
    smells = _check_god_classes()
    assert [s.file for s in smells] == ["src/azure_vm/ok.py"]


def test_check_god_classes_accepts_a_small_class(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "src/azure_vm/small.py",
        "class Small:\n"
        "    def m1(self): ...\n"
        "    def m2(self): ...\n"
        "    def _helper(self): ...\n",
    )
    assert _check_god_classes() == []


def test_check_god_classes_without_source_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _check_god_classes() == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_all_checks_registry_is_complete_and_callable():
    assert [name for name, _ in ALL_CHECKS] == [
        "unused-exception-classes",
        "bare-except",
        "broad-except",
        "raise-without-from",
        "mutable-defaults",
        "large-functions",
        "module-size",
        "high-coupling",
        "circular-deps",
        "init-exports-internal",
        "duplicated-run",
        "god-classes",
    ]
    assert all(callable(fn) for _, fn in ALL_CHECKS)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_with_no_smells():
    assert format_report([]) == "No issues found."


def test_format_report_renders_every_category():
    smell = Smell(category="bug", severity="high", file="a.py", line=3, message="boom")
    assert format_report([smell]) == (
        "1. Possible Bugs\n"
        "----------------\n"
        "  [HIGH] a.py:3 — boom\n"
        "\n"
        "2. Excessive Coupling\n"
        "---------------------\n"
        "  (none detected)\n"
        "\n"
        "3. Simplification Opportunities\n"
        "-------------------------------\n"
        "  (none detected)\n"
        "\n"
        "4. Code & Architectural Smells\n"
        "------------------------------\n"
        "  (none detected)\n"
    )


def test_format_report_sorts_categories_and_severities():
    smells = [
        Smell("smell", "low", "d.py", 4, "smell-low"),
        Smell("bug", "low", "b.py", 2, "bug-low"),
        Smell("bug", "high", "a.py", 1, "bug-high"),
        Smell("bug", "medium", "c.py", 3, "bug-medium"),
        Smell("coupling", "high", "e.py", 5, "coupling-high"),
    ]
    report = format_report(smells)
    assert report.index("1. Possible Bugs") < report.index("2. Excessive Coupling")
    assert report.index("2. Excessive Coupling") < report.index(
        "3. Simplification Opportunities"
    )
    assert report.index("4. Code & Architectural Smells") > report.index(
        "3. Simplification Opportunities"
    )
    bug_body = report.split("2. Excessive Coupling")[0]
    assert bug_body.index("[HIGH] a.py:1") < bug_body.index("[MEDIUM] c.py:3")
    assert bug_body.index("[MEDIUM] c.py:3") < bug_body.index("[LOW] b.py:2")
    assert "(none detected)" in report
    assert "smell-low" in report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_json_outputs_smell_objects(monkeypatch, capsys):
    smell = Smell("bug", "high", "a.py", 1, "boom")
    monkeypatch.setattr(sys, "argv", ["azure-vm-eval", "--json"])
    monkeypatch.setattr(code_eval, "ALL_CHECKS", [("one", lambda: [smell])])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "category": "bug",
            "severity": "high",
            "file": "a.py",
            "line": 1,
            "message": "boom",
        }
    ]


def test_main_text_outputs_formatted_report(monkeypatch, capsys):
    smell = Smell("bug", "high", "a.py", 1, "boom")
    monkeypatch.setattr(sys, "argv", ["azure-vm-eval"])
    monkeypatch.setattr(code_eval, "ALL_CHECKS", [("one", lambda: [smell])])
    main()
    assert capsys.readouterr().out == format_report([smell]) + "\n"


def test_main_aggregates_checks_in_registry_order(monkeypatch, capsys):
    first = Smell("bug", "high", "a.py", 1, "first")
    second = Smell("smell", "low", "b.py", 2, "second")
    monkeypatch.setattr(sys, "argv", ["azure-vm-eval", "--json"])
    monkeypatch.setattr(
        code_eval,
        "ALL_CHECKS",
        [("first", lambda: [first]), ("second", lambda: [second])],
    )
    main()
    payload = json.loads(capsys.readouterr().out)
    assert [item["message"] for item in payload] == ["first", "second"]


def test_main_reports_failed_checks_in_text_mode(monkeypatch, capsys):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sys, "argv", ["azure-vm-eval"])
    monkeypatch.setattr(code_eval, "ALL_CHECKS", [("boom", boom)])
    main()
    assert capsys.readouterr().out == (
        "No issues found.\nCheck failures: boom: kaboom\n"
    )


def test_main_hides_failures_in_json_mode(monkeypatch, capsys):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sys, "argv", ["azure-vm-eval", "--json"])
    monkeypatch.setattr(code_eval, "ALL_CHECKS", [("boom", boom)])
    main()
    out = capsys.readouterr().out
    assert json.loads(out) == []
    assert "kaboom" not in out
