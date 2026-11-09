FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

CMD ["uvicorn", "controller.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
