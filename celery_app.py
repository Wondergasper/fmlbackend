"""
celery_app.py — Celery Application Configuration for Farm-Connect
"""

import os

try:
    from celery import Celery
except ImportError:
    class _DummyConf(dict):
        def update(self, *args, **kwargs):
            if args and isinstance(args[0], dict):
                super().update(args[0])
            super().update(kwargs)

    class Celery:
        def __init__(self, main=None, broker=None, backend=None, include=None, **kwargs):
            self.main = main
            self.broker = broker
            self.backend = backend
            self.include = include or []
            self.conf = _DummyConf()

        def task(self, *args, **kwargs):
            def decorator(func):
                def delay(*a, **kw):
                    return func(*a, **kw)
                def apply_async(args=None, kwargs=None, **kw):
                    a = args or ()
                    k = kwargs or {}
                    return func(*a, **k)
                func.delay = delay
                func.apply_async = apply_async
                func.run = func
                return func

            if len(args) == 1 and callable(args[0]):
                return decorator(args[0])
            return decorator

        def start(self, *args, **kwargs):
            pass


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)

celery_app = Celery(
    "farmconnect_tasks",
    broker=BROKER_URL,
    backend=REDIS_URL,
    include=["services.email"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "true").lower() == "true",
)
