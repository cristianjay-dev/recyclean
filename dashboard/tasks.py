# dashboard/tasks.py
from __future__ import annotations
import logging
from celery import shared_task
from django.conf import settings

from .utils.cleanup import cleanup_diy_images

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def cleanup_diy_images_task(self):
    """
    Deletes old DIYSubmission image files while keeping rows.
    Runs with a 365-day retention by default; adjust via env if needed.
    """
    older_than_days = int(getattr(settings, "DIY_IMAGE_RETENTION_DAYS", 365))
    batch_size = int(getattr(settings, "DIY_IMAGE_CLEANUP_BATCH", 500))

    # No limit in scheduled run
    try:
        res = cleanup_diy_images(
            older_than_days=older_than_days,
            batch_size=batch_size,
            limit=None,
            dry_run=False,
            delete_rows=False,
        )
        logger.info(
            "[DIY cleanup] older_than=%sd -> scanned=%s, images_deleted=%s, rows_deleted=%s, skipped=%s",
            older_than_days, res["scanned"], res["images_deleted"], res["rows_deleted"], res["skipped"]
        )
        return res
    except Exception as e:
        logger.exception("DIY cleanup failed")
        raise self.retry(exc=e)
