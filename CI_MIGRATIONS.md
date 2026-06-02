# CI & Apache AGE migration gotchas

This is the Iknos-specific companion to `MIGRATIONS.md` (which is generic Alembic
guidance carried over from a prior project). It records the **non-obvious traps
that come from running Alembic against Apache AGE**, and the invariants the
`migrations` CI workflow (`.github/workflows/migrations.yml`) enforces so they
don't regress.

Read this **before writing any new migration or touching `alembic/env.py`.**

> Host rule: you cannot `docker compose up` on this host (it runs other people's
> live containers). The `migrations` CI workflow is the *only* place migrations
> are exercised against a live AGE database. Treat a green CI run as the gate.

## The CI invariants (what the workflow checks)

The workflow builds the real AGE + pgvector image (`docker/postgres.Dockerfile`)
and enforces, on every change under `alembic/**`, `src/iknos/db/orm.py`,
`docker/postgres.Dockerfile`, `pyproject.toml`, or `uv.lock`:

1. `alembic upgrade head` applies cleanly from an **empty** database.
2. `alembic downgrade base` reverses **every** revision.
3. `alembic upgrade head` re-applies after the downgrade (revisions are
   idempotent across a full down/up cycle).
4. `alembic revision --autogenerate` produces an **empty** migration — the ORM
   models and the migrations agree (relational schema only; AGE graph DDL is
   `op.execute` and exempt).

## The root cause behind almost every failure: role name == graph name

The DB role is `iknos` **and** `create_graph('iknos')` creates a Postgres schema
named `iknos`. The default `search_path` is `"$user", public`, so **once the
graph exists, `"$user"` resolves to the AGE graph schema** and
`current_schema()` becomes `iknos` instead of `public`. This single collision
caused all three CI failures below.

If we ever rename the role or the graph so they differ, several of these
workarounds become unnecessary — but the code is written to be correct
regardless, so leave the guards in place.

## The three bugs we hit (symptom → cause → fix)

### 1. Dead base image tag
- **Symptom:** CI build step fails in ~9s: `docker.io/apache/age:PG16_latest: not found`.
- **Cause:** `apache/age` removed the rolling `PG16_latest` tag from Docker Hub.
- **Fix:** pin an explicit release tag in `docker/postgres.Dockerfile`
  (`apache/age:release_PG16_1.6.0`). **Never use a rolling/`latest`-style tag** —
  it is both irreproducible and silently disappears. Verify a replacement exists
  first: `curl -s https://hub.docker.com/v2/repositories/apache/age/tags/`.

### 2. Relational tables created in `ag_catalog`, not `public`
- **Symptom:** `downgrade base` fails: `index "ix_actions_actor_type" does not exist`.
- **Cause:** the migration sets `search_path = ag_catalog, "$user", public` for the
  AGE graph DDL and **never reset it**, so the unqualified `op.create_table` /
  `op.create_index` calls landed in `ag_catalog`. On downgrade the connection's
  default search_path is `public`, so the drops couldn't find them.
- **Fix:** **reset `op.execute("SET search_path = public")` before any relational
  DDL** in `upgrade()`, and pin `SET search_path = public` at the top of
  `downgrade()` before the relational drops. Keep graph DDL and relational DDL
  schema-separated within the migration.
- **Note:** a `DROP INDEX IF EXISTS` band-aid does NOT fix this — it only moves
  the failure to the next `drop_table` and leaves orphaned tables polluting
  `ag_catalog`. Fix the *placement*, not the symptom.

### 3. Autogenerate drift check sees the graph schema
- **Symptoms (in order, as each layer was fixed):**
  - autogenerate wants to `drop_table` every AGE vertex/edge label table
    (`Document`, `Span`, `_ag_label_vertex`, …); then
  - `FAILED: Target database is not up to date.`
- **Cause:** because `current_schema()` is the `iknos` graph schema (see root
  cause), autogenerate reflects the graph tables, **and** SQLAlchemy caches
  `default_schema_name` from `current_schema()` **at connect time** — so
  `alembic_version` and the reflection target land in the graph schema,
  inconsistently across the up/down/up cycle (the graph schema doesn't exist yet
  during the first upgrade, but does by the drift-check step).
- **Fix (`alembic/env.py`):**
  - Pin `SET search_path TO public` in a **`@event.listens_for(engine, "connect")`**
    listener. This must run at *connect* time — doing it with `exec_driver_sql`
    after `connect()` is too late, because the dialect has already cached the
    default schema.
  - Add an `include_object` filter that excludes anything whose schema is not
    `None`/`public`, as defense in depth.
- **Plus (`src/iknos/db/orm.py`):** declare every relational index in the ORM
  (`__table_args__ = (Index(...), ...)`). The `actions` indexes existed only in
  the migration, so autogenerate flagged them as drift forever until the ORM
  matched.

## Checklist for any new migration

- [ ] In `upgrade()`, if you touch AGE (`create_graph`/`create_vlabel`/etc.), set
      `search_path = ag_catalog, ...` **only around the graph calls**, then
      `SET search_path = public` before relational DDL.
- [ ] In `downgrade()`, `SET search_path = public` before relational drops; switch
      to `ag_catalog` only for `drop_graph`/graph teardown.
- [ ] Pass `table_name=` to `op.drop_index(...)` (not a bare positional).
- [ ] Every new relational table's indexes/constraints are declared in
      `src/iknos/db/orm.py` so the autogenerate drift check stays empty.
- [ ] Pin any new image to an explicit, verified tag — never `:latest`.
- [ ] Don't expect to test locally (no `docker compose up`). Push to a branch and
      let the `migrations` CI run the up/down/up + drift check. Watch it go green
      before merging.

## Where the fixes live

- `docker/postgres.Dockerfile` — pinned AGE base image.
- `alembic/versions/20260601_0001_initial_schema.py` — search_path reset pattern.
- `alembic/env.py` — connect-time `search_path=public` listener + `include_object`.
- `src/iknos/db/orm.py` — ORM index declarations.
- `.github/workflows/migrations.yml` — the four invariants above.
