from django.urls import path

from . import views

urlpatterns = [
    path("live", views.live, name="health-live"),
    path("ready", views.ready, name="health-ready"),
    path("turnstile", views.turnstile, name="health-turnstile"),
    path("google-oauth", views.google_oauth, name="health-google-oauth"),
]
