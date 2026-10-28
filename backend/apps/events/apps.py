from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    label = "events"

    def ready(self):
        # Connect domain signals -> outbox / location stream. Business apps never
        # import this app; they only send their own signals.
        from . import receivers  # noqa: F401
