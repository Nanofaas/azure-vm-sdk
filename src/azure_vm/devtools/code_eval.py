"""Static analysis of the azure_vm package for bugs, coupling and smells.

Walks each module's syntax tree with targeted AST checks and queries grimp's
import graph for coupling and cycle problems. Results are printed either as a
categorised text report or, with ``--json``, as a JSON array.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import grimp

ROOT_PACKAGE = "azure_vm"
EXCLUDED_MODULES = frozenset(
    {
        "azure_vm.devtools.package_report",
        "azure_vm.devtools.quality",
        "azure_vm.devtools.code_eval",
    }
)


# ---------------------------------------------------------------------------
# AST-based heuristics
# ---------------------------------------------------------------------------


@dataclass
class Smell:
    """One issue found by a check, with the place it was found.

    Attributes:
        category: Report section the issue belongs to: ``bug``, ``coupling``,
            ``simplification`` or ``smell``.
        severity: ``high``, ``medium`` or ``low``; drives ordering within a
            section.
        file: Path of the file the issue was detected in, or the module name
            for checks that only know the module.
        line: Source line the issue was detected at.
        message: Description of the problem, shown to the reader verbatim.

    """

    category: str  # bug, coupling, simplification, smell
    severity: str  # high, medium, low
    file: str
    line: int
    message: str


def _check_bare_except(module_path: str, tree: ast.AST, source: str) -> list[Smell]:
    smells: list[Smell] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            smells.extend(
                Smell(
                    category="bug",
                    severity="high",
                    file=module_path,
                    line=handler.lineno,
                    message="Bare except: — catches KeyboardInterrupt and SystemExit",
                )
                for handler in node.handlers
                if handler.type is None
            )
    return smells


def _check_broad_except(module_path: str, tree: ast.AST, source: str) -> list[Smell]:
    smells: list[Smell] = []
    broad = {"Exception", "BaseException"}
    smells.extend(
        Smell(
            category="bug",
            severity="medium",
            file=module_path,
            line=node.lineno,
            message=f"Broad except clause catches {ast.unparse(node.type)}",
        )
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.ExceptHandler)
            and node.type
            and ast.unparse(node.type) in broad
        )
    )
    return smells


def _check_raise_without_from(
    module_path: str, tree: ast.AST, source: str
) -> list[Smell]:
    smells: list[Smell] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                smells.extend(
                    Smell(
                        category="bug",
                        severity="medium",
                        file=module_path,
                        line=child.lineno,
                        message="Raise inside except without `from` — "
                        "exception chain is lost",
                    )
                    for child in ast.walk(handler)
                    if isinstance(child, ast.Raise) and child.exc and not child.cause
                )
    return smells


def _check_mutable_defaults(
    module_path: str, tree: ast.AST, source: str
) -> list[Smell]:
    smells: list[Smell] = []
    mutable = (ast.List, ast.Dict, ast.Set)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            smells.extend(
                Smell(
                    category="bug",
                    severity="high",
                    file=module_path,
                    line=default.lineno,
                    message=f"Mutable default argument in `{node.name}()`",
                )
                # `default` is None for keyword-only parameters that have no
                # default, hence the truthiness test. There used to be a second
                # condition here requiring `isinstance(default, ast.Constant)`,
                # which no list, dict or set literal can ever satisfy - so this
                # check reported nothing at all.
                for default in node.args.defaults + node.args.kw_defaults
                if default and isinstance(default, mutable)
            )
    return smells


def _check_large_functions(
    module_path: str, tree: ast.AST, source: str, *, max_loc: int = 30
) -> list[Smell]:
    smells: list[Smell] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            loc = end - node.lineno + 1
            if loc > max_loc:
                smells.append(
                    Smell(
                        category="simplification",
                        severity="medium",
                        file=module_path,
                        line=node.lineno,
                        message=(
                            f"Function `{node.name}()` is {loc} lines "
                            f"(max recommended: {max_loc})"
                        ),
                    )
                )
    return smells


# ---------------------------------------------------------------------------
# grimp-based heuristics
# ---------------------------------------------------------------------------


def _check_module_size(modules: Sequence[str], *, max_loc: int = 250) -> list[Smell]:
    """Check file size against convention."""
    smells: list[Smell] = []
    for module in modules:
        if module in EXCLUDED_MODULES:
            continue
        # A default is needed here: suppress() falls through where the original
        # try/except/continue skipped the module, so loc must be distinguishable
        # from a real measurement.
        loc: int | None = None
        with contextlib.suppress(Exception):
            import importlib

            mod = importlib.import_module(module)
            source = __import__("inspect").getsource(mod)
            loc = len(source.splitlines())
        if loc is None:
            continue  # the module could not be imported or inspected
        if loc > max_loc:
            smells.append(
                Smell(
                    category="smell",
                    severity="medium",
                    file=module,
                    line=1,
                    message=(
                        f"Module `{module}` is {loc} lines (max recommended: {max_loc})"
                    ),
                )
            )
    return smells


def _check_high_coupling(graph) -> list[Smell]:
    """Flag modules with fan-out >= 4 or no incoming dependencies."""
    smells: list[Smell] = []
    root_mods = sorted(
        m
        for m in graph.modules
        if (m == ROOT_PACKAGE or m.startswith(f"{ROOT_PACKAGE}."))
        and m not in EXCLUDED_MODULES
    )
    for mod in root_mods:
        imports = sorted(graph.find_modules_directly_imported_by(mod))
        internal_imports = [
            i for i in imports if i == ROOT_PACKAGE or i.startswith(f"{ROOT_PACKAGE}.")
        ]
        imported_by = sorted(graph.find_modules_that_directly_import(mod))
        internal_imported_by = [
            i
            for i in imported_by
            if i == ROOT_PACKAGE or i.startswith(f"{ROOT_PACKAGE}.")
        ]
        if len(internal_imports) >= 4:
            smells.append(
                Smell(
                    category="coupling",
                    severity="medium",
                    file=mod,
                    line=1,
                    message=(
                        f"High fan-out ({len(internal_imports)}): "
                        f"depends on {internal_imports}. "
                        f"Consider introducing an intermediary."
                    ),
                )
            )
        if mod != ROOT_PACKAGE and len(internal_imported_by) == 0:
            smells.append(
                Smell(
                    category="coupling",
                    severity="low",
                    file=mod,
                    line=1,
                    message=f"Zero fan-in: no other internal module imports {mod}",
                )
            )
    return smells


def _check_circular_deps(graph) -> list[Smell]:
    """Detect circular dependencies."""
    smells: list[Smell] = []
    # The graph query is best-effort: a grimp failure means no cycle data, not
    # a failed check, and the remaining checks still run.
    with contextlib.suppress(Exception):
        cycles = graph.find_cycles()
        smells.extend(
            Smell(
                category="coupling",
                severity="high",
                file=cycle[0],
                line=1,
                message=f"Circular dependency: {' -> '.join(cycle)}",
            )
            for cycle in cycles
        )
    return smells


def _check_init_exports_internal() -> list[Smell]:
    """Detect if __init__ exports internal/private modules."""
    smells: list[Smell] = []
    # Importing the package under test is best-effort: if it cannot be imported
    # or parsed, this check reports nothing rather than failing the whole run.
    with contextlib.suppress(Exception):
        import ast
        import importlib
        import inspect

        mod = importlib.import_module(f"{ROOT_PACKAGE}")
        source = inspect.getsource(mod)
        tree = ast.parse(source)
        smells.extend(
            Smell(
                category="smell",
                severity="medium",
                file=f"{ROOT_PACKAGE}/__init__.py",
                line=node.lineno,
                message=(
                    f"Public __init__ imports private module "
                    f"`{node.module}` — leaks internal implementation detail"
                ),
            )
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(f"{ROOT_PACKAGE}._")
            )
        )
    return smells


def _check_unused_exception_classes() -> list[Smell]:
    """Find exception classes that are defined but never raised or caught."""
    smells: list[Smell] = []
    import ast
    from pathlib import Path

    root = Path(f"src/{ROOT_PACKAGE}")
    if not root.exists():
        return smells

    defined_in: dict[str, tuple[str, int]] = {}
    for py_file in sorted(root.rglob("*.py")):
        if "devtools" in str(py_file):
            continue
        source = py_file.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_str = ast.unparse(base) if hasattr(ast, "unparse") else ""
                    if "Error" in base_str or "Exception" in base_str:
                        defined_in[node.name] = (str(py_file), node.lineno)

    used: set[str] = set()
    for py_file in sorted(root.rglob("*.py")):
        if "devtools" in str(py_file) or py_file.name == "__init__.py":
            continue
        source = py_file.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in defined_in:
                used.add(node.id)

    for exc_name, (path, line) in defined_in.items():
        if exc_name not in used and exc_name != "AzureVmError":
            smells.append(
                Smell(
                    category="bug",
                    severity="low",
                    file=path,
                    line=line,
                    message=(
                        f"Exception `{exc_name}` is defined but never "
                        f"raised or caught — dead code, or missing validation "
                        f"that should raise it"
                    ),
                )
            )
    return smells


def _check_duplicated_run_method() -> list[Smell]:
    """Check that _run logic is not duplicated between AzureClient and AzureVM."""
    smells: list[Smell] = []
    import inspect

    # A missing client module or unreadable source means nothing to report, not
    # a failed check.
    with contextlib.suppress(Exception):
        from azure_vm.client import AzureClient

        source = inspect.getsource(AzureClient)
        # If _run still exists as a method on AzureClient, flag it
        if "def _run(self" in source:
            smells.append(
                Smell(
                    category="simplification",
                    severity="medium",
                    file="src/azure_vm/client.py",
                    line=1,
                    message=(
                        "AzureClient defines its own _run() — use run_command() instead"
                    ),
                )
            )
    return smells


# ---------------------------------------------------------------------------
# God class detection
# ---------------------------------------------------------------------------


@dataclass
class ClassMetrics:
    """Size, method count and coupling measurements for one class.

    Attributes:
        name: Name of the class.
        file: Path of the file the class is defined in.
        lines: Number of source lines the class spans.
        public_methods: Count of methods whose names do not start with an
            underscore.
        total_methods: Count of all methods defined directly in the class.
        fan_out: Number of internal ``azure_vm`` modules the class's module
            imports.
        responsibilities: Names of the class's public methods.

    """

    name: str
    file: str
    lines: int
    public_methods: int
    total_methods: int
    fan_out: int
    responsibilities: list[str]


def _check_god_classes() -> list[Smell]:
    """Flag classes that have too many responsibilities or methods."""
    smells: list[Smell] = []
    import ast as ast_m
    from pathlib import Path

    root = Path(f"src/{ROOT_PACKAGE}")
    if not root.exists():
        return smells

    for py_file in sorted(root.rglob("*.py")):
        if "devtools" in str(py_file) or py_file.name == "__init__.py":
            continue
        source = py_file.read_text()
        try:
            tree = ast_m.parse(source)
        except SyntaxError:
            continue

        for node in ast_m.walk(tree):
            if not isinstance(node, ast_m.ClassDef):
                continue

            public = 0
            total = 0
            responsibilities_list: list[str] = []
            for item in node.body:
                if isinstance(item, (ast_m.FunctionDef, ast_m.AsyncFunctionDef)):
                    total += 1
                    if not item.name.startswith("_"):
                        public += 1
                        responsibilities_list.append(item.name)

            end = node.end_lineno or node.lineno
            loc = end - node.lineno + 1

            # Compute fan-out via grimp
            module_name = (
                str(py_file).replace("src/", "").replace("/", ".").replace(".py", "")
            )
            if module_name.endswith(".__init__"):
                module_name = module_name[: -len(".__init__")]
            # fan keeps its 0 default when grimp cannot supply a graph, so a
            # suppression here leaves the same value the except/pass did.
            fan = 0
            with contextlib.suppress(Exception):
                graph = _get_graph()
                if module_name in graph.modules:
                    imports = graph.find_modules_directly_imported_by(module_name)
                    internal = [
                        i
                        for i in imports
                        if i == ROOT_PACKAGE or i.startswith(f"{ROOT_PACKAGE}.")
                    ]
                    fan = len(internal)

            issues: list[str] = []

            if len(responsibilities_list) > 7:
                issues.append(
                    f"{len(responsibilities_list)} public methods "
                    f"({', '.join(responsibilities_list[:5])}...)"
                )
            if loc > 250:
                issues.append(f"{loc} lines (max 250)")
            if fan > 4:
                issues.append(f"fan-out {fan} (max 4)")
            if total > 12:
                issues.append(f"{total} total methods")

            if issues:
                smells.append(
                    Smell(
                        category="smell",
                        severity="medium",
                        file=str(py_file),
                        line=node.lineno,
                        message=f"God class `{node.name}`: {'; '.join(issues)}. "
                        "Refactor by extracting cohesive responsibilities "
                        "into separate classes.",
                    )
                )

    return smells


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


ALL_CHECKS: list[tuple[str, Callable[[], list[Smell]]]] = [
    ("unused-exception-classes", lambda: _check_unused_exception_classes()),
    ("bare-except", lambda: _check_ast_heuristic(_check_bare_except)),
    ("broad-except", lambda: _check_ast_heuristic(_check_broad_except)),
    ("raise-without-from", lambda: _check_ast_heuristic(_check_raise_without_from)),
    ("mutable-defaults", lambda: _check_ast_heuristic(_check_mutable_defaults)),
    (
        "large-functions",
        lambda: _check_ast_heuristic(lambda p, t, s: _check_large_functions(p, t, s)),
    ),
    ("module-size", lambda: _check_module_size_cached()),
    ("high-coupling", lambda: _check_high_coupling_cached()),
    ("circular-deps", lambda: _check_circular_deps_cached()),
    ("init-exports-internal", lambda: _check_init_exports_internal()),
    ("duplicated-run", lambda: _check_duplicated_run_method()),
    ("god-classes", lambda: _check_god_classes()),
]


def _check_ast_heuristic(check_fn) -> list[Smell]:
    import ast
    from pathlib import Path

    root = Path(f"src/{ROOT_PACKAGE}")
    smells: list[Smell] = []
    for py_file in sorted(root.rglob("*.py")):
        if "devtools" in str(py_file) and py_file.name != "__init__.py":
            continue
        source = py_file.read_text()
        tree = ast.parse(source)
        smells.extend(check_fn(str(py_file), tree, source))
    return smells


# Cached grimp graph
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = grimp.build_graph(ROOT_PACKAGE, include_external_packages=False)
    return _graph


def _check_module_size_cached():
    root_mods = sorted(
        m
        for m in _get_graph().modules
        if (m == ROOT_PACKAGE or m.startswith(f"{ROOT_PACKAGE}."))
        and m not in EXCLUDED_MODULES
    )
    return _check_module_size(root_mods)


def _check_high_coupling_cached():
    return _check_high_coupling(_get_graph())


def _check_circular_deps_cached():
    return _check_circular_deps(_get_graph())


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_report(smells: list[Smell]) -> str:
    """Render smells as a numbered, category-grouped text report.

    Each category gets a heading, a rule and its smells sorted most severe
    first; a category with no smells is marked ``(none detected)``.

    Args:
        smells: Issues to report.

    Returns:
        The formatted report, or ``"No issues found."`` when ``smells`` is
        empty.

    """
    if not smells:
        return "No issues found."

    by_category: dict[str, list[Smell]] = {}
    for s in smells:
        by_category.setdefault(s.category, []).append(s)

    category_names = {
        "bug": "1. Possible Bugs",
        "coupling": "2. Excessive Coupling",
        "simplification": "3. Simplification Opportunities",
        "smell": "4. Code & Architectural Smells",
    }

    lines: list[str] = []
    for cat_key in ("bug", "coupling", "simplification", "smell"):
        items = by_category.get(cat_key, [])
        lines.append(category_names[cat_key])
        lines.append("-" * len(category_names[cat_key]))
        if not items:
            lines.append("  (none detected)\n")
            continue
        for item in sorted(
            items, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.severity]
        ):
            sev = item.severity.upper()
            lines.append(f"  [{sev}] {item.file}:{item.line} — {item.message}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Run every check and print the collected smells.

    A check that raises is skipped and recorded as a failure rather than
    aborting the run. Output is a JSON array with ``--json``, otherwise the
    text report; failures are appended to the text report only.
    """
    parser = argparse.ArgumentParser(
        description="Evaluate code quality: bugs, coupling, simplifications, smells."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON.",
    )
    args = parser.parse_args()

    all_smells: list[Smell] = []
    failures: list[str] = []
    for name, check_fn in ALL_CHECKS:
        try:
            all_smells.extend(check_fn())
        except Exception as exc:
            failures.append(f"{name}: {exc}")

    if args.json:
        import json

        items = [
            {
                "category": s.category,
                "severity": s.severity,
                "file": s.file,
                "line": s.line,
                "message": s.message,
            }
            for s in all_smells
        ]
        print(json.dumps(items, indent=2))
    else:
        print(format_report(all_smells))
        if failures:
            print(f"Check failures: {', '.join(failures)}")


if __name__ == "__main__":
    main()
