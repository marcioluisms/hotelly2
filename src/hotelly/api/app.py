"""FastAPI application entry point.

Usage:
    uvicorn hotelly.api.app:app  # Uses APP_ROLE env var (default: public)
"""

from .factory import create_app

# Default app instance for uvicorn compatibility
app = create_app()
