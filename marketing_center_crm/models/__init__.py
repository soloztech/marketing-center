from . import crm_lead, crm_stage, event_policy, links, service

# Inherits the service declared above; registration order is significant.
from . import native_utm  # isort: skip  # noqa: E402
from . import campaign_board  # isort: skip  # noqa: E402
