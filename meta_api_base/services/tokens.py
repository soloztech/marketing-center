class _MetaApiRuntimeCapability:
    """Process-local authority that cannot cross an RPC serialization boundary."""

    __slots__ = ()

    def __repr__(self):
        return "<Meta API runtime capability>"

    def __reduce__(self):
        raise TypeError("Meta API runtime capability is not serializable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("Meta API runtime capability is not serializable")


META_API_RUNTIME_CONTEXT_KEY = "meta_api_runtime"
META_API_RUNTIME_TOKEN = _MetaApiRuntimeCapability()
