"""EKIP REST API process entrypoint. Run as its own process:

    python scripts/run_api_server.py

Unlike `scripts/run_mcp_server.py`, no dependency-inversion trick is needed
here: app/api has no import-linter restriction against importing
app.database (see app/api/__init__.py's module docstring), so
`app.api.main:app` already wires up `get_db_session` directly and can be
handed straight to Uvicorn.
"""

from __future__ import annotations

import uvicorn

from app.shared.config.logging import configure_logging

configure_logging()

if __name__ == "__main__":
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=False)
