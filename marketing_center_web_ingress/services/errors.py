from psycopg2 import errorcodes, errors as pg_errors


class WebIngressSerializationFailure(pg_errors.SerializationFailure):
    """Synthetic concurrency signal compatible with Odoo's request retry loop.

    Instantiating psycopg2's generated ``SerializationFailure`` directly leaves
    ``pgcode`` empty, so ``odoo.service.model.retrying`` does not recognize it
    as retryable. This subclass preserves the OperationalError hierarchy while
    exposing the PostgreSQL serialization SQLSTATE expected by Odoo.
    """

    @property
    def pgcode(self):
        return errorcodes.SERIALIZATION_FAILURE
