"""`aieos-api`: run the API with uvicorn using settings from the environment."""

from __future__ import annotations

import uvicorn

from aieos.api.app import create_app
from aieos.core.settings import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=True,
        server_header=False,
        access_log=False,  # the app logs requests itself, with redaction
        log_config=None,  # no plain-text uvicorn handlers; warnings and errors still reach stderr
    )


if __name__ == "__main__":
    main()
