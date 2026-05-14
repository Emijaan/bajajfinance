"""Run ``BatchReportJob`` in a background thread (MVP single-process worker)."""

from __future__ import annotations

import gc
import logging
from uuid import UUID

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.utils import timezone

from .batch_report import build_payment_report
from .excel_io import read_loan_numbers_from_xlsx, write_payment_report_xlsx
from .models import BatchReportJob

logger = logging.getLogger(__name__)


def run_batch_report_job(job_id: UUID) -> None:
    close_old_connections()
    try:
        job = BatchReportJob.objects.get(pk=job_id)
    except BatchReportJob.DoesNotExist:
        logger.error("BatchReportJob not found: %s", job_id)
        return

    try:
        job = BatchReportJob.objects.get(pk=job_id)
        if job.status != BatchReportJob.Status.PENDING:
            logger.warning("Job %s not pending (status=%s); skip", job_id, job.status)
            return
        job.status = BatchReportJob.Status.RUNNING
        job.save(update_fields=["status", "updated_at"])

        raw = job.input_file.read()
        loans = read_loan_numbers_from_xlsx(raw)
        max_loans = int(getattr(settings, "SOA_BATCH_MAX_LOANS", 20000))
        total = min(len(loans), max_loans)
        BatchReportJob.objects.filter(pk=job_id).update(
            total_loans=total,
            updated_at=timezone.now(),
        )

        def on_progress(processed: int, total_cap: int, payment_count: int) -> None:
            BatchReportJob.objects.filter(pk=job_id).update(
                processed_loans=processed,
                payment_count=payment_count,
                updated_at=timezone.now(),
            )

        payments, statuses = build_payment_report(
            loans,
            job.date_from,
            job.date_to,
            on_progress=on_progress,
        )

        xlsx = write_payment_report_xlsx(payments, statuses)
        job.refresh_from_db()
        job.result_file.save(
            f"soa-payments-{job_id}.xlsx",
            ContentFile(xlsx),
            save=False,
        )
        job.status = BatchReportJob.Status.DONE
        job.processed_loans = total
        job.payment_count = len(payments)
        job.error_message = ""
        job.save(
            update_fields=[
                "result_file",
                "status",
                "processed_loans",
                "payment_count",
                "error_message",
                "updated_at",
            ]
        )
        gc.collect()
        logger.info(
            "Batch job %s done: %s loans, %s payment rows", job_id, total, len(payments)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Batch job %s failed", job_id)
        BatchReportJob.objects.filter(pk=job_id).update(
            status=BatchReportJob.Status.FAILED,
            error_message=str(exc)[:8000],
            updated_at=timezone.now(),
        )
    finally:
        close_old_connections()


def start_batch_report_job_thread(job_id: UUID) -> None:
    import threading

    t = threading.Thread(
        target=run_batch_report_job,
        args=(job_id,),
        daemon=True,
        name=f"batch-report-{job_id}",
    )
    t.start()
