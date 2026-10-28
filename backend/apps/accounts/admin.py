from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import BaseUserCreationForm

from .models import User


class UserCreationForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "role")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = user.email.lower()
        user.username = user.email
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            "email",
            "password",
            "role",
            "first_name",
            "last_name",
            "phone_number",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "last_login",
            "date_joined",
        )


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationForm
    form = UserChangeForm
    list_display = ("email", "first_name", "last_name", "role", "is_active", "date_joined")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name", "phone_number")
    ordering = ("-date_joined",)
    readonly_fields = ("last_login", "date_joined")
    fieldsets = (
        (None, {"fields": ("email", "password", "role")}),
        ("Personal info", {"fields": ("first_name", "last_name", "phone_number")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "role", "password1", "password2")}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Role is chosen at creation and immutable afterwards.
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "role") if obj else fields
