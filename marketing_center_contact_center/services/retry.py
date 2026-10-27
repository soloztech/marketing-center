"""Bound retries to failures for which a fresh transaction can make progress."""

from functools import wraps

from psycopg2 import OperationalError, errors

from odoo import _

from odoo.addons.queue_job.exception import RetryableJobError

MAX_BRIDGE_RETRIES = 8
_TRANSIENT_SQLSTATES = frozenset(
    {"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "53300"}
)
_TRANSIENT_ERRORS = (
    errors.SerializationFailure,
    errors.DeadlockDetected,
    errors.LockNotAvailable,
    errors.AdminShutdown,
    errors.CrashShutdown,
    errors.CannotConnectNow,
    errors.TooManyConnections,
    errors.ConnectionException,
)


def retry_database_error(error, message):
    """Keep the queue's native failed state for permanent/unclassified failures.

    Typed checks also cover tests and failures raised before psycopg populates
    pgcode. Never classify errors by their message, which can contain SQL/PII.
    """
    code = error.pgcode or ""
    if (
        isinstance(error, _TRANSIENT_ERRORS)
        or code in _TRANSIENT_SQLSTATES
        or code.startswith("08")
    ):
        raise RetryableJobError(message) from None
    raise error


def retry_transient_database(method):
    """Apply classification inside Job.perform, where its retry budget is enforced.

    The queue HTTP controller also retries some SQL failures, but that fallback
    happens outside Job.perform and cannot enforce max_retries on its own.
    """

    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OperationalError as error:
            retry_database_error(
                error,
                _("Transient database failure while processing the marketing bridge."),
            )

    return wrapped
