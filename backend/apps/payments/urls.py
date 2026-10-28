from django.urls import path

from .views import (
    PaymentMethodDetailView,
    PaymentMethodListView,
    RefundPaymentView,
    RetryPaymentView,
    TripPaymentView,
    WebhookView,
)

urlpatterns = [
    path("methods/", PaymentMethodListView.as_view(), name="payment-methods"),
    path("methods/<uuid:method_id>/", PaymentMethodDetailView.as_view(), name="payment-method"),
    path("trips/<uuid:trip_id>/", TripPaymentView.as_view(), name="trip-payment"),
    path("<uuid:payment_id>/retry/", RetryPaymentView.as_view(), name="payment-retry"),
    path("<uuid:payment_id>/refund/", RefundPaymentView.as_view(), name="payment-refund"),
    path("webhooks/<slug:provider>/", WebhookView.as_view(), name="payment-webhook"),
]
