"""Worker process: ``python -m cloudoptics.worker`` runs queued jobs."""

import asyncio

from cloudoptics.platform.jobs import deadline_middleware, get_job_app
from cloudoptics.platform.settings import get_settings


async def main() -> None:
    settings = get_settings()
    settings.require_aws_connection_settings()
    app = get_job_app()
    async with app.open_async():
        await app.run_worker_async(
            # Several jobs at once, so one tenant's long job does not hold every other
            # tenant's (platform/jobs.py has the deadlines and stalled-job recovery).
            concurrency=settings.worker_concurrency,
            worker_middleware=[deadline_middleware],
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
            ],
        )


if __name__ == "__main__":
    asyncio.run(main())
