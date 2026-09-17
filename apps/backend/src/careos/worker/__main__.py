from __future__ import annotations

import asyncio
import contextlib

from careos.core.config import get_settings
from careos.worker.runner import run_worker


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker(get_settings()))


if __name__ == "__main__":
    main()
