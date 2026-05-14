from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/soa", views.api_soa, name="api_soa"),
    path(
        "reports/payments/job/<uuid:job_id>/status",
        views.batch_job_status_json,
        name="batch_job_status_json",
    ),
    path(
        "reports/payments/job/<uuid:job_id>/download",
        views.batch_job_download,
        name="batch_job_download",
    ),
    path(
        "reports/payments/job/<uuid:job_id>/",
        views.batch_job_status,
        name="batch_job_status",
    ),
    path("reports/payments", views.payment_report, name="payment_report"),
    path("healthz", views.healthz, name="healthz"),
]
