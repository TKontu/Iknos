"""action_metrics: per-Action operational metrics column (R12, observability floor)

Revision ID: 0015_action_metrics
Revises: 0014_baseline_chunks
Create Date: 2026-06-12

Touches AGE graph: no (relational only)

The §6.1 cost discipline and Trials A/C need per-Action operational numbers — LLM token counts,
wall-clock durations, span counts — to measure where compute goes. This adds the `metrics` JSONB
column to `actions`, the observability floor the operators populate (`record_action(metrics=...)`):
extract/verify Actions carry `{duration_ms, prompt_tokens, completion_tokens, n_samples,
cache_hit}`, parse/segment Actions carry `{duration_ms, n_spans, n_skipped_whitespace}`. Keys are
omitted, never zeroed, when a source is absent.

`NOT NULL DEFAULT '{}'::jsonb` — like `inputs`/`outputs`, so every row (existing and new) has an
object to read and a consumer never hits a null. A constant default makes the `ADD COLUMN` a
metadata-only operation in Postgres (no table rewrite). Mirrored in `iknos.db.orm.Action.metrics`
so the autogenerate-drift gate (CI_MIGRATIONS.md §1.4) stays clean.

Search-path discipline (CI_MIGRATIONS.md §2): a prior AGE migration left
`search_path = ag_catalog, "$user", public` for the session and never reset it, so an unqualified
`ALTER TABLE actions` could resolve against the wrong schema. `upgrade()` and `downgrade()` both pin
`SET search_path = public` before the relational DDL. The revision id is kept short
(`alembic_version.version_num` is varchar(32)).
"""

from alembic import op

revision = "0015_action_metrics"
down_revision = "0014_baseline_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pin public: a prior AGE migration left search_path = ag_catalog,"$user",public for the
    # session, so unqualified relational DDL could land in the wrong schema (CI_MIGRATIONS.md §2).
    op.execute("SET search_path = public")
    op.execute("ALTER TABLE actions ADD COLUMN metrics JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("SET search_path = public")  # drops must resolve in public (CI_MIGRATIONS.md §2)
    op.execute("ALTER TABLE actions DROP COLUMN metrics")
