# CLAUDE.md

Project rules for Claude Code, and how this repo is being built with it.

## Conventions the tool must keep

- **This is the reporting layer only.** SQL and Python live in the three
  sibling repositories. Nothing here recomputes a metric; the export script
  reads what they produced and reshapes it for a BI model.
- **The `.pbix` is built by hand, by the human.** Claude Code does not
  generate Power BI files. The dashboards are the skill being demonstrated,
  so the human builds the pages in Power BI Desktop following the specs in
  `README.md`; the tool helps with the dataset, the model, DAX and review.
- **Parquet is the committed format**, CSV is a local fallback and ignored.
  Every table in `data/` must be validated by `tests/test_dataset.py`:
  non-empty, unique on its stated grain, foreign keys resolving to
  `dim_company`, dates parseable. A table that is not tested is not in the
  model.
- **Relationships are single direction, dimension to fact**, and measures
  live in a `_Measures` table. Keep the README model diagram in step with
  the `.pbix`.
- **Nothing proprietary, no keys, no contact details.** Same rule as the
  sibling repositories.

## How this is being built

The dataset export, star schema, page specifications and DAX starters were
drafted with Claude Code on 2026-09-23 from the outputs of the three sibling
repositories. Power BI Desktop is then used by hand to build the model and
the three report pages; the tool's role at that stage is reviewing the DAX,
checking that visuals match the specs, and validating the exported data.
The repository is published once the `.pbix` and a PDF export exist.
