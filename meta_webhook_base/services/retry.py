from functools import wraps

from psycopg2 import OperationalError, errors

from odoo.addons.queue_job.exception import RetryableJobError

DATABASE_RETRY_CEILING = 8
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


def retry_transient_database(method):
    """Classify inside Job.perform, where OCA enforces the queue retry budget.

    This covers lock/flush failures outside the application's processing
    savepoint. Database contention does not commit a provider failure or mark
    the delivery dead. Exhaustion remains visible on the existing queue job.
    """

    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OperationalError as error:
            code = error.pgcode or ""
            if not (
                isinstance(error, _TRANSIENT_ERRORS)
                or code in _TRANSIENT_SQLSTATES
                or code.startswith("08")
            ):
                raise
        # Drop database exception text and chains from retained retry details.
        raise RetryableJobError("Meta webhook hit a transient database failure")

    return wrapped


def bounded_retry_seconds(value):
    """Return a queue-safe positive retry delay or the queue pattern sentinel."""

    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return min(value, 86_400)
