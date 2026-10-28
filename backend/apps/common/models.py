from django.db import models


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class IdempotencyRecord(models.Model):
    """Stored response for a client-supplied Idempotency-Key (per user + scope)."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE)
    scope = models.CharField(max_length=50)
    key = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "scope", "key"], name="idempotency_once")
        ]

    def __str__(self):
        return f"{self.scope}:{self.key}"
