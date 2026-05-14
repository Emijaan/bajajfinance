"""Remove old batch report jobs and files."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from soa.models import BatchReportJob


class Command(BaseCommand):
    help = "Delete batch report jobs older than SOA_BATCH_JOB_RETENTION_DAYS (default 7)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override SOA_BATCH_JOB_RETENTION_DAYS from settings.",
        )

    def handle(self, *args, **options) -> None:
        days = options["days"]
        if days is None:
            days = int(getattr(settings, "SOA_BATCH_JOB_RETENTION_DAYS", 7))
        cutoff = timezone.now() - timedelta(days=days)
        qs = BatchReportJob.objects.filter(created_at__lt=cutoff)
        count = 0
        for job in list(qs):
            try:
                if job.input_file:
                    job.input_file.delete(save=False)
            except OSError:
                pass
            try:
                if job.result_file:
                    job.result_file.delete(save=False)
            except OSError:
                pass
            job.delete()
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} batch job(s) older than {days} days."))
