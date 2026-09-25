"""Validate the exported star schema in data/.

The export script runs locally against the sibling repositories' databases;
CI cannot rebuild the data, so it checks what was committed: every table
the README lists exists, is non-empty, is unique on its stated grain, and
every fact's foreign key resolves to its dimension. If the model diagram in
the README says a relationship is one-to-many, this file proves it.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

DATA = Path(__file__).resolve().parents[1] / "data"

# table -> (grain columns, minimum rows)
GRAIN = {
    "dim_company": (["cik"], 1000),
    # fiscal_year_end, not fiscal_year: a handful of filers changed their year
    # end and legitimately have two statements in one calendar year.
    "fundamentals_annual": (["cik", "fiscal_year_end"], 5000),
    "piotroski_f_score": (["cik", "fiscal_year"], 1000),
    "altman_z_double_prime": (["cik"], 500),
    "sector_percentiles": (["cik", "fiscal_year"], 500),
    "dupont": (["cik", "fiscal_year"], 500),
    "mdw_dim_company": (["cik"], 5),
    "mdw_fundamental_metrics": (["cik", "fiscal_year"], 50),
    "portfolio_daily": (["date"], 1000),
    "asset_returns_long": (["date", "asset_id"], 5000),
    "asset_levels_long": (["date", "asset_id"], 5000),
    "risk_contributions": (["asset_id"], 3),
    "monthly_returns": (["year"], 3),
    "monthly_returns_long": (["year", "month"], 30),
    "summary_metrics": (["scope"], 2),
    "tail_risk": (["level"], 2),
    "correlation_long": (["asset_a", "asset_b"], 9),
}

# fact -> dimension it must join to on cik
COMPANY_FACTS = ["fundamentals_annual", "piotroski_f_score", "altman_z_double_prime", "sector_percentiles", "dupont"]


@pytest.fixture(scope="module")
def tables() -> dict[str, pd.DataFrame]:
    return {name: pd.read_parquet(DATA / f"{name}.parquet") for name in GRAIN}


def test_every_table_in_the_model_is_present():
    missing = [name for name in GRAIN if not (DATA / f"{name}.parquet").exists()]
    assert missing == []


@pytest.mark.parametrize("name", list(GRAIN))
def test_table_is_unique_on_its_grain_and_populated(tables, name):
    df, (keys, min_rows) = tables[name], GRAIN[name]
    assert len(df) >= min_rows, f"{name}: {len(df)} rows"
    assert not df.duplicated(keys).any(), f"{name}: duplicate keys on {keys}"
    assert not df[keys].isna().any().any(), f"{name}: NULL in key columns {keys}"


@pytest.mark.parametrize("fact", COMPANY_FACTS)
def test_company_facts_resolve_to_dim_company(tables, fact):
    orphans = set(tables[fact]["cik"]) - set(tables["dim_company"]["cik"])
    assert orphans == set(), f"{fact}: {len(orphans)} ciks missing from dim_company"


def test_daily_facts_share_the_date_range(tables):
    daily = pd.to_datetime(tables["portfolio_daily"]["date"])
    assets = pd.to_datetime(tables["asset_returns_long"]["date"])
    assert daily.min() == assets.min() and daily.max() == assets.max()
    assert daily.is_monotonic_increasing
    assert (daily.dt.dayofweek < 5).all()


def test_asset_dimension_is_consistent_across_portfolio_tables(tables):
    ids = set(tables["risk_contributions"]["asset_id"])
    assert set(tables["asset_returns_long"]["asset_id"]) == ids
    assert set(tables["asset_levels_long"]["asset_id"]) == ids
    corr = tables["correlation_long"]
    assert set(corr["asset_a"]) == ids and len(corr) == len(ids) ** 2
    diagonal = corr[corr["asset_a"] == corr["asset_b"]]["correlation"]
    assert (diagonal.round(9) == 1.0).all()


def test_risk_contributions_sum_to_one(tables):
    rc = tables["risk_contributions"]
    assert rc["share"].sum() == pytest.approx(1.0, abs=1e-6)
    assert rc["weight"].sum() == pytest.approx(1.0, abs=1e-6)


def test_scores_are_in_range(tables):
    f = tables["piotroski_f_score"]
    scored = f[f["f_score"].notna()]
    assert scored["f_score"].between(0, 9).all() and scored["signals_available"].eq(9).all()
    assert f["signals_available"].between(0, 9).all()
    assert (f["f_score_partial"].fillna(0) <= f["signals_available"]).all()
    assert (scored["f_score_partial"] == scored["f_score"]).all()
    z = tables["altman_z_double_prime"]
    assert set(z["zone"]) <= {"safe", "grey", "distress"}
    assert not z["sic2"].between(60, 67).any()             # financials excluded upstream
    p = tables["sector_percentiles"]
    assert p["net_margin_pctile"].between(0, 1).all() and (p["peers"] >= 20).all()


def test_dim_company_has_sector_labels_and_year_span(tables):
    d = tables["dim_company"]
    assert d["sector"].notna().all() and d["sector"].nunique() >= 5
    assert (d["last_fiscal_year"] >= d["first_fiscal_year"]).all()
    assert (d["years_reported"] >= 1).all()


def test_summary_and_tail_tables_carry_portfolio_and_benchmark(tables):
    assert set(tables["summary_metrics"]["scope"]) == {"portfolio", "benchmark"}
    t = tables["tail_risk"].set_index("level")
    assert set(t.index) == {0.95, 0.99}
    assert (t.loc[0.99] >= t.loc[0.95]).all()
