class _GoogleApiRuntimeCapability:
    """Process-local authority that cannot cross an RPC boundary."""

    __slots__ = ()

    def __repr__(self):
        return "<Google API runtime capability>"

    def __reduce__(self):
        raise TypeError("Google API runtime capability is not serializable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("Google API runtime capability is not serializable")


GOOGLE_API_RUNTIME_CONTEXT_KEY = "google_api_runtime"
GOOGLE_API_RUNTIME_TOKEN = _GoogleApiRuntimeCapability()
