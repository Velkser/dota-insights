FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app
WORKDIR /app

# Install dependencies first so they cache independently of source changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev && chown -R app:app /app

USER app
ENV PATH="/app/.venv/bin:$PATH" \
    DOTA_INSIGHTS_HOST=0.0.0.0 \
    DOTA_INSIGHTS_PORT=8000

EXPOSE 8000
CMD ["dota-insights"]
