"""Start the backend: `backend\\.venv\\Scripts\\python.exe -m app`."""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.main import create_app


def main() -> None:
    settings = get_settings()
    # log_config=None keeps the redacting JSON handler installed by create_app.
    # access_log=False: uvicorn's access line includes the query string; the app's own
    # request log records the path only.
    uvicorn.run(
        create_app(settings),
        host=settings.devpilot_host,
        port=settings.devpilot_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
