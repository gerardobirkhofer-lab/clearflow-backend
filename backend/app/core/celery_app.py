from celery import Celery

celery_app = Celery(
    "clearflow",
    broker="redis://redis:6379/1",
    backend="redis://redis:6379/2",
    include=[],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Madrid",
    enable_utc=True,
)
