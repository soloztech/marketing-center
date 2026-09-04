"""PostgreSQL serialization primitives shared by Marketing Center services."""

from psycopg2 import errorcodes, errors as pg_errors


class MarketingSerializationFailure(pg_errors.SerializationFailure):
    """Ask Odoo to retry an operation from a fresh database snapshot.

    Odoo 16 runs normal requests and jobs at PostgreSQL ``REPEATABLE READ``.
    Waiting for an advisory lock would retain the transaction's old snapshot,
    so the waiter could not safely observe the commit made by the lock owner.
    A generated psycopg2 exception instantiated in Python has no ``pgcode``;
    exposing SQLSTATE 40001 keeps it compatible with Odoo's retry machinery.
    """

    @property
    def pgcode(self):
        return errorcodes.SERIALIZATION_FAILURE


def acquire_advisory_xact_lock(cr, lock_key, message=None):
    """Acquire ``lock_key`` without waiting or require a fresh transaction.

    The caller must invoke this before reading the mutable state protected by
    the key.  A busy key is deliberately not waited on: the only safe retry at
    ``REPEATABLE READ`` is a new transaction with a new snapshot.
    """

    cr.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
        [lock_key],
    )
    if cr.fetchone()[0]:
        return
    raise MarketingSerializationFailure(
        message or "Concurrent Marketing Center operation requires a fresh snapshot"
    )
