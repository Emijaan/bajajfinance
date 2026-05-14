from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/soa", views.api_soa, name="api_soa"),
    path("healthz", views.healthz, name="healthz"),
]
