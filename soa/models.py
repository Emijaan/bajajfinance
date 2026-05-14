"""Persistent batch report jobs (SQLite)."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from django.db import models


class BatchReportJob(models.Model):
    """Background SOA batch payment report."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    date_from = models.DateField()
    date_to = models.DateField()
    total_loans = models.PositiveIntegerField(default=0)
    processed_loans = models.PositiveIntegerField(default=0)
    payment_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    access_token = models.CharField(max_length=64, editable=False, default="")
    input_file = models.FileField(upload_to="batch_jobs/input/")
    result_file = models.FileField(
        upload_to="batch_jobs/output/",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ("-created_at",)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.access_token:
            self.access_token = secrets.token_urlsafe(32)[:64]
        super().save(*args, **kwargs)
        return f"BatchReportJob({self.id}, {self.status})"
