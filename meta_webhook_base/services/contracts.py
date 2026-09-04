import re
from datetime import timedelta

# Public health contract for every addon consuming the shared Meta webhook
# projection.  Reconciliation runs well before this boundary, leaving time for
# queue latency and bounded transient retries without presenting stale state as
# healthy.
META_WEBHOOK_FRESHNESS = timedelta(minutes=30)
META_WEBHOOK_REFRESH_AFTER = timedelta(minutes=10)
META_WEBHOOK_ROUTING_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
