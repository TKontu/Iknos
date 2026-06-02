# Apache AGE base image + pgvector. Both extensions are loaded in 0001_initial.
#
# AGE has no pgvector-bundled image. If the tag below stops pulling, alternatives:
#   - apache/age:release_PG16_1.5.0  (pinned version)
#   - build AGE from source on pgvector/pgvector:pg16
FROM apache/age:PG16_latest

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-pgvector \
 && rm -rf /var/lib/apt/lists/*
USER postgres
