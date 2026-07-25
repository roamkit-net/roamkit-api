"""Order URL configuration — mounted at ``/api/v1/orders/``."""

from django.urls import path

from apps.orders.views import OrderCreateView

urlpatterns = [
    path("", OrderCreateView.as_view(), name="order-create"),
]
