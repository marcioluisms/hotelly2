# Hotelly V2

> **uv** escolhido por ser 10-100x mais rápido que poetry e ter lockfile nativo.

## Setup

```bash
# Criar venv e instalar dependências
uv sync --all-extras

# Rodar app local
uv run uvicorn hotelly.api.app:app --reload

# Rodar testes
uv run pytest -q
```
