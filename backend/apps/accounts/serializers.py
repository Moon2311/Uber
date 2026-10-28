from django.contrib.auth import authenticate, password_validation
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from apps.drivers.models import RideCategory

from .models import Role, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "phone_number", "role", "date_joined"]
        read_only_fields = fields


class VehicleInputSerializer(serializers.Serializer):
    make = serializers.CharField(max_length=50)
    model = serializers.CharField(max_length=50)
    color = serializers.CharField(max_length=30)
    plate_number = serializers.CharField(max_length=20)
    year = serializers.IntegerField(min_value=1990, max_value=2100)
    category = serializers.ChoiceField(choices=RideCategory.choices)


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone_number = serializers.RegexField(
        r"^\+?[0-9]{7,15}$", required=False, allow_blank=True, max_length=20
    )
    role = serializers.ChoiceField(choices=Role.choices)
    license_number = serializers.CharField(max_length=50, required=False)
    vehicle = VehicleInputSerializer(required=False)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, attrs):
        user = User(
            email=attrs["email"],
            first_name=attrs["first_name"],
            last_name=attrs.get("last_name", ""),
        )
        try:
            password_validation.validate_password(attrs["password"], user)
        except Exception as exc:  # django ValidationError -> DRF error
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        if attrs["role"] == Role.DRIVER:
            errors = {}
            if not attrs.get("license_number"):
                errors["license_number"] = ["This field is required for drivers."]
            if not attrs.get("vehicle"):
                errors["vehicle"] = ["This field is required for drivers."]
            if errors:
                raise serializers.ValidationError(errors)
        else:
            # Riders cannot smuggle driver data in.
            attrs.pop("license_number", None)
            attrs.pop("vehicle", None)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        from apps.drivers.services import create_driver_profile

        license_number = validated_data.pop("license_number", None)
        vehicle = validated_data.pop("vehicle", None)
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        if user.role == Role.DRIVER:
            create_driver_profile(user=user, license_number=license_number, vehicle=vehicle)
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            email=attrs["email"].strip().lower(),
            password=attrs["password"],
        )
        if user is None:
            # Same message for unknown email / wrong password / inactive user.
            raise serializers.ValidationError(
                {"non_field_errors": ["Invalid email or password."]}, code="authentication_failed"
            )
        attrs["user"] = user
        return attrs


def tokens_for(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class AuthResponseSerializer(serializers.Serializer):
    user = UserSerializer()
    access = serializers.CharField()
    refresh = serializers.CharField()
