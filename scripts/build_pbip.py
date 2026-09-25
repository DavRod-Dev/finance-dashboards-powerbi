"""Generate the Power BI project (PBIP) from the exported dataset.

Reads the Parquet tables in ../data and writes a complete Power BI project
under ../powerbi:

    finance-dashboards.pbip
    finance-dashboards.SemanticModel/   TMDL: tables typed from the Parquet
                                        schemas, date and fiscal-year
                                        dimensions, relationships, measures
    finance-dashboards.Report/          PBIR: four pages of visuals

Everything Power BI Desktop needs to open, refresh and render the report is
produced here, so the model is reproducible and reviewable as text. The
only machine-specific value is the DataFolder parameter (the absolute path
of ../data), which Desktop exposes under Transform data > Manage parameters.

Run:  python scripts/build_pbip.py
Then: open powerbi/finance-dashboards.pbip in Power BI Desktop and Refresh.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "powerbi"
NAME = "finance-dashboards"
MODEL_DIR = OUT / f"{NAME}.SemanticModel"
REPORT_DIR = OUT / f"{NAME}.Report"

# ---------------------------------------------------------------------------
# helpers

def tag(*parts: str) -> str:
    """Deterministic lineage tag (a GUID) so regenerated files diff cleanly."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "finance-dashboards/" + "/".join(parts)))


def oid(*parts: str) -> str:
    """Deterministic 20-hex-character PBIR object name."""
    return hashlib.sha1(("/".join(parts)).encode()).hexdigest()[:20]


def q(name: str) -> str:
    """Quote a TMDL object name when it needs it."""
    return f"'{name}'" if any(c in name for c in " .=:'") else name


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, obj) -> None:
    write(path, json.dumps(obj, indent=2) + "\n")


# ---------------------------------------------------------------------------
# semantic model: tables from parquet

ARROW_TYPES = {
    "int8": "int64", "int16": "int64", "int32": "int64", "int64": "int64",
    "uint8": "int64", "uint16": "int64", "uint32": "int64", "uint64": "int64",
    "float": "double", "double": "double", "float32": "double", "float64": "double",
    "string": "string", "large_string": "string", "bool": "boolean",
    "date32[day]": "dateTime", "date64[ms]": "dateTime",
}

PERCENT_HINTS = ("margin", "pctile", "return", "drawdown", "share", "weight", "volatility",
                 "_var", "cvar", "growth", "roa", "roe", "to_assets", "to_liabilities",
                 "correlation", "level", "positive_share", "wc_", "re_", "ebit_", "equity_to")
PLAIN_DECIMAL = ("sharpe", "sortino", "calmar", "skew", "kurtosis", "z_double_prime", "identity_residual",
                 "asset_turnover", "equity_multiplier", "marginal", "component", "diversification_ratio",
                 "best_period", "worst_period", "periods")

# Tables in the model, their key column (dimensions) and hidden columns (foreign keys).
MODEL_TABLES = {
    "dim_company": {"key": "cik", "description": "One row per SEC filer: name, SIC code, sector label and the fiscal years reported."},
    "fundamentals_annual": {"hidden": ["cik", "fiscal_year"], "description": "One row per filer per fiscal year end: income statement, balance sheet and cash flow lines from the latest 10-K view."},
    "piotroski_f_score": {"hidden": ["cik", "fiscal_year"], "description": "Piotroski signals per filer-year. f_score is set only when all nine signals could be evaluated; f_score_partial sums the available ones."},
    "altman_z_double_prime": {"hidden": ["cik", "fiscal_year"], "description": "Altman Z''-score components and zone, latest fiscal year per non-financial filer with at least $100M of assets."},
    "sector_percentiles": {"hidden": ["cik", "fiscal_year"], "description": "Net margin, ROA and FCF margin percentile within the filer's two-digit SIC peer group and fiscal year."},
    "dupont": {"hidden": ["cik", "fiscal_year"], "description": "ROE decomposed into net margin x asset turnover x equity multiplier on average balances."},
    "portfolio_daily": {"hidden": ["date"], "description": "Portfolio and benchmark daily return, growth of 1 and drawdown from peak."},
    "asset_returns_long": {"hidden": ["date", "asset_id"], "description": "Daily return per asset."},
    "risk_contributions": {"hidden": ["asset_id"], "description": "Euler risk decomposition: weight, stand-alone volatility, marginal and component contribution, share of portfolio risk."},
    "monthly_returns_long": {"description": "Portfolio return per calendar month.", "sort": {"month_name": "month"}},
    "summary_metrics": {"description": "Performance summary for the portfolio and its benchmark."},
    "tail_risk": {"description": "Historical, parametric and Cornish-Fisher VaR and CVaR at 95% and 99%."},
    "correlation_long": {"description": "Pairwise correlation of daily asset returns."},
}


def format_for(column: str, dtype: str) -> str | None:
    if dtype != "double":
        return None
    if any(h in column for h in PLAIN_DECIMAL):
        return "0.00"
    if any(h in column for h in PERCENT_HINTS):
        return "0.0%"
    return "#,##0"


def parquet_columns(table: str) -> list[tuple[str, str]]:
    schema = pq.read_schema(DATA / f"{table}.parquet")
    cols = []
    for field in schema:
        t = str(field.type)
        if t.startswith("timestamp"):
            dt = "dateTime"
        else:
            dt = ARROW_TYPES.get(t)
            if dt is None:
                raise ValueError(f"{table}.{field.name}: unmapped arrow type {t}")
        cols.append((field.name, dt))
    return cols


def column_tmdl(table: str, name: str, dtype: str, *, hidden=False, key=False, fmt=None,
                sort_by=None, source=None) -> str:
    lines = [f"\tcolumn {q(name)}", f"\t\tdataType: {dtype}"]
    if key:
        lines.append("\t\tisKey")
    if hidden:
        lines.append("\t\tisHidden")
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")
    lines.append(f"\t\tlineageTag: {tag(table, name)}")
    lines.append("\t\tsummarizeBy: none")
    lines.append(f"\t\tsourceColumn: {source or name}")
    if sort_by:
        lines.append(f"\t\tsortByColumn: {q(sort_by)}")
    lines.append("")
    lines.append("\t\tannotation SummarizationSetBy = Automatic")
    if dtype == "dateTime":
        lines.append("")
        lines.append("\t\tannotation UnderlyingDateTimeDataType = Date")
    return "\n".join(lines) + "\n"


def partition_tmdl(table: str, m_lines: list[str]) -> str:
    body = "\n".join("\t\t\t\t" + line for line in m_lines)
    return (f"\tpartition {q(table)} = m\n\t\tmode: import\n\t\tsource =\n{body}\n\n"
            f"\tannotation PBI_ResultType = Table\n")


def parquet_table_tmdl(table: str, spec: dict) -> str:
    cols = parquet_columns(table)
    hidden = set(spec.get("hidden", []))
    key = spec.get("key")
    sort = spec.get("sort", {})
    out = [f"/// {spec['description']}", f"table {table}", f"\tlineageTag: {tag(table)}", ""]
    for name, dtype in cols:
        out.append(column_tmdl(table, name, dtype, hidden=name in hidden, key=(name == key),
                               fmt=format_for(name, dtype), sort_by=sort.get(name)))
    m = [
        "let",
        f'    Source = Parquet.Document(File.Contents(DataFolder & "\\{table}.parquet"))',
        "in",
        "    Source",
    ]
    out.append(partition_tmdl(table, m))
    return "\n".join(out)


def date_dim_tmdl(start: str, end: str) -> str:
    """Contiguous calendar built in M from the portfolio's first to last day."""
    table = "dim_date"
    out = ["/// Calendar covering the portfolio history, one row per day. Marked as the date table.",
           f"table {table}", f"\tlineageTag: {tag(table)}", "\tdataCategory: Time", ""]
    out.append(column_tmdl(table, "Date", "dateTime", key=True, fmt="yyyy-mm-dd"))
    out.append(column_tmdl(table, "Year", "int64", fmt="0"))
    out.append(column_tmdl(table, "Quarter", "string"))
    out.append(column_tmdl(table, "Month", "int64", fmt="0"))
    out.append(column_tmdl(table, "Month name", "string", sort_by="Month", source="Month name"))
    out.append(column_tmdl(table, "Year month", "string", sort_by="Year month key", source="Year month"))
    out.append(column_tmdl(table, "Year month key", "int64", hidden=True, fmt="0", source="Year month key"))
    y0, m0, d0 = start.split("-")
    y1, m1, d1 = end.split("-")
    m = [
        "let",
        f"    StartDate = #date({int(y0)}, {int(m0)}, {int(d0)}),",
        f"    EndDate = #date({int(y1)}, {int(m1)}, {int(d1)}),",
        "    Days = Duration.Days(EndDate - StartDate) + 1,",
        "    Dates = List.Dates(StartDate, Days, #duration(1, 0, 0, 0)),",
        '    AsTable = Table.FromList(Dates, Splitter.SplitByNothing(), {"Date"}),',
        '    Typed = Table.TransformColumnTypes(AsTable, {{"Date", type date}}),',
        '    Year = Table.AddColumn(Typed, "Year", each Date.Year([Date]), Int64.Type),',
        '    Quarter = Table.AddColumn(Year, "Quarter", each "Q" & Text.From(Date.QuarterOfYear([Date])), type text),',
        '    Month = Table.AddColumn(Quarter, "Month", each Date.Month([Date]), Int64.Type),',
        '    MonthName = Table.AddColumn(Month, "Month name", each Date.ToText([Date], "MMM"), type text),',
        '    YearMonth = Table.AddColumn(MonthName, "Year month", each Date.ToText([Date], "yyyy-MM"), type text),',
        '    YearMonthKey = Table.AddColumn(YearMonth, "Year month key", each Date.Year([Date]) * 100 + Date.Month([Date]), Int64.Type)',
        "in",
        "    YearMonthKey",
    ]
    out.append(partition_tmdl(table, m))
    return "\n".join(out)


def fiscal_year_dim_tmdl(lo: int, hi: int) -> str:
    table = "dim_fiscal_year"
    out = ["/// Fiscal-year dimension shared by every fundamentals fact, so one slicer filters them all.",
           f"table {table}", f"\tlineageTag: {tag(table)}", ""]
    out.append(column_tmdl(table, "fiscal_year", "int64", key=True, fmt="0"))
    m = [
        "let",
        f"    Years = {{{lo}..{hi}}},",
        '    AsTable = Table.FromList(Years, Splitter.SplitByNothing(), {"fiscal_year"}),',
        '    Typed = Table.TransformColumnTypes(AsTable, {{"fiscal_year", Int64.Type}})',
        "in",
        "    Typed",
    ]
    out.append(partition_tmdl(table, m))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# measures

MEASURES: list[tuple[str, str, str, str, str]] = [
    # (name, dax, format, folder, description)
    ("Filers", "DISTINCTCOUNT(fundamentals_annual[cik])", "#,##0", "Fundamentals", "Distinct SEC filers in the current filter context."),
    ("Filer-years", "COUNTROWS(fundamentals_annual)", "#,##0", "Fundamentals", "Rows of the annual statement table in context."),
    ("Revenue", "DIVIDE(SUM(fundamentals_annual[revenue]), 1e6)", "\\$#,##0\\M", "Fundamentals", "Revenue, $ millions."),
    ("Net income", "DIVIDE(SUM(fundamentals_annual[net_income]), 1e6)", "\\$#,##0\\M", "Fundamentals", "Net income, $ millions."),
    ("Operating cash flow", "DIVIDE(SUM(fundamentals_annual[operating_cash_flow]), 1e6)", "\\$#,##0\\M", "Fundamentals", "Cash from operations, $ millions."),
    ("Total assets", "DIVIDE(SUM(fundamentals_annual[total_assets]), 1e6)", "\\$#,##0\\M", "Fundamentals", "Total assets, $ millions."),
    ("Net margin", "DIVIDE([Net income], [Revenue])", "0.0%", "Fundamentals", "Net income over revenue."),
    ("Piotroski rows", "COUNTROWS(piotroski_f_score)", "#,##0", "Piotroski", "Filer-years with any Piotroski signal evaluated."),
    ("Scored filer-years", "CALCULATE(COUNTROWS(piotroski_f_score), NOT ISBLANK(piotroski_f_score[f_score]))", "#,##0", "Piotroski", "Filer-years where all nine signals could be evaluated and a full F-score exists."),
    ("Median F-score (partial)", "MEDIAN(piotroski_f_score[f_score_partial])", "0.0", "Piotroski", "Median of the sum of available signals. Compare with Signals available."),
    ("Avg F-score (partial)", "AVERAGE(piotroski_f_score[f_score_partial])", "0.00", "Piotroski", "Mean of the sum of available signals."),
    ("Signals available", "AVERAGE(piotroski_f_score[signals_available])", "0.0", "Piotroski", "Mean number of the nine Piotroski signals that could be evaluated."),
    ("Altman filers", "COUNTROWS(altman_z_double_prime)", "#,##0", "Altman", "Non-financial filers with at least $100M of assets and a Z''-score."),
    ("Share distress", "DIVIDE(CALCULATE(COUNTROWS(altman_z_double_prime), altman_z_double_prime[zone] = \"distress\"), COUNTROWS(altman_z_double_prime))", "0.0%", "Altman", "Share of scored filers in the Altman distress zone (Z'' below 1.1)."),
    ("Median Z''", "MEDIAN(altman_z_double_prime[z_double_prime])", "0.00", "Altman", "Median Altman Z''-score."),
    ("Avg net margin percentile", "AVERAGE(sector_percentiles[net_margin_pctile])", "0.0%", "Peers", "Mean percentile of net margin within SIC2 peer group."),
    ("Avg ROA percentile", "AVERAGE(sector_percentiles[roa_pctile])", "0.0%", "Peers", "Mean percentile of return on assets within SIC2 peer group."),
    ("Avg FCF margin percentile", "AVERAGE(sector_percentiles[fcf_margin_pctile])", "0.0%", "Peers", "Mean percentile of free-cash-flow margin within SIC2 peer group."),
    ("ROE", "AVERAGE(dupont[roe])", "0.0%", "DuPont", "Return on average equity."),
    ("DuPont net margin", "AVERAGE(dupont[net_margin])", "0.0%", "DuPont", "Net income over revenue (DuPont factor 1)."),
    ("Asset turnover", "AVERAGE(dupont[asset_turnover])", "0.00", "DuPont", "Revenue over average assets (DuPont factor 2)."),
    ("Equity multiplier", "AVERAGE(dupont[equity_multiplier])", "0.00", "DuPont", "Average assets over average equity (DuPont factor 3)."),
    ("Portfolio growth", "MAX(portfolio_daily[portfolio_growth])", "0.00", "Portfolio", "Growth of 1 invested in the portfolio; exact per date."),
    ("Benchmark growth", "MAX(portfolio_daily[benchmark_growth])", "0.00", "Portfolio", "Growth of 1 invested in the benchmark; exact per date."),
    ("Portfolio drawdown", "MIN(portfolio_daily[portfolio_drawdown])", "0.0%", "Portfolio", "Fraction below the running peak; the minimum over a period is its max drawdown."),
    ("Benchmark drawdown", "MIN(portfolio_daily[benchmark_drawdown])", "0.0%", "Portfolio", "Benchmark fraction below its running peak."),
    ("Return in period", "PRODUCTX(portfolio_daily, 1 + portfolio_daily[portfolio_return]) - 1", "0.0%", "Portfolio", "Compounded portfolio return over the dates in context."),
    ("Annualized return", "CALCULATE(MAX(summary_metrics[annualized_return]), summary_metrics[scope] = \"portfolio\")", "0.0%", "Summary", "Geometric annualised return of the portfolio over the full history."),
    ("Annualized volatility", "CALCULATE(MAX(summary_metrics[annualized_volatility]), summary_metrics[scope] = \"portfolio\")", "0.0%", "Summary", "Annualised standard deviation of daily returns."),
    ("Sharpe", "CALCULATE(MAX(summary_metrics[sharpe]), summary_metrics[scope] = \"portfolio\")", "0.00", "Summary", "Sharpe ratio at the configured risk-free rate."),
    ("Max drawdown", "CALCULATE(MAX(summary_metrics[max_drawdown]), summary_metrics[scope] = \"portfolio\")", "0.0%", "Summary", "Deepest peak-to-trough decline over the full history."),
    ("Benchmark annualized return", "CALCULATE(MAX(summary_metrics[annualized_return]), summary_metrics[scope] = \"benchmark\")", "0.0%", "Summary", "Benchmark geometric annualised return."),
    ("VaR 95 (1-day)", "CALCULATE(MAX(tail_risk[historical_var]), tail_risk[level] = 0.95)", "0.00%", "Tail risk", "Historical one-day value at risk at 95% confidence."),
    ("CVaR 95 (1-day)", "CALCULATE(MAX(tail_risk[historical_cvar]), tail_risk[level] = 0.95)", "0.00%", "Tail risk", "Expected shortfall beyond the 95% VaR."),
    ("VaR 99 (1-day)", "CALCULATE(MAX(tail_risk[historical_var]), tail_risk[level] = 0.99)", "0.00%", "Tail risk", "Historical one-day value at risk at 99% confidence."),
    ("Weight", "SUM(risk_contributions[weight])", "0.0%", "Risk contribution", "Target portfolio weight."),
    ("Share of risk", "SUM(risk_contributions[share])", "0.0%", "Risk contribution", "Euler component contribution as a share of portfolio volatility; sums to 100%."),
    ("Stand-alone volatility", "SUM(risk_contributions[volatility])", "0.0%", "Risk contribution", "Annualised volatility of the asset on its own."),
    ("Diversification ratio", "MAX(risk_contributions[diversification_ratio])", "0.00", "Risk contribution", "Weighted average stand-alone volatility over portfolio volatility."),
    ("Correlation", "AVERAGE(correlation_long[correlation])", "0.00", "Correlation", "Pearson correlation of daily returns."),
    # Company page: blank until exactly one filer is in context, so the page
    # never shows an average over six thousand companies as if it were one.
    ("Filer selected", "HASONEVALUE(dim_company[cik])", "", "Company page", "TRUE when exactly one filer is in the filter context."),
    ("Revenue (filer)", "VAR y = MAX(fundamentals_annual[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Revenue], fundamentals_annual[fiscal_year] = y))", "\\$#,##0\\M", "Company page", "Revenue of the selected filer in the latest fiscal year in context, $ millions."),
    ("Net income (filer)", "VAR y = MAX(fundamentals_annual[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Net income], fundamentals_annual[fiscal_year] = y))", "\\$#,##0\\M", "Company page", "Net income of the selected filer in the latest fiscal year in context, $ millions."),
    ("Operating cash flow (filer)", "VAR y = MAX(fundamentals_annual[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Operating cash flow], fundamentals_annual[fiscal_year] = y))", "\\$#,##0\\M", "Company page", "Operating cash flow of the selected filer in the latest fiscal year in context, $ millions."),
    ("ROE (filer)", "VAR y = MAX(dupont[fiscal_year]) RETURN IF([Filer selected], CALCULATE([ROE], dupont[fiscal_year] = y))", "0.0%", "Company page", "Return on average equity of the selected filer, latest year in context."),
    ("Avg ROA percentile (filer)", "VAR y = MAX(sector_percentiles[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Avg ROA percentile], sector_percentiles[fiscal_year] = y))", "0.0%", "Company page", "ROA percentile within peers for the selected filer, latest year in context."),
    ("Avg net margin percentile (filer)", "VAR y = MAX(sector_percentiles[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Avg net margin percentile], sector_percentiles[fiscal_year] = y))", "0.0%", "Company page", "Net margin percentile within peers for the selected filer, latest year in context."),
    ("Avg FCF margin percentile (filer)", "VAR y = MAX(sector_percentiles[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Avg FCF margin percentile], sector_percentiles[fiscal_year] = y))", "0.0%", "Company page", "FCF margin percentile within peers for the selected filer, latest year in context."),
    ("Avg F-score (filer)", "VAR y = MAX(piotroski_f_score[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Avg F-score (partial)], piotroski_f_score[fiscal_year] = y))", "0.00", "Company page", "Partial F-score of the selected filer, latest year in context."),
    ("Signals available (filer)", "VAR y = MAX(piotroski_f_score[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Signals available], piotroski_f_score[fiscal_year] = y))", "0.0", "Company page", "Signals evaluated for the selected filer, latest year in context."),
    ("DuPont net margin (filer)", "VAR y = MAX(dupont[fiscal_year]) RETURN IF([Filer selected], CALCULATE([DuPont net margin], dupont[fiscal_year] = y))", "0.0%", "Company page", "DuPont net margin of the selected filer, latest year in context."),
    ("Asset turnover (filer)", "VAR y = MAX(dupont[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Asset turnover], dupont[fiscal_year] = y))", "0.00", "Company page", "Asset turnover of the selected filer, latest year in context."),
    ("Equity multiplier (filer)", "VAR y = MAX(dupont[fiscal_year]) RETURN IF([Filer selected], CALCULATE([Equity multiplier], dupont[fiscal_year] = y))", "0.00", "Company page", "Equity multiplier of the selected filer, latest year in context."),
    ("Monthly return", "SUM(monthly_returns_long[monthly_return])", "0.0%", "Portfolio", "Compounded portfolio return for a calendar month."),
]


def measures_table_tmdl() -> str:
    table = "_Measures"
    out = ["/// Home of every explicit measure. The single column is hidden so the table shows as a measure table.",
           f"table {table}", f"\tlineageTag: {tag(table)}", ""]
    for name, dax, fmt, folder, desc in MEASURES:
        out.append(f"\t/// {desc}")
        out.append(f"\tmeasure {q(name)} = {dax}")
        if fmt:
            out.append(f"\t\tformatString: {fmt}")
        out.append(f"\t\tdisplayFolder: {folder}")
        out.append(f"\t\tlineageTag: {tag(table, name)}")
        out.append("")
    out.append(column_tmdl(table, "Column", "string", hidden=True))
    m = ["let", '    Source = #table(type table [Column = text], {})', "in", "    Source"]
    out.append(partition_tmdl(table, m))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# relationships

RELATIONSHIPS = [
    ("fundamentals_annual", "cik", "dim_company", "cik"),
    ("piotroski_f_score", "cik", "dim_company", "cik"),
    ("altman_z_double_prime", "cik", "dim_company", "cik"),
    ("sector_percentiles", "cik", "dim_company", "cik"),
    ("dupont", "cik", "dim_company", "cik"),
    ("fundamentals_annual", "fiscal_year", "dim_fiscal_year", "fiscal_year"),
    ("piotroski_f_score", "fiscal_year", "dim_fiscal_year", "fiscal_year"),
    ("altman_z_double_prime", "fiscal_year", "dim_fiscal_year", "fiscal_year"),
    ("sector_percentiles", "fiscal_year", "dim_fiscal_year", "fiscal_year"),
    ("dupont", "fiscal_year", "dim_fiscal_year", "fiscal_year"),
    ("portfolio_daily", "date", "dim_date", "Date"),
    ("asset_returns_long", "date", "dim_date", "Date"),
]


def relationships_tmdl() -> str:
    out = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        out.append(f"relationship {tag('rel', ft, fc, tt, tc)}\n\tfromColumn: {ft}.{q(fc)}\n\ttoColumn: {tt}.{q(tc)}\n")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# report: PBIR

VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.1.0/schema.json"
PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.4.0/schema.json"


def lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


AXIS_ROLES = {"Category", "Series", "X", "Rows", "Columns", "Values"}
NO_ACTIVE_VISUALS = {"tableEx", "cardVisual"}


def col(table: str, column: str, label: str | None = None) -> dict:
    out = {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}},
           "queryRef": f"{table}.{column}", "nativeQueryRef": column}
    if label:
        out["displayName"] = label
    return out


def mea(name: str, label: str | None = None) -> dict:
    out = {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": name}},
           "queryRef": f"_Measures.{name}", "nativeQueryRef": name}
    if label:
        out["displayName"] = label
    return out


def visual(page: str, key: str, vtype: str, x: float, y: float, w: float, h: float, *, roles: dict | None = None,
           title: str | None = None, objects: dict | None = None, sort: tuple[dict, str] | None = None,
           z: int = 1000) -> tuple[str, dict]:
    name = oid(page, key)
    v: dict = {"visualType": vtype, "drillFilterOtherVisuals": True}
    if roles:
        # Desktop marks axis/grouping column projections active (drill state);
        # table and card projections must not carry the flag, or the table
        # renderer throws while mixing columns and measures.
        state = {}
        for role, projs in roles.items():
            out = []
            for pr in projs:
                pr = dict(pr)
                if role in AXIS_ROLES and "Column" in pr["field"] and vtype not in NO_ACTIVE_VISUALS:
                    pr["active"] = True
                out.append(pr)
            state[role] = {"projections": out}
        query: dict = {"queryState": state}
        if sort:
            query["sortDefinition"] = {"sort": [{"field": sort[0]["field"], "direction": sort[1]}], "isDefaultSort": True}
        v["query"] = query
    if objects:
        v["objects"] = objects
    if title is not None:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": lit("true"), "text": lit(f"'{title}'")}}]}
    return name, {"$schema": VC_SCHEMA, "name": name,
                  "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": z},
                  "visual": v}


def textbox(page: str, key: str, text: str, x: float, y: float, w: float, h: float, size: str = "18pt") -> tuple[str, dict]:
    name = oid(page, key)
    return name, {"$schema": VC_SCHEMA, "name": name,
                  "position": {"x": x, "y": y, "z": 0, "height": h, "width": w, "tabOrder": 0},
                  "visual": {"visualType": "textbox",
                             "objects": {"general": [{"properties": {"paragraphs": [{"textRuns": [
                                 {"value": text, "textStyle": {"fontFamily": "Segoe UI Semibold", "fontSize": size, "color": "#252423"}}]}]}}]},
                             "drillFilterOtherVisuals": True}}


def slicer(page: str, key: str, table: str, column: str, x: float, y: float, w: float, h: float, *,
           single: bool = False, title: str | None = None) -> tuple[str, dict]:
    objects = {"data": [{"properties": {"mode": lit("'Dropdown'")}}],
               "header": [{"properties": {"show": lit("false")}}],
               "general": [{"properties": {}}]}
    if single:
        objects["selection"] = [{"properties": {"singleSelect": lit("true"), "strictSingleSelect": lit("true")}}]
    return visual(page, key, "slicer", x, y, w, h, roles={"Values": [col(table, column)]},
                  title=title, objects=objects)


def cards(page: str, key: str, measures: list[str], x: float, y: float, w: float, h: float,
          font: int = 26) -> tuple[str, dict]:
    objects = {
        "layout": [{"properties": {"orientation": lit("0D"), "columnCount": lit(f"{len(measures)}L"),
                                   "alignment": lit("'middle'"), "style": lit("'Cards'")}}],
        # labelDisplayUnits 1 = none (0 = auto): money measures already scale to $M in
        # their format string, so "auto" units would print "$0bnM".
        "value": [{"properties": {"fontSize": lit(f"{font}D"), "labelDisplayUnits": lit("1D")}, "selector": {"id": "default"}}],
        "padding": [{"properties": {"paddingSelection": lit("'Wide'")}, "selector": {"id": "default"}}],
        "accentBar": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
    }
    name, v = visual(page, key, "cardVisual", x, y, w, h, roles={"Data": [mea(m) for m in measures]}, objects=objects)
    v["visual"]["visualContainerObjects"] = {"title": [{"properties": {"show": lit("false")}}],
                                             "border": [{"properties": {"show": lit("false")}}]}
    return name, v


def fill_rule(field: dict, stops: list[tuple[str, float | None]]) -> dict:
    """Background colour rule for a table/matrix cell: two or three (colour, value) stops.

    The rule's Input becomes a query projection, which must be a measure or
    an aggregation: a bare column is rejected by the query engine, so column
    inputs are wrapped in Sum. A stop value of None means "auto" (data min/max).
    """
    inp = field["field"]
    if "Column" in inp:
        inp = {"Aggregation": {"Expression": {"Column": inp["Column"]}, "Function": 0}}
    key = "linearGradient2" if len(stops) == 2 else "linearGradient3"
    names = ["min", "max"] if len(stops) == 2 else ["min", "mid", "max"]
    grad = {}
    for n, (c, v) in zip(names, stops):
        stop = {"color": {"Literal": {"Value": f"'{c}'"}}}
        if v is not None:
            stop["value"] = {"Literal": {"Value": f"{v}D"}}
        grad[n] = stop
    grad["nullColoringStrategy"] = {"strategy": {"Literal": {"Value": "'asZero'"}}}
    return {"solid": {"color": {"expr": {"FillRule": {"Input": inp, "FillRule": {key: grad}}}}}}


def cell_colours(projections: list[dict], stops: list[tuple[str, float | None]]) -> list[dict]:
    """One `values` formatting entry per projection; Desktop keys these by the
    field's queryRef and a data wildcard selector."""
    return [{"properties": {"backColor": fill_rule(pr, stops)},
             "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": pr["queryRef"]}}
            for pr in projections]


RED_GREEN = [("#F5B7B1", 0.0), ("#A9DFBF", 1.0)]
BLUE_WHITE_RED = [("#5B8DEF", -1.0), ("#FFFFFF", 0.0), ("#E57373", 1.0)]

NO_TOTALS = {"subTotals": [{"properties": {"rowSubtotals": lit("false"), "columnSubtotals": lit("false")}}]}
NO_TABLE_TOTAL = {"total": [{"properties": {"totals": lit("false")}}]}
AXIS_NO_UNITS = {"valueAxis": [{"properties": {"labelDisplayUnits": lit("1D")}}]}


SIGNAL_COLS = [
    col("piotroski_f_score", "s_roa_positive", "ROA > 0"),
    col("piotroski_f_score", "s_ocf_positive", "OCF > 0"),
    col("piotroski_f_score", "s_roa_improved", "ROA up"),
    col("piotroski_f_score", "s_accruals", "OCF > NI"),
    col("piotroski_f_score", "s_leverage_fell", "Lev. down"),
    col("piotroski_f_score", "s_liquidity_rose", "Liq. up"),
    col("piotroski_f_score", "s_no_dilution", "No dilution"),
    col("piotroski_f_score", "s_margin_rose", "Margin up"),
    col("piotroski_f_score", "s_turnover_rose", "Turnover up"),
]

# The company page is a drill-through target on dim_company[filer_name]:
# right-click any filer on page 1 and choose Drill through > Company detail.
DRILL_FILTER = oid("filter", "company", "filer")
DRILL_BINDING = oid("binding", "company")
COMPANY_PAGE_EXTRA = {
    "filterConfig": {"filters": [{"name": DRILL_FILTER, "field": col("dim_company", "filer_name")["field"],
                                  "type": "Categorical", "howCreated": "Drillthrough"}]},
    "pageBinding": {"name": DRILL_BINDING, "type": "Drillthrough",
                    "parameters": [{"name": oid("param", "company", "filer"), "boundFilter": DRILL_FILTER,
                                    "fieldExpr": col("dim_company", "filer_name")["field"]}]},
}
PAGE_EXTRA = {"company": COMPANY_PAGE_EXTRA}


def build_pages() -> dict[str, tuple[str, list[tuple[str, dict]]]]:
    """page key -> (display name, [(visual name, visual json)])."""
    pages: dict[str, tuple[str, list[tuple[str, dict]]]] = {}

    # ---- page 1: fundamentals screen
    p = "fundamentals"
    vs = [
        textbox(p, "title", "Fundamentals screen - every US filer, SEC Financial Statement Data Sets 2025q1-q4", 24, 12, 1000, 40),
        slicer(p, "sector", "dim_company", "sector", 24, 60, 300, 60, title="Sector"),
        slicer(p, "year", "dim_fiscal_year", "fiscal_year", 340, 60, 200, 60, title="Fiscal year"),
        slicer(p, "status", "dim_company", "filer_status", 556, 60, 260, 60, title="Filer status (SEC size class)"),
        cards(p, "kpis", ["Filers", "Piotroski rows", "Scored filer-years", "Median F-score (partial)", "Share distress"], 24, 132, 1232, 100),
        visual(p, "fscore", "columnChart", 24, 248, 400, 214,
               roles={"Category": [col("piotroski_f_score", "f_score_partial")], "Y": [mea("Piotroski rows")]},
               title="Filer-years by partial F-score (sum of available signals)"),
        visual(p, "zones", "hundredPercentStackedBarChart", 440, 248, 400, 214,
               roles={"Category": [col("dim_company", "sector")], "Series": [col("altman_z_double_prime", "zone")], "Y": [mea("Altman filers")]},
               title="Altman zone share by sector (>= $100M assets, non-financial)"),
        visual(p, "scatter", "scatterChart", 856, 248, 400, 214,
               roles={"Category": [col("dim_company", "filer_name")], "X": [mea("Avg ROA percentile")],
                      "Y": [mea("Avg net margin percentile")], "Size": [mea("Total assets")]},
               title="ROA percentile vs net-margin percentile, sized by assets"),
        visual(p, "table", "tableEx", 24, 478, 1232, 226,
               roles={"Values": [col("dim_company", "filer_name", "Filer"), col("dim_fiscal_year", "fiscal_year", "Year"),
                                 col("piotroski_f_score", "f_score_partial", "Partial F"),
                                 col("piotroski_f_score", "signals_available", "Signals"), *SIGNAL_COLS]},
               sort=(col("piotroski_f_score", "f_score_partial"), "Descending"),
               title="Filers by partial F-score with the nine signals (green = 1, red = 0); right-click a filer to drill through",
               objects={**NO_TABLE_TOTAL, "values": cell_colours(SIGNAL_COLS, RED_GREEN)}),
    ]
    pages[p] = ("Fundamentals screen", vs)

    # ---- page 2: company detail
    p = "company"
    vs = [
        textbox(p, "title", "Company detail - pick a filer, or drill through from the fundamentals screen", 24, 12, 1100, 40),
        slicer(p, "company", "dim_company", "filer_name", 24, 60, 520, 60, single=True, title="Filer (type to search)"),
        slicer(p, "sector", "dim_company", "sector", 560, 60, 300, 60, title="Sector"),
        cards(p, "kpis", ["Revenue (filer)", "Net income (filer)", "Operating cash flow (filer)", "ROE (filer)", "Signals available (filer)"], 24, 132, 1232, 100),
        visual(p, "lines", "clusteredColumnChart", 24, 248, 608, 220,
               roles={"Category": [col("dim_fiscal_year", "fiscal_year")],
                      "Y": [mea("Revenue (filer)"), mea("Net income (filer)"), mea("Operating cash flow (filer)")]},
               title="Revenue, net income and operating cash flow by fiscal year ($M)", objects=AXIS_NO_UNITS),
        visual(p, "fscore", "lineChart", 648, 248, 608, 220,
               roles={"Category": [col("dim_fiscal_year", "fiscal_year")], "Y": [mea("Avg F-score (filer)"), mea("Signals available (filer)")]},
               title="Partial F-score and signals available by fiscal year"),
        visual(p, "dupont", "tableEx", 24, 484, 608, 220,
               roles={"Values": [col("dim_fiscal_year", "fiscal_year", "Year"), mea("DuPont net margin (filer)", "Net margin"),
                                 mea("Asset turnover (filer)", "Asset turnover"), mea("Equity multiplier (filer)", "Equity multiplier"),
                                 mea("ROE (filer)", "ROE")]},
               title="DuPont: ROE = net margin x asset turnover x equity multiplier", objects=NO_TABLE_TOTAL),
        visual(p, "peers", "tableEx", 648, 484, 608, 220,
               roles={"Values": [col("dim_fiscal_year", "fiscal_year", "Year"), mea("Avg net margin percentile (filer)", "Net margin pctile"),
                                 mea("Avg ROA percentile (filer)", "ROA pctile"), mea("Avg FCF margin percentile (filer)", "FCF margin pctile")]},
               title="Percentile within SIC2 peer group by fiscal year", objects=NO_TABLE_TOTAL),
    ]
    pages[p] = ("Company detail", vs)

    # ---- page 3: portfolio risk
    p = "portfolio"
    vs = [
        textbox(p, "title", "Portfolio risk - multi-asset proxy book, FRED index levels", 24, 12, 900, 40),
        cards(p, "kpis", ["Annualized return", "Annualized volatility", "Sharpe", "Max drawdown", "VaR 95 (1-day)", "CVaR 95 (1-day)"], 24, 60, 1232, 100),
        visual(p, "growth", "lineChart", 24, 176, 740, 260,
               roles={"Category": [col("dim_date", "Date")], "Y": [mea("Portfolio growth"), mea("Benchmark growth")]},
               title="Growth of 1: portfolio vs S&P 500"),
        visual(p, "risk", "clusteredBarChart", 780, 176, 476, 260,
               roles={"Category": [col("risk_contributions", "asset")], "Y": [mea("Share of risk"), mea("Weight")]},
               sort=(mea("Share of risk"), "Descending"),
               title="Share of risk vs weight (Euler contributions)"),
        visual(p, "drawdown", "areaChart", 24, 452, 740, 250,
               roles={"Category": [col("dim_date", "Date")], "Y": [mea("Portfolio drawdown"), mea("Benchmark drawdown")]},
               title="Drawdown from peak"),
        visual(p, "vol", "tableEx", 780, 452, 476, 250,
               roles={"Values": [col("risk_contributions", "asset", "Asset"), mea("Weight"), mea("Stand-alone volatility", "Volatility"), mea("Share of risk")]},
               sort=(mea("Share of risk"), "Descending"),
               title="Risk contribution table", objects=NO_TABLE_TOTAL),
    ]
    pages[p] = ("Portfolio risk", vs)

    # ---- page 4: correlation and calendar
    p = "calendar"
    vs = [
        textbox(p, "title", "Correlation and monthly returns", 24, 12, 800, 40),
        cards(p, "period", ["Return in period", "Diversification ratio", "VaR 99 (1-day)"], 24, 60, 700, 100, font=22),
        slicer(p, "dates", "dim_date", "Date", 740, 60, 516, 100, title="Date range (filters the cards and the monthly grid)"),
        visual(p, "corr", "pivotTable", 24, 176, 1232, 250,
               roles={"Rows": [col("correlation_long", "asset_a_label", "Asset")], "Columns": [col("correlation_long", "asset_b_label", "vs")],
                      "Values": [mea("Correlation")]},
               title="Correlation of daily returns (blue = -1, white = 0, red = +1)",
               objects={**NO_TOTALS, "values": cell_colours([mea("Correlation")], BLUE_WHITE_RED)}),
        visual(p, "monthly", "pivotTable", 24, 442, 1232, 262,
               roles={"Rows": [col("monthly_returns_long", "year", "Year")], "Columns": [col("monthly_returns_long", "month_name", "Month")],
                      "Values": [mea("Monthly return")]},
               title="Monthly portfolio returns (red = loss, green = gain)",
               objects={**NO_TOTALS, "values": cell_colours([mea("Monthly return")], [("#F5B7B1", -0.08), ("#FFFFFF", 0.0), ("#A9DFBF", 0.08)])}),
    ]
    # the date slicer should be a between-slider, not a dropdown
    vs[2][1]["visual"]["objects"] = {"data": [{"properties": {"mode": lit("'Between'")}}], "general": [{"properties": {}}]}
    pages[p] = ("Correlation & calendar", vs)
    return pages


def report_json() -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"}, "type": "SharedResources"}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources",
                              "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultFilterActionIsDataFilter": True,
                     "defaultDrillFilterOtherVisuals": True, "allowChangeFilterTypes": True,
                     "allowInlineExploration": True, "useEnhancedTooltips": True},
    }


# ---------------------------------------------------------------------------
# main

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)

    # --- semantic model
    tables = []
    for table, spec in MODEL_TABLES.items():
        write(MODEL_DIR / "definition" / "tables" / f"{table}.tmdl", parquet_table_tmdl(table, spec))
        tables.append(table)

    pd = pq.read_table(DATA / "portfolio_daily.parquet", columns=["date"]).to_pandas()
    start, end = pd["date"].min().strftime("%Y-%m-%d"), pd["date"].max().strftime("%Y-%m-%d")
    write(MODEL_DIR / "definition" / "tables" / "dim_date.tmdl", date_dim_tmdl(start, end))
    years = []
    for t in ("fundamentals_annual", "piotroski_f_score", "altman_z_double_prime", "sector_percentiles", "dupont"):
        s = pq.read_table(DATA / f"{t}.parquet", columns=["fiscal_year"]).to_pandas()["fiscal_year"]
        years += [int(s.min()), int(s.max())]
    write(MODEL_DIR / "definition" / "tables" / "dim_fiscal_year.tmdl", fiscal_year_dim_tmdl(min(years), max(years)))
    write(MODEL_DIR / "definition" / "tables" / "_Measures.tmdl", measures_table_tmdl())
    tables = ["dim_company", "dim_fiscal_year", "dim_date", *[t for t in tables if t != "dim_company"], "_Measures"]

    write(MODEL_DIR / "definition" / "relationships.tmdl", relationships_tmdl())
    data_folder = str(DATA).replace("/", "\\")
    write(MODEL_DIR / "definition" / "expressions.tmdl",
          f'expression DataFolder = "{data_folder}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
          f"\tlineageTag: {tag('expr', 'DataFolder')}\n\n\tannotation PBI_ResultType = Text\n")
    write(MODEL_DIR / "definition" / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n\tcompatibilityMode: powerBI\n")
    order = json.dumps(["DataFolder", *tables])
    write(MODEL_DIR / "definition" / "model.tmdl",
          "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\tsourceQueryCulture: en-US\n"
          "\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
          f"annotation PBI_QueryOrder = {order}\n\nannotation __PBI_TimeIntelligenceEnabled = 0\n\n"
          + "".join(f"ref table {q(t)}\n" for t in tables))
    write_json(MODEL_DIR / "definition.pbism", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {"qnaEnabled": True}})
    write_json(MODEL_DIR / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": NAME}, "config": {"version": "2.0", "logicalId": tag("platform", "model")}})

    # --- report
    pages = build_pages()
    for key, (display, visuals) in pages.items():
        pname = oid("page", key)
        page_json = {"$schema": PAGE_SCHEMA, "name": pname, "displayName": display, "displayOption": "FitToPage",
                     "height": 720, "width": 1280, **PAGE_EXTRA.get(key, {})}
        write_json(REPORT_DIR / "definition" / "pages" / pname / "page.json", page_json)
        for vname, vjson in visuals:
            write_json(REPORT_DIR / "definition" / "pages" / pname / "visuals" / vname / "visual.json", vjson)
    write_json(REPORT_DIR / "definition" / "pages" / "pages.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [oid("page", k) for k in pages], "activePageName": oid("page", next(iter(pages)))})
    write_json(REPORT_DIR / "definition" / "version.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write_json(REPORT_DIR / "definition" / "report.json", report_json())
    write_json(REPORT_DIR / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    write_json(REPORT_DIR / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": NAME}, "config": {"version": "2.0", "logicalId": tag("platform", "report")}})
    write_json(OUT / f"{NAME}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})

    n_visuals = sum(len(v) for _, v in pages.values())
    print(f"[pbip] {len(tables)} tables, {len(MEASURES)} measures, {len(RELATIONSHIPS)} relationships, "
          f"{len(pages)} pages, {n_visuals} visuals -> {OUT}")


if __name__ == "__main__":
    main()
