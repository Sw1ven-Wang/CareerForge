from celery import Celery  # type: ignore[import-untyped]

from careerforge.config.settings import get_settings

app = Celery("careerforge", broker=get_settings().redis_url.get_secret_value())
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_routes={"careerforge.execute_run": {"queue": "careerforge"}},
    imports=["careerforge.infrastructure.queue.tasks"],
)
