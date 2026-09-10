"""Per-module import metrics for the azure_vm package.

Builds the internal dependency graph with grimp and derives fan-in, fan-out and
instability for each module. ``main`` prints the table, optionally with the
individual edges and the modules that have no internal dependencies.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import grimp

ROOT_PACKAGE = "azure_vm"
EXCLUDED_MODULES = frozenset(
    {
        "azure_vm.devtools.package_report",
        "azure_vm.devtools.quality",
    }
)


@dataclass(frozen=True)
class ModuleMetrics:
    """Import statistics for a single module in the dependency graph.

    Attributes:
        module: Module name with the ``azure_vm.`` prefix stripped.
        internal_imports: Count of edges where the module imports itself.
        outgoing_imports: Count of internal modules this one imports.
        incoming_imports: Count of internal modules that import this one.
        external_imports: Count of imports whose target is outside the module
            set being measured.
        instability: ``outgoing / (incoming + outgoing)`` rounded to two
            decimals, or ``0.0`` when the module has neither.

    """

    module: str
    internal_imports: int
    outgoing_imports: int
    incoming_imports: int
    external_imports: int
    instability: float


def _short_name(module: str) -> str:
    if module.startswith(f"{ROOT_PACKAGE}."):
        return module[len(ROOT_PACKAGE) + 1 :]
    return module


def calculate_metrics(
    *,
    modules: Sequence[str],
    edges: Iterable[tuple[str, str]],
) -> list[ModuleMetrics]:
    """Count import edges per module and derive their instability.

    Edges whose importer is not in ``modules`` are skipped entirely; an edge
    whose imported module is not in ``modules`` only increments the importer's
    external count.

    Args:
        modules: Modules to report on, in output order.
        edges: ``(importer, imported)`` module name pairs.

    Returns:
        One :class:`ModuleMetrics` per entry in ``modules``, in that order.

    """
    mod_set = frozenset(modules)
    internal_counts = dict.fromkeys(modules, 0)
    outgoing_counts = dict.fromkeys(modules, 0)
    incoming_counts = dict.fromkeys(modules, 0)
    external_counts = dict.fromkeys(modules, 0)

    for importer, imported in edges:
        importer_internal = importer in mod_set
        imported_internal = imported in mod_set

        if not importer_internal:
            continue

        if not imported_internal:
            external_counts[importer] += 1
            continue

        if importer == imported:
            internal_counts[importer] += 1
            continue
        outgoing_counts[importer] += 1
        incoming_counts[imported] += 1

    metrics: list[ModuleMetrics] = []
    for module in modules:
        outgoing = outgoing_counts[module]
        incoming = incoming_counts[module]
        denominator = incoming + outgoing
        instability = round(outgoing / denominator, 2) if denominator else 0.0
        metrics.append(
            ModuleMetrics(
                module=_short_name(module),
                internal_imports=internal_counts[module],
                outgoing_imports=outgoing,
                incoming_imports=incoming,
                external_imports=external_counts[module],
                instability=instability,
            )
        )
    return metrics


def format_metrics_table(metrics: Sequence[ModuleMetrics]) -> str:
    """Lay module metrics out as a fixed-width text table.

    Args:
        metrics: Metrics to render, one row each, in order.

    Returns:
        The header row, a rule of matching width and one row per metric,
        joined by newlines.

    """
    header = (
        f"{'module':30} {'internal':>8} {'outgoing':>8} "
        f"{'incoming':>8} {'external':>8} {'instability':>11}"
    )
    rows = [header, "-" * len(header)]
    rows.extend(
        f"{metric.module:30} "
        f"{metric.internal_imports:8d} "
        f"{metric.outgoing_imports:8d} "
        f"{metric.incoming_imports:8d} "
        f"{metric.external_imports:8d} "
        f"{metric.instability:11.2f}"
        for metric in metrics
    )
    return "\n".join(rows)


def _iter_grimp_edges(root_package: str) -> list[tuple[str, str]]:
    graph = grimp.build_graph(root_package, include_external_packages=False)
    modules = sorted(
        module
        for module in graph.modules
        if (module == root_package or module.startswith(f"{root_package}."))
        and module not in EXCLUDED_MODULES
    )
    edges: list[tuple[str, str]] = []
    for importer in modules:
        edges.extend(
            (importer, imported)
            for imported in sorted(graph.find_modules_directly_imported_by(importer))
        )
    return edges


def build_current_metrics() -> list[ModuleMetrics]:
    """Summarise the current azure_vm import graph in one call.

    Returns:
        Metrics for every module reached by an internal edge, except the
        reporting modules themselves, sorted by module name.

    """
    edges = _iter_grimp_edges(ROOT_PACKAGE)
    modules_set: set[str] = set()
    for importer, imported in edges:
        if importer not in EXCLUDED_MODULES:
            modules_set.add(importer)
        if imported not in EXCLUDED_MODULES:
            modules_set.add(imported)
    return calculate_metrics(
        modules=sorted(modules_set),
        edges=edges,
    )


def main() -> None:
    """Print the module metrics table and any requested extra sections.

    ``--edges`` adds every internal import edge and ``--orphans`` adds the
    modules involved in no internal edge at all.
    """
    parser = argparse.ArgumentParser(
        description="Report internal and cross-module imports for azure_vm."
    )
    parser.add_argument(
        "--edges",
        action="store_true",
        help="Show individual import edges.",
    )
    parser.add_argument(
        "--orphans",
        action="store_true",
        help="Show modules with no internal dependencies.",
    )
    args = parser.parse_args()

    edges = _iter_grimp_edges(ROOT_PACKAGE)
    modules_set: set[str] = set()
    for importer, imported in edges:
        if importer not in EXCLUDED_MODULES:
            modules_set.add(importer)
        if imported not in EXCLUDED_MODULES:
            modules_set.add(imported)
    modules = sorted(modules_set)

    print(format_metrics_table(calculate_metrics(modules=modules, edges=edges)))

    if args.edges:
        print("\n[Dependency edges]")
        for importer, imported in edges:
            if importer in EXCLUDED_MODULES or imported in EXCLUDED_MODULES:
                continue
            print(f"  {_short_name(importer)} -> {_short_name(imported)}")

    if args.orphans:
        all_with_deps: set[str] = set()
        for importer, imported in edges:
            if importer in EXCLUDED_MODULES or imported in EXCLUDED_MODULES:
                continue
            all_with_deps.add(importer)
            all_with_deps.add(imported)
        orphans = sorted(m for m in modules if m not in all_with_deps)
        if orphans:
            print("\n[Orphan modules (no internal deps)]")
            for o in orphans:
                print(f"  {_short_name(o)}")


if __name__ == "__main__":
    main()
