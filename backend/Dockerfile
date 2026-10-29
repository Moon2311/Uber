# syntax=docker/dockerfile:1.7
# One image, many roles (see docker-entrypoint.sh): web | asgi | migrate | worker commands.

FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home \
    --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root . .
# Hashed admin static files baked into the image (served by WhiteNoise). Throwaway
# values: nothing connects anywhere at build time.
RUN APP_ENV=production DJANGO_SECRET_KEY=collectstatic-only DATABASE_URL=postgres://x@x/x \
    python manage.py collectstatic --noinput -v0 \
    && chmod 0755 docker-entrypoint.sh
ARG APP_RELEASE=dev
ENV APP_RELEASE=${APP_RELEASE}
LABEL org.opencontainers.image.revision=${APP_RELEASE}
# Code is root-owned and read-only for the app user; only /tmp is writable
# (an emptyDir / tmpfs in Kubernetes with readOnlyRootFilesystem).
USER 10001:10001
EXPOSE 8000 8001 9100
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["web"]
