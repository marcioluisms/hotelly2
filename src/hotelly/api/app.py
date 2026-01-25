"""FastAPI application."""

from fastapi import FastAPI

app = FastAPI(title="Hotelly V2", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
