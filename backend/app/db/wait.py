"""Wait until the database accepts connections (container startup), with exponential backoff.

    python -m app.db.wait --timeout 90

Exit code 0 when reachable, 1 after the timeout (the container then exits and its restart policy retries).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def wait(timeout: float) -> bool:
    url = get_settings().database_url
    deadline = time.monotonic() + timeout
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print(f"database reachable (attempt {attempt})", flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            left = deadline - time.monotonic()
            if left <= 0:
                print(f"database not reachable after {timeout:.0f}s: {type(e).__name__}", file=sys.stderr, flush=True)
                return False
            print(f"waiting for database ({type(e).__name__}); retry in {delay:.1f}s", flush=True)
            await asyncio.sleep(min(delay, left))
            delay = min(delay * 2, 5)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=90)
    sys.exit(0 if asyncio.run(wait(ap.parse_args().timeout)) else 1)
