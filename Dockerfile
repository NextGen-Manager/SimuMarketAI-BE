FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv==0.11.23

# The worker image is built with `--build-arg INSTALL_OASIS=1`. The API image is
# not: it never runs a simulation, and `camel-oasis` drags in a model stack whose
# pins conflict with the API's own toolchain.
ARG INSTALL_OASIS=0

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_OASIS" = "1" ]; then \
        uv sync --frozen --no-dev --extra oasis; \
    else \
        uv sync --frozen --no-dev; \
    fi

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN groupadd --system --gid 10001 simumarket \
    && useradd --system --uid 10001 --gid simumarket --create-home simumarket \
    && mkdir -p /var/lib/simumarket/oasis-traces \
    && chown -R simumarket:simumarket /var/lib/simumarket

USER simumarket

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
