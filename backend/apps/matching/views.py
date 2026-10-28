from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsDriver

from .models import OfferStatus, RideOffer
from .offers import accept_offer, decline_offer
from .serializers import RideOfferSerializer
from .state import get_reservation_locks


class MyOffersView(generics.ListAPIView):
    """The authenticated driver's live (pending, unexpired) offers."""

    permission_classes = [IsDriver]
    serializer_class = RideOfferSerializer
    pagination_class = None

    def get_queryset(self):
        return RideOffer.objects.filter(
            driver=self.request.user.driver_profile,
            status=OfferStatus.PENDING,
            expires_at__gt=timezone.now(),
        ).select_related("trip")


class _OfferActionView(APIView):
    permission_classes = [IsDriver]
    # Not called `action`: DRF/drf-spectacular reserve view.action for viewset actions.
    respond = None

    @extend_schema(request=None, responses=RideOfferSerializer)
    def post(self, request, offer_id):
        try:
            result = type(self).respond(
                offer_id=offer_id,
                driver=request.user.driver_profile,
                locks=get_reservation_locks(),
            )
        except RideOffer.DoesNotExist as exc:
            raise Http404 from exc  # other drivers' offers are invisible
        return Response(RideOfferSerializer(result.offer).data)


class AcceptOfferView(_OfferActionView):
    """Idempotent: repeating an accept returns the accepted offer."""

    respond = staticmethod(accept_offer)


class DeclineOfferView(_OfferActionView):
    respond = staticmethod(decline_offer)
