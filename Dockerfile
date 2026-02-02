# Dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock README.md alembic.ini ./
RUN uv sync --frozen --no-dev

COPY src/ src/
COPY migrations/ migrations/

ENV APP_HOST=0.0.0.0 APP_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "hotelly.api.factory:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
