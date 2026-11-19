"""Worker process: ``python -m cloudoptics.worker`` runs queued jobs."""

import asyncio

from cloudoptics.platform.jobs import get_job_app
from cloudoptics.platform.settings import get_settings


async def main() -> None:
    get_settings().require_aws_connection_settings()
    app = get_job_app()
    async with app.open_async():
        await app.run_worker_async(
            queues=[
                "connections",
                "ingestion",
                "fx",
                "inventory",
                "metrics",
                "analysis",
                "notifications",
                "privacy",
                "exports",
            ]
        )


if __name__ == "__main__":
    asyncio.run(main())
