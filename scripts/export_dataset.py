"""Export the Power BI dataset from the three sibling portfolio repos.

Reads the DuckDB databases built by equity-fundamentals-sql and
market-data-warehouse, and the cached FRED series used by
portfolio-risk-report, and writes tidy tables as Parquet (primary) and
CSV (fallback) into ../data. Power BI Desktop reads both natively.

Run from anywhere:  python scripts/export_dataset.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PORTFOLIO = ROOT.parent
OUT = ROOT / "data"
EFS = PORTFOLIO / "equity-fundamentals-sql"
MDW = PORTFOLIO / "market-data-warehouse"
PRR = PORTFOLIO / "portfolio-risk-report"

# SIC two-digit major groups -> a readable sector label for slicers.
SIC2_SECTOR = {
    **{i: "Agriculture, forestry, fishing" for i in range(1, 10)},
    **{i: "Mining & oil/gas" for i in range(10, 15)},
    **{i: "Construction" for i in range(15, 18)},
    **{i: "Manufacturing" for i in range(20, 40)},
    **{i: "Transport, utilities & communications" for i in range(40, 50)},
    **{i: "Wholesale trade" for i in range(50, 52)},
    **{i: "Retail trade" for i in range(52, 60)},
    **{i: "Finance, insurance & real estate" for i in range(60, 68)},
    **{i: "Services" for i in range(70, 90)},
    **{i: "Public administration" for i in range(91, 100)},
}


def write(df: pd.DataFrame, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT / f"{name}.parquet", index=False)
    df.to_csv(OUT / f"{name}.csv", index=False)
    print(f"[write] {name:28s} {len(df):>9,} rows x {df.shape[1]:>2} cols")


def run_sql_file(con: duckdb.DuckDBPyConnection, path: Path) -> pd.DataFrame:
    return con.execute(path.read_text(encoding="utf-8")).df()


def export_fundamentals() -> None:
    con = duckdb.connect(str(EFS / "data" / "fundamentals.duckdb"), read_only=True)
    q = EFS / "sql" / "queries"

    annual = con.execute("""
        SELECT cik, filer_name, sic, sic2, filer_status, fiscal_year, fiscal_year_end,
               revenue, gross_profit, operating_income, pretax_income, net_income,
               operating_cash_flow, capex, free_cash_flow,
               total_assets, current_assets, current_liabilities, total_liabilities,
               equity, retained_earnings, cash, long_term_debt, working_capital,
               shares_outstanding, source_filed
        FROM core.annual
        ORDER BY cik, fiscal_year_end
    """).df()
    annual["sector"] = annual["sic2"].map(SIC2_SECTOR).fillna("Unclassified")
    write(annual, "fundamentals_annual")

    companies = (annual.sort_values("fiscal_year_end")
                 .groupby("cik", as_index=False)
                 .agg(filer_name=("filer_name", "last"), sic=("sic", "last"), sic2=("sic2", "last"),
                      sector=("sector", "last"), filer_status=("filer_status", "last"),
                      first_fiscal_year=("fiscal_year", "min"), last_fiscal_year=("fiscal_year", "max"),
                      years_reported=("fiscal_year", "nunique")))
    write(companies, "dim_company")

    piotroski = run_sql_file(con, q / "05_piotroski_f_score.sql")
    # Upstream reports f_score only when all nine signals could be evaluated,
    # which on SEC data is a small minority (long-term debt and share counts
    # are the usual gaps). For the dashboard, also carry the sum of the
    # signals that were available so a partial score can be shown as
    # "n of signals_available", labelled as such.
    signal_cols = [c for c in piotroski.columns if c.startswith("s_")]
    piotroski["f_score_partial"] = piotroski[signal_cols].sum(axis=1, min_count=1).astype("Int32")
    write(piotroski, "piotroski_f_score")
    write(run_sql_file(con, q / "06_altman_z_double_prime.sql"), "altman_z_double_prime")
    write(run_sql_file(con, q / "03_sector_percentiles.sql"), "sector_percentiles")
    write(run_sql_file(con, q / "07_dupont.sql"), "dupont")
    con.close()


def export_warehouse() -> None:
    con = duckdb.connect(str(MDW / "data" / "warehouse.duckdb"), read_only=True)
    write(con.execute("SELECT * FROM marts.mart_fundamental_metrics ORDER BY ticker, fiscal_year").df(),
          "mdw_fundamental_metrics")
    write(con.execute("SELECT * FROM marts.dim_company ORDER BY ticker").df(), "mdw_dim_company")
    con.close()


def export_risk() -> None:
    sys.path.insert(0, str(PRR / "src"))
    from prr import config, data, report, returns as R  # noqa: E402

    portfolio = config.load_portfolio(PRR / "config" / "portfolio.json")
    client = data.FredClient(cache_dir=PRR / "data", max_age_days=10_000)  # cached CSVs only, no network
    prices = data.build_price_panel(portfolio, client=client, log=lambda *_: None)
    rd = report.compute(portfolio, prices)
    labels = {a.id: a.label for a in portfolio.assets}

    daily = pd.DataFrame({
        "date": rd.port.index,
        "portfolio_return": rd.port.values,
        "benchmark_return": rd.bench.values if rd.bench is not None else None,
        "portfolio_growth": R.growth_index(rd.port).values,
        "benchmark_growth": R.growth_index(rd.bench).values if rd.bench is not None else None,
        "portfolio_drawdown": R.drawdowns(rd.port).values,
        "benchmark_drawdown": R.drawdowns(rd.bench).values if rd.bench is not None else None,
    })
    write(daily, "portfolio_daily")

    asset_long = (rd.asset_returns.reset_index().melt(id_vars=rd.asset_returns.index.name or "index",
                                                        var_name="asset_id", value_name="daily_return"))
    asset_long.columns = ["date", "asset_id", "daily_return"]
    asset_long["asset"] = asset_long["asset_id"].map(labels)
    write(asset_long, "asset_returns_long")

    prices_long = prices.reset_index().melt(id_vars=prices.index.name or "index", var_name="asset_id", value_name="level")
    prices_long.columns = ["date", "asset_id", "level"]
    prices_long["asset"] = prices_long["asset_id"].map(labels)
    write(prices_long, "asset_levels_long")

    contrib = rd.contributions.reset_index().rename(columns={"index": "asset_id"})
    if "asset_id" not in contrib.columns:
        contrib = contrib.rename(columns={contrib.columns[0]: "asset_id"})
    contrib["asset"] = contrib["asset_id"].map(labels)
    write(contrib, "risk_contributions")

    monthly = rd.monthly.reset_index()
    write(monthly, "monthly_returns")

    summ = pd.DataFrame([{"scope": "portfolio", **rd.summary}]
                        + ([{"scope": "benchmark", **rd.bench_summary}] if rd.bench_summary else []))
    write(summ, "summary_metrics")

    write(rd.tails.reset_index(), "tail_risk")
    corr = rd.correlation.reset_index().melt(id_vars=rd.correlation.index.name or "index", var_name="asset_b", value_name="correlation")
    corr.columns = ["asset_a", "asset_b", "correlation"]
    write(corr, "correlation_long")


if __name__ == "__main__":
    export_fundamentals()
    export_warehouse()
    export_risk()
    print(f"\nDataset written to {OUT}")
