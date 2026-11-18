from contextlib import asynccontextmanager

from ..errors import DomainError, GraphCheckpointUnavailable


class Checkpointers:
    def __init__(self, settings):
        self.settings = settings
        self._memory = None

    @asynccontextmanager
    async def open(self):
        if self.settings.mode == "test":
            from langgraph.checkpoint.memory import InMemorySaver

            if self._memory is None:
                self._memory = InMemorySaver()
            yield self._memory
            return
        if not self.settings.graph_checkpoint_url.startswith(("postgresql://", "postgres://")):
            raise DomainError(
                "GRAPH_CHECKPOINTER", "缺少 PostgreSQL Checkpointer，禁止内存回退。", 503
            )
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import InterfaceError, OperationalError

        try:
            async with AsyncPostgresSaver.from_conn_string(
                self.settings.graph_checkpoint_url
            ) as saver:
                # Setup is an explicit deployment operation, never a worker startup mutation.
                yield saver
        except (OperationalError, InterfaceError) as exc:
            raise GraphCheckpointUnavailable("Checkpoint transport unavailable") from exc

    async def health(self):
        try:
            async with self.open() as saver:
                await saver.aget_tuple({"configurable": {"thread_id": "__health__"}})
        except Exception:
            # Never expose DSNs, database errors or private checkpoint data.
            return {"status": "unavailable"}
        return {"status": "ready", "persistent": self.settings.mode != "test"}
