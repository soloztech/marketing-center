class _MetaWebhookCapability:
    """Process-local authority that must never cross RPC or queue boundaries."""

    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return "<Meta webhook %s capability>" % self._name

    def __reduce__(self):
        raise TypeError("Meta webhook capability is not serializable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("Meta webhook capability is not serializable")


META_WEBHOOK_INTERNAL_TOKEN = _MetaWebhookCapability("internal")
META_WEBHOOK_RUNTIME_TOKEN = _MetaWebhookCapability("runtime")
