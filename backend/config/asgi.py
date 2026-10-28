import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Django Channels will wrap this in a ProtocolTypeRouter in a later step.
application = get_asgi_application()
