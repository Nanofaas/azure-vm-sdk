import sys
import uuid
from pathlib import Path

import pytest

from azure_vm.devtools import package_report

# The header is width-30 module, then five right-aligned numeric columns. It is
# spelled out here rather than reused from the module so the test pins the exact
# layout the report promises its readers.
EXPECTED_HEADER = (
    "module" + " " * 24 + " internal outgoing incoming external instability"
)

# Rows the ``reporting_package`` fixture must render, in module-name order.
REPORT_ROWS = [
    "alpha                                 0        1        0        0        1.00",
    "beta                                  0        1        1        0        0.50",
    "epsilon                               0        0        0        1        0.00",
    "gamma                                 0        0        1        0        0.00",
]


def _make_package(tmp_path: Path, files: dict[str, str]) -> str:
    """Write a real importable package under tmp_path and return its name.

    A fresh name is used every call: grimp caches the parsed graph by package
    name, so reusing a name would hand back the first test's tree.
    """
    name = f"pkg_{uuid.uuid4().hex[:10]}"
    package_dir = tmp_path / name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    for module, source in files.items():
        (package_dir / f"{module}.py").write_text(source.format(pkg=name))
    return name


def _make_reporting_package(tmp_path: Path, monkeypatch, files: dict[str, str]) -> str:
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _make_package(tmp_path, files)
    monkeypatch.setattr(package_report, "ROOT_PACKAGE", name)
    return name


# --------------------------------------------------------------------------- #
# calculate_metrics
# --------------------------------------------------------------------------- #


def test_calculate_metrics_counts_each_kind_of_edge():
    metrics = package_report.calculate_metrics(
        modules=["azure_vm.a", "azure_vm.b", "plain"],
        edges=[
            ("azure_vm.a", "azure_vm.b"),  # outgoing / incoming
            ("azure_vm.a", "azure_vm.a"),  # self import
            ("azure_vm.b", "external.mod"),  # external target
            ("outside.mod", "azure_vm.a"),  # importer not listed -> dropped
            ("plain", "azure_vm.a"),  # non azure_vm module still counted
            ("azure_vm.a", "plain"),
        ],
    )
    assert metrics == [
        package_report.ModuleMetrics(
            module="a",
            internal_imports=1,
            outgoing_imports=2,
            incoming_imports=1,
            external_imports=0,
            instability=0.67,
        ),
        package_report.ModuleMetrics(
            module="b",
            internal_imports=0,
            outgoing_imports=0,
            incoming_imports=1,
            external_imports=1,
            instability=0.0,
        ),
        package_report.ModuleMetrics(
            module="plain",
            internal_imports=0,
            outgoing_imports=1,
            incoming_imports=1,
            external_imports=0,
            instability=0.5,
        ),
    ]


def test_calculate_metrics_skips_edges_from_unlisted_importers():
    metrics = package_report.calculate_metrics(
        modules=["azure_vm.a"],
        edges=[("ghost.mod", "azure_vm.a")],
    )
    assert metrics == [
        package_report.ModuleMetrics(
            module="a",
            internal_imports=0,
            outgoing_imports=0,
            incoming_imports=0,
            external_imports=0,
            instability=0.0,
        )
    ]


def test_calculate_metrics_instability_is_zero_for_a_module_with_no_edges():
    metrics = package_report.calculate_metrics(modules=["azure_vm.solo"], edges=[])
    assert metrics[0].instability == 0.0
    assert metrics[0].incoming_imports == 0
    assert metrics[0].outgoing_imports == 0


def test_calculate_metrics_keeps_the_modules_argument_order():
    metrics = package_report.calculate_metrics(
        modules=["azure_vm.z", "azure_vm.a"],
        edges=[("azure_vm.z", "azure_vm.a")],
    )
    assert [metric.module for metric in metrics] == ["z", "a"]


def test_calculate_metrics_does_not_strip_the_bare_root_package_name():
    metrics = package_report.calculate_metrics(modules=["azure_vm"], edges=[])
    assert metrics[0].module == "azure_vm"


def test_calculate_metrics_does_not_strip_a_near_miss_prefix():
    metrics = package_report.calculate_metrics(modules=["azure_vm_extra.mod"], edges=[])
    assert metrics[0].module == "azure_vm_extra.mod"


# --------------------------------------------------------------------------- #
# format_metrics_table
# --------------------------------------------------------------------------- #


def test_format_metrics_table_renders_header_rule_and_row():
    metrics = [
        package_report.ModuleMetrics(
            module="client",
            internal_imports=1,
            outgoing_imports=2,
            incoming_imports=3,
            external_imports=4,
            instability=0.67,
        )
    ]
    expected_row = (
        "client"
        + " " * 24
        + " "
        + "       1"
        + " "
        + "       2"
        + " "
        + "       3"
        + " "
        + "       4"
        + " "
        + "       0.67"
    )
    assert package_report.format_metrics_table(metrics) == "\n".join(
        [EXPECTED_HEADER, "-" * len(EXPECTED_HEADER), expected_row]
    )


def test_format_metrics_table_without_metrics_has_only_header_and_rule():
    assert package_report.format_metrics_table([]) == "\n".join(
        [EXPECTED_HEADER, "-" * len(EXPECTED_HEADER)]
    )


# --------------------------------------------------------------------------- #
# _iter_grimp_edges
# --------------------------------------------------------------------------- #


def test_iter_grimp_edges_reads_a_real_package_tree(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _make_package(
        tmp_path,
        {
            "alpha": "from {pkg} import gamma\nfrom {pkg} import beta\n",
            "beta": "from {pkg} import gamma\n",
            "gamma": "",
        },
    )
    assert package_report._iter_grimp_edges(name) == [
        (f"{name}.alpha", f"{name}.beta"),
        (f"{name}.alpha", f"{name}.gamma"),
        (f"{name}.beta", f"{name}.gamma"),
    ]


def test_iter_grimp_edges_drops_edges_whose_importer_is_excluded(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _make_package(
        tmp_path,
        {
            "alpha": "from {pkg} import beta\n",
            "beta": "from {pkg} import gamma\n",
            "gamma": "",
        },
    )
    monkeypatch.setattr(package_report, "EXCLUDED_MODULES", frozenset({f"{name}.beta"}))
    # beta still shows up as an import target, but its own import of gamma is
    # gone because excluded modules are not walked as importers.
    assert package_report._iter_grimp_edges(name) == [(f"{name}.alpha", f"{name}.beta")]


# --------------------------------------------------------------------------- #
# build_current_metrics
# --------------------------------------------------------------------------- #


def test_build_current_metrics_summarises_the_installed_package():
    metrics = package_report.build_current_metrics()
    names = [metric.module for metric in metrics]

    assert names
    assert len(names) == len(set(names))
    assert "client" in names
    assert "devtools.package_report" not in names
    assert "devtools.quality" not in names

    edges = package_report._iter_grimp_edges(package_report.ROOT_PACKAGE)
    described = sorted(
        {module for edge in edges for module in edge}
        - set(package_report.EXCLUDED_MODULES)
    )
    assert names == [package_report._short_name(module) for module in described]
    assert metrics == package_report.calculate_metrics(modules=described, edges=edges)


def test_build_current_metrics_omits_excluded_modules_that_are_imported(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _make_package(tmp_path, {"alpha": "from {pkg} import tool\n", "tool": ""})
    monkeypatch.setattr(package_report, "ROOT_PACKAGE", name)
    monkeypatch.setattr(package_report, "EXCLUDED_MODULES", frozenset({f"{name}.tool"}))
    metrics = package_report.build_current_metrics()
    # alpha's import of the excluded tool counts as external, and tool itself
    # never becomes a reported row.
    assert metrics == [
        package_report.ModuleMetrics(
            module="alpha",
            internal_imports=0,
            outgoing_imports=0,
            incoming_imports=0,
            external_imports=1,
            instability=0.0,
        )
    ]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

REPORTING_TREE = {
    "alpha": "from {pkg} import beta\n",
    "beta": "from {pkg} import gamma\n",
    "gamma": "",
    "epsilon": "from {pkg} import delta\n",
    "delta": "",
}


@pytest.fixture
def reporting_package(tmp_path, monkeypatch):
    name = _make_reporting_package(tmp_path, monkeypatch, REPORTING_TREE)
    monkeypatch.setattr(
        package_report, "EXCLUDED_MODULES", frozenset({f"{name}.delta"})
    )
    return name


def test_main_prints_only_the_metrics_table_by_default(
    reporting_package, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["azure-vm-package-report"])
    package_report.main()
    out = capsys.readouterr().out
    assert out.splitlines() == [
        EXPECTED_HEADER,
        "-" * len(EXPECTED_HEADER),
        *REPORT_ROWS,
    ]


def test_main_edges_flag_lists_internal_edges_and_hides_excluded_ones(
    reporting_package, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["azure-vm-package-report", "--edges"])
    package_report.main()
    out = capsys.readouterr().out
    assert "\n[Dependency edges]\n" in out
    section = out.split("[Dependency edges]", 1)[1]
    assert section.splitlines() == ["", "  alpha -> beta", "  beta -> gamma"]
    assert "delta" not in out
    assert "[Orphan modules" not in out


def test_main_orphans_flag_lists_modules_with_no_internal_edges(
    reporting_package, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["azure-vm-package-report", "--orphans"])
    package_report.main()
    out = capsys.readouterr().out
    assert "\n[Orphan modules (no internal deps)]\n  epsilon\n" in out
    assert "[Dependency edges]" not in out


def test_main_omits_the_orphans_section_when_there_are_none(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _make_package(tmp_path, {"alpha": "from {pkg} import beta\n", "beta": ""})
    monkeypatch.setattr(package_report, "ROOT_PACKAGE", name)
    monkeypatch.setattr(package_report, "EXCLUDED_MODULES", frozenset())
    monkeypatch.setattr(sys, "argv", ["azure-vm-package-report", "--orphans"])
    package_report.main()
    out = capsys.readouterr().out
    assert "[Orphan modules" not in out
    assert out.splitlines()[2] == REPORT_ROWS[0]
