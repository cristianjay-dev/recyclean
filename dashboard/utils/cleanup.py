# dashboard/utils/cleanup.py
from __future__ import annotations
from datetime import timedelta
from typing import Optional, Dict
from django.utils import timezone
from django.db import transaction
from django.core.files.storage import default_storage

from ..models import DIYSubmission

def cleanup_diy_images(
    older_than_days: int = 365,
    batch_size: int = 500,
    limit: Optional[int] = None,
    dry_run: bool = False,
    delete_rows: bool = False,
) -> Dict[str, int]:
    """
    Remove images from DIYSubmission older than the cutoff.
    Default: keep row, delete file (image -> None).
    When delete_rows=True, delete the entire row (dangerous).
    """
    cutoff = timezone.now() - timedelta(days=max(1, int(older_than_days)))

    qs = (
        DIYSubmission.objects
        .filter(created_at__lt=cutoff)
        .only("id", "image", "created_at")
        .order_by("id")                      # deterministic iteration
    )

    scanned = images_deleted = rows_deleted = skipped = 0
    seen = 0
    for sub in qs.iterator(chunk_size=batch_size):
        if limit is not None and seen >= int(limit):
            break
        seen += 1
        scanned += 1

        img = getattr(sub, "image", None)
        if not img:
            skipped += 1
            continue

        if dry_run:
            images_deleted += 1 if not delete_rows else 0
            rows_deleted    += 1 if delete_rows else 0
            continue

        with transaction.atomic():
            if delete_rows:
                # delete the file explicitly before row removal
                try:
                    if img.name and default_storage.exists(img.name):
                        default_storage.delete(img.name)
                except Exception:
                    pass
                sub.delete()
                rows_deleted += 1
            else:
                try:
                    sub.image.delete(save=False)  # removes file from storage
                except Exception:
                    try:
                        if img.name and default_storage.exists(img.name):
                            default_storage.delete(img.name)
                    except Exception:
                        pass
                sub.image = None
                sub.save(update_fields=["image"])
                images_deleted += 1

    return {
        "scanned": scanned,
        "images_deleted": images_deleted,
        "rows_deleted": rows_deleted,
        "skipped": skipped,
    }
