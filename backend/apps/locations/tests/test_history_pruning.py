from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.locations.models import DriverLocationEvent
from conftest import make_driver

pytestmark = pytest.mark.django_db


def test_prune_location_history_deletes_only_expired_rows():
    d = make_driver()
    now = timezone.now()
    for days in (40, 31, 5):
        DriverLocationEvent.objects.create(
            driver=d, latitude=1, longitude=1, recorded_at=now - timedelta(days=days)
        )
    out = StringIO()
    call_command("prune_location_history", "--days", "30", "--batch-size", "1", stdout=out)
    assert "Deleted 2" in out.getvalue()
    assert DriverLocationEvent.objects.count() == 1
