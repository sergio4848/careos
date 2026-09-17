# syntax=docker/dockerfile:1.7
# CareOS backend image: one image, two entrypoints (API and worker).
#
#   docker build -t careos-backend .                      # includes dev/test tooling
#   docker build --build-arg INSTALL_DEV=false -t careos-backend:prod .

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build
ARG INSTALL_DEV=true
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency layer (cached until pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN python - <<'PY'
import os, tomllib
project = tomllib.load(open("pyproject.toml", "rb"))["project"]
deps = list(project["dependencies"])
if os.environ.get("INSTALL_DEV", "true") == "true":
    deps += project["optional-dependencies"]["dev"]
open("/tmp/requirements.txt", "w").write("\n".join(deps) + "\n")
PY
RUN pip install -r /tmp/requirements.txt

COPY src ./src
RUN pip install --no-deps .

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
RUN groupadd --system --gid 10001 careos \
    && useradd --system --uid 10001 --gid careos --home-dir /app --shell /usr/sbin/nologin careos
WORKDIR /app
RUN chown careos:careos /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=careos:careos alembic.ini pyproject.toml ./
COPY --chown=careos:careos migrations ./migrations
COPY --chown=careos:careos tests ./tests
USER careos
EXPOSE 8000
CMD ["uvicorn", "careos.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
