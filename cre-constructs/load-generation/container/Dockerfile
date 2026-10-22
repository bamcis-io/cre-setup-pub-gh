# Generic Locust base container for the LoadGenerator construct.
#
# NO `--platform` here on purpose: the platform is set by the DockerImageAsset
# (kaniko `--custom-platform` on both providers), derived from the construct's single
# `architecture` prop. Pinning it in
# the Dockerfile would let the image and the Fargate runtimePlatform drift.
#
# Not pinned to a digest: pin it (python:3.12-slim@sha256:...) in a demo that needs
# byte-reproducible images; the template can't pick one that stays current.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy the whole build context so a demo overlay (extra .py modules + its own
# locustfile.py) rides along unchanged. .dockerignore keeps local
# tooling state (.venv, .git, __pycache__) out.
COPY . .
EXPOSE 8089
RUN chmod +x /app/entrypoint.sh \
 && useradd --system --uid 10001 --no-create-home locust
# Locust needs no root: it binds 8089 (> 1024) and writes nothing under /app.
USER 10001
ENTRYPOINT ["/app/entrypoint.sh"]
