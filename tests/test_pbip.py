"""The generated Power BI project must be internally consistent.

Power BI Desktop only reports a broken field reference when the page is
opened, so CI checks it here: every column and measure a visual refers to
exists in the TMDL model, every relationship joins columns that exist, and
every JSON file parses. The generator is run into a temporary folder so the
committed ``powerbi/`` folder is not touched.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_pbip.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_pbip", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_pbip"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    out = tmp_path_factory.mktemp("pbip")
    gen = _load_generator()
    gen.OUT = out
    gen.MODEL_DIR = out / f"{gen.NAME}.SemanticModel"
    gen.REPORT_DIR = out / f"{gen.NAME}.Report"
    gen.main()
    return gen, out


def _parse_tmdl_tables(model_dir: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """table -> columns, table -> measures, from the TMDL table files."""
    columns: dict[str, set[str]] = {}
    measures: dict[str, set[str]] = {}
    name_re = re.compile(r"^(column|measure) (?:'([^']+)'|(\S+))")
    for f in (model_dir / "definition" / "tables").glob("*.tmdl"):
        table = None
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("table "):
                table = line[6:].strip().strip("'")
                columns.setdefault(table, set()); measures.setdefault(table, set())
            m = name_re.match(line.strip())
            if m and table:
                name = m.group(2) or m.group(3)
                (columns if m.group(1) == "column" else measures)[table].add(name)
    return columns, measures


def _field_refs(node, out: list[tuple[str, str, str]]):
    """Collect (kind, table, property) for every Column/Measure reference."""
    if isinstance(node, dict):
        for kind in ("Column", "Measure"):
            if kind in node and isinstance(node[kind], dict) and "Property" in node[kind]:
                ent = node[kind]["Expression"]["SourceRef"]["Entity"]
                out.append((kind, ent, node[kind]["Property"]))
        for v in node.values():
            _field_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _field_refs(v, out)


def test_project_files_exist_and_parse(project):
    gen, out = project
    assert (out / f"{gen.NAME}.pbip").exists()
    assert (gen.REPORT_DIR / "definition.pbir").exists()
    assert (gen.MODEL_DIR / "definition.pbism").exists()
    json_files = list(out.rglob("*.json")) + list(out.rglob("*.pbir")) + list(out.rglob("*.pbism")) + list(out.rglob("*.pbip"))
    assert len(json_files) >= 35
    for f in json_files:
        json.loads(f.read_text(encoding="utf-8"))


def test_every_model_table_has_a_partition_and_columns(project):
    gen, out = project
    columns, _ = _parse_tmdl_tables(gen.MODEL_DIR)
    assert set(columns) >= set(gen.MODEL_TABLES) | {"dim_date", "dim_fiscal_year", "_Measures"}
    for f in (gen.MODEL_DIR / "definition" / "tables").glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        assert "\tpartition " in text and "\t\tmode: import" in text, f.name
        assert "\tcolumn " in text, f.name


def test_relationships_reference_existing_columns(project):
    gen, _ = project
    columns, _ = _parse_tmdl_tables(gen.MODEL_DIR)
    for ft, fc, tt, tc in gen.RELATIONSHIPS:
        assert fc in columns[ft], f"{ft}.{fc}"
        assert tc in columns[tt], f"{tt}.{tc}"


def test_every_visual_field_exists_in_the_model(project):
    gen, _ = project
    columns, measures = _parse_tmdl_tables(gen.MODEL_DIR)
    refs: list[tuple[str, str, str]] = []
    n_visuals = 0
    for vf in gen.REPORT_DIR.rglob("visual.json"):
        n_visuals += 1
        _field_refs(json.loads(vf.read_text(encoding="utf-8")), refs)
    assert n_visuals == 28 and refs
    missing = [(k, t, p) for k, t, p in refs
               if p not in (columns if k == "Column" else measures).get(t, set())]
    assert missing == [], missing


def test_no_active_flag_on_table_or_card_projections(project):
    gen, _ = project
    for vf in gen.REPORT_DIR.rglob("visual.json"):
        v = json.loads(vf.read_text(encoding="utf-8"))["visual"]
        if v["visualType"] in ("tableEx", "cardVisual"):
            for role in v.get("query", {}).get("queryState", {}).values():
                assert not any(p.get("active") for p in role["projections"]), vf


def test_measures_have_descriptions_and_formats(project):
    gen, _ = project
    text = (gen.MODEL_DIR / "definition" / "tables" / "_Measures.tmdl").read_text(encoding="utf-8")
    assert text.count("\tmeasure ") == len(gen.MEASURES)
    assert text.count("\t/// ") >= len(gen.MEASURES)
    for name, dax, fmt, folder, desc in gen.MEASURES:
        assert desc and folder
        assert "/" not in dax.replace("//", ""), f"{name}: use DIVIDE() rather than /"


def test_pages_json_orders_every_page(project):
    gen, _ = project
    pages = json.loads((gen.REPORT_DIR / "definition" / "pages" / "pages.json").read_text(encoding="utf-8"))
    folders = {p.name for p in (gen.REPORT_DIR / "definition" / "pages").iterdir() if p.is_dir()}
    assert set(pages["pageOrder"]) == folders and pages["activePageName"] in folders
