"""Explicit deployment step. Uses CAREERFORGE_GRAPH_CHECKPOINT_URL only."""

import asyncio

from careerforge.config import Settings
from careerforge.workflows.checkpointer import Checkpointers


async def main():
    settings = Settings.from_env()
    settings.validate()
    if settings.mode == "test":
        raise RuntimeError("Provision persistent PostgreSQL, not a test saver")
    checkpoints = Checkpointers(settings)
    async with checkpoints.open() as saver:
        await saver.setup()
    print(await checkpoints.health())


if __name__ == "__main__":
    asyncio.run(main())
