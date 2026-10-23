"""Worker process: ``python -m cloudoptics.worker`` runs queued jobs."""

import asyncio

from cloudoptics.platform.jobs import get_job_app


async def main() -> None:
    app = get_job_app()
    async with app.open_async():
        await app.run_worker_async(queues=["connections", "ingestion", "fx", "inventory"])


if __name__ == "__main__":
    asyncio.run(main())
