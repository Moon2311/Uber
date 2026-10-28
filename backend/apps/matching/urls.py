from django.urls import path

from .views import AcceptOfferView, DeclineOfferView, MyOffersView

urlpatterns = [
    path("offers/", MyOffersView.as_view(), name="offers-mine"),
    path("offers/<uuid:offer_id>/accept/", AcceptOfferView.as_view(), name="offer-accept"),
    path("offers/<uuid:offer_id>/decline/", DeclineOfferView.as_view(), name="offer-decline"),
]
