from odoo.addons.meta_api_base.services.errors import MetaApiError

# This is the Marketing Center adapter contract, not the generic Meta App default.
# Catalog, Insights and Lead Ads must move together after fixtures for a new Graph
# version have passed. Keeping this separate from meta_api_base's baseline prevents
# an infrastructure default bump from silently changing provider semantics.
META_MARKETING_GRAPH_VERSION = "v26.0"

_CONSUMER_LABELS = {
    "catalog": "Catalog",
    "insights": "Insights",
    "lead_ads": "Lead Ads",
}


def require_marketing_graph_version(value, consumer):
    label = _CONSUMER_LABELS.get(consumer)
    if not label:
        raise MetaApiError("Meta Marketing Center consumer is invalid")
    actual = str(value or "").strip()
    if actual != META_MARKETING_GRAPH_VERSION:
        raise MetaApiError(
            "Meta %s supports Graph %s, but the shared Meta App is configured as "
            "%s. Upgrade every Marketing Center Meta adapter before changing the "
            "App version." % (label, META_MARKETING_GRAPH_VERSION, actual or "<empty>")
        )
    return actual
