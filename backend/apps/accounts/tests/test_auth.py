import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from conftest import PASSWORD, client_for, make_rider

pytestmark = pytest.mark.django_db

VEHICLE = {
    "make": "Honda",
    "model": "Civic",
    "color": "Blue",
    "plate_number": "xyz-123",
    "year": 2021,
    "category": "ECONOMY",
}


def register(api_client, **overrides):
    payload = {
        "email": "new@example.com",
        "password": PASSWORD,
        "first_name": "New",
        "role": "RIDER",
        **overrides,
    }
    return api_client.post(reverse("v1:auth-register"), payload, format="json")


def test_register_rider_returns_tokens_and_hashes_password(api_client):
    response = register(api_client)
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["role"] == "RIDER" and body["access"] and body["refresh"]
    user = User.objects.get(email="new@example.com")
    assert user.password != PASSWORD and user.check_password(PASSWORD)


def test_register_driver_creates_profile_and_vehicle(api_client):
    response = register(api_client, role="DRIVER", license_number="L-1", vehicle=VEHICLE)
    assert response.status_code == 201
    user = User.objects.get(email="new@example.com")
    assert user.driver_profile.vehicles.get().plate_number == "XYZ-123"


def test_register_driver_requires_vehicle_and_license(api_client):
    response = register(api_client, role="DRIVER")
    assert response.status_code == 400
    assert set(response.json()["error"]["details"]) >= {"license_number", "vehicle"}


def test_register_rejects_duplicate_email_case_insensitive(api_client):
    make_rider(email="dup@example.com")
    response = register(api_client, email="DUP@example.com")
    assert response.status_code == 400
    assert "email" in response.json()["error"]["details"]


def test_register_rejects_weak_password(api_client):
    response = register(api_client, password="password")
    assert response.status_code == 400
    assert "password" in response.json()["error"]["details"]


def test_register_rejects_unknown_role(api_client):
    assert register(api_client, role="ADMIN").status_code == 400


def test_login_success_and_me(api_client):
    make_rider(email="me@example.com")
    response = api_client.post(
        reverse("v1:auth-login"), {"email": "ME@example.com", "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    me = api_client.get(reverse("v1:auth-me"))
    assert me.status_code == 200 and me.json()["email"] == "me@example.com"


def test_login_wrong_password(api_client):
    make_rider(email="me@example.com")
    response = api_client.post(
        reverse("v1:auth-login"), {"email": "me@example.com", "password": "nope"}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_me_requires_authentication(api_client):
    assert api_client.get(reverse("v1:auth-me")).status_code == 401


def test_invalid_token_rejected(api_client):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
    assert api_client.get(reverse("v1:auth-me")).status_code == 401


def test_role_comes_from_database_not_request(rider):
    # A rider cannot act as a driver by claiming a role in the request.
    client = client_for(rider)
    response = client.post(
        reverse("v1:driver-availability"), {"is_online": True, "role": Role.DRIVER}, format="json"
    )
    assert response.status_code == 403


def test_auth_endpoints_are_rate_limited(api_client, monkeypatch):
    from rest_framework.throttling import SimpleRateThrottle

    rates = {**SimpleRateThrottle.THROTTLE_RATES, "auth": "2/min"}
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates)
    url = reverse("v1:auth-login")
    codes = [
        api_client.post(url, {"email": "a@b.co", "password": "x"}, format="json").status_code
        for _ in range(3)
    ]
    assert codes == [400, 400, 429]
