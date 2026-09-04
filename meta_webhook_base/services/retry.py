def bounded_retry_seconds(value):
    """Return a queue-safe positive retry delay or the queue pattern sentinel."""

    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return min(value, 86_400)
