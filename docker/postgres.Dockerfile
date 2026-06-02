# Apache AGE base image + pgvector. Both extensions are loaded in 0001_initial.
#
# AGE has no pgvector-bundled image. Pinned to an explicit release tag — the
# rolling `PG16_latest` tag was removed from Docker Hub and no longer resolves.
# If this tag stops pulling, alternatives:
#   - apache/age:release_PG16_1.5.0  (older pinned version)
#   - build AGE from source on pgvector/pgvector:pg16
FROM apache/age:release_PG16_1.6.0

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-pgvector \
 && rm -rf /var/lib/apt/lists/*
USER postgres
