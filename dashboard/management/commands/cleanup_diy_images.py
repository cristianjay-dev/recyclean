# recyclean/management/commands/cleanup_diy_images.py
from __future__ import annotations

import json
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from dashboard.utils.cleanup import cleanup_diy_images


class Command(BaseCommand):
    help = (
        "Cleanup DIYSubmission images older than N days (default 365). "
        "Keeps rows by default; optionally deletes rows with --delete-rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("--older-than", type=int, default=365,
                            help="Delete images older than this many days (default 365).")
        parser.add_argument("--batch-size", type=int, default=500,
                            help="Number of rows processed per DB chunk (default 500).")
        parser.add_argument("--limit", type=int, default=None,
                            help="Hard cap on number of rows to scan (optional).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be deleted without changing anything.")
        parser.add_argument(
            "--delete-rows",
            action="store_true",
            help="Dangerous: delete entire DIYSubmission rows (not just images).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip interactive confirmation when using --delete-rows (non-interactive environments).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output JSON result instead of human-readable text.",
        )

    def handle(self, *args, **opts):
        older_than = opts["older_than"]
        batch_size = opts["batch_size"]
        limit      = opts.get("limit")
        dry_run    = opts["dry_run"]
        delete_rows = opts["delete_rows"]
        auto_yes   = opts["yes"]
        as_json    = opts["json"]
        verbosity  = int(opts.get("verbosity", 1))

        # ---- Basic validation ----
        if older_than <= 0:
            raise CommandError("--older-than must be > 0")
        if batch_size <= 0:
            raise CommandError("--batch-size must be > 0")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be > 0 when provided")

        if verbosity >= 1 and not as_json:
            mode = "DRY-RUN" if dry_run else ("DELETE ROWS" if delete_rows else "DELETE IMAGES")
            self.stdout.write(
                f"[{timezone.now().isoformat()}] Starting DIY cleanup | "
                f"mode={mode}, older_than={older_than}d, batch_size={batch_size}, "
                f"limit={limit or '∞'}"
            )

        # ---- Safety confirmation for row deletion ----
        if delete_rows and not dry_run and not auto_yes and not as_json:
            self.stdout.write(self.style.WARNING(
                "You are about to DELETE ENTIRE DIYSubmission rows.\n"
                "This is destructive and cannot be undone."
            ))
            confirm = input("Type DELETE to continue: ").strip()
            if confirm != "DELETE":
                raise CommandError("Aborted. (Did not type DELETE)")

        # ---- Execute cleanup ----
        result = cleanup_diy_images(
            older_than_days=older_than,
            batch_size=batch_size,
            limit=limit,
            dry_run=dry_run,
            delete_rows=delete_rows,
        )

        # ---- Output ----
        if as_json:
            # Machine-readable summary
            self.stdout.write(json.dumps(result))
            return

        # Human-readable summary
        scanned = result.get("scanned", 0)
        imgs    = result.get("images_deleted", 0)
        rows    = result.get("rows_deleted", 0)
        skipped = result.get("skipped", 0)

        summary = (
            f"Done. Scanned={scanned}, "
            f"ImagesDeleted={imgs}, RowsDeleted={rows}, Skipped(no image)={skipped}"
        )
        if dry_run:
            summary = f"(DRY-RUN) {summary}"

        # Success style green if anything changed (or would change in dry-run)
        if (imgs + rows) > 0 or dry_run:
            self.stdout.write(self.style.SUCCESS(summary))
        else:
            self.stdout.write(summary)
