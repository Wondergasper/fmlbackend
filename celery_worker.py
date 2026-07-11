"""
celery_worker.py — Celery App and Task Queue Worker Setup for Farm-Connect
"""

from celery_app import celery_app


if __name__ == "__main__":
    celery_app.start()
