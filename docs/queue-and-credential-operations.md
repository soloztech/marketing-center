# Queue and credential operations

This is the runtime contract for the Marketing Center suite. It is not a claim
about the current production configuration. Record the deployed source/peer SHAs,
runner configuration, Odoo HTTP worker count and database connection budget before
changing capacity.

## Capacity and admission

`queue.job.channel` records name channels. Capacity belongs to the OCA runner's
configuration (`ODOO_QUEUE_JOB_CHANNELS`), not an XML `capacity` field. A job must
fit every ancestor: with `root:1`, all children together run at most one job.

The Marketing Center channels are:

| Channel | Work |
|---|---|
| `root.meta_webhook` | Shared Meta webhook dispatch/reconciliation |
| `root.marketing_meta` | Meta validation, catalog and Insights |
| `root.marketing_google` | Google validation, catalog and metrics |
| `root.marketing_meta_crm` | Lead Ads CRM projection |
| `root.marketing_contact_center` | Attribution, lifecycle and response episodes |
| `root.marketing_contact_center_crm` | Conversation-to-CRM convergence |
| `root.marketing_website_crm` | Website CRM evidence recovery |

Include the Contact Center's own channels in the same inventory. Limiting only
Marketing Center's children does not reserve capacity for customer messages.
Priorities order waiting jobs; they do not preempt a running HTTP request. Channels
are not dedicated worker pools.

Size root and child limits against measured HTTP and PostgreSQL headroom. Reserve
HTTP capacity for interactive traffic; increasing the runner's root limit without
available workers does not add throughput. Keep long historical imports paginated
and at lower priority than live customer work. The separate lead-history feature
must retain its own bounded pages, cursors and retry contract.

Before applying a candidate configuration, replay a representative mixed workload
in homologation: message ingress/send, slow provider response, API rate limiting,
catalog sync and historical pages. Record queue age p50/p95/max, oldest pending job,
job duration, failed/retried counts, HTTP latency and active DB connections. Choose
explicit latency/error budgets and abort criteria with the operator; do not infer a
new slot count from the old `root:1` incident alone.

Rollback is restoration of the recorded runner configuration and the separately
authorized service restart. Retry/requeue of jobs is a separate action, after
checking idempotency and any external side effect.

## Retry exhaustion and recovery

Marketing Contact Center and CRM bridge jobs now have eight attempts. Recognized
serialization/deadlock/lock contention, connection SQLSTATEs and temporary server
unavailability can retry. Unclassified database operational errors fail visibly
instead of looping forever. Known revision-uniqueness races retain their existing
special handling. The OCA `failed` state is the terminal queue; no second dead-letter
model is needed.

Monitor oldest pending/enqueued jobs, retry count and failed jobs by channel/company.
Alert on exhausted retries and on breached latency budgets even if jobs eventually
succeed. This contract requires an operational alert consumer; merely defining a
channel does not install one.

For recovery: identify exact failed jobs and input/source revisions; correct the
underlying problem; verify the replay's idempotency; requeue only those jobs; check
the business projection and the queue state. Do not bulk reset all failed jobs.
Jobs persisted before this code change may still carry `max_retries=0`; inventory
and update/requeue them deliberately through ORM, with evidence and rollback. A
code update changes new enqueue calls, not old database rows automatically.

The shared Meta webhook now also gives newly queued fanout/consumer jobs eight
attempts, alongside its existing application-level budget. Database failures are
classified inside the job so the queue budget covers lock acquisition and
processing. A terminal queue failure does not falsely mark a rolled-back delivery
as successfully processed. Its persisted job pointer prevents the orphan cron
from silently creating another retry budget; recovery requires explicit requeue.
Missing/cancelled jobs remain recoverable. The new lead-history feature is owned
separately and is not rewritten by this bridge change.

## Rotating Meta/Google credentials

1. Inventory the exact app/profile/connection, company, current reference/revision,
   pending jobs and documented scopes. Do not record secret values.
2. Provision the replacement secret in a new externally managed file/reference,
   with the existing ownership, size and `0600`/read-only mount protections.
3. Validate the replacement against required scopes and account identity in the
   authorized environment. Record only sanitized status, scope names and expiry.
4. Switch the configuration to the new reference so revision fences invalidate old
   queued work. Do not silently overwrite bytes behind an unchanged reference.
5. Validate again through the normal profile job, then check a bounded read/sync and
   expected health. Reschedule stale work using the new revision.
6. Revoke the old provider credential only after the new one is verified and the
   agreed rollback window closes. Provider revocation may be irreversible.

Before provider revocation, rollback can restore the previous reference through
the supported configuration flow and validate it again. After revocation, restoring
a filename does not restore the credential. Never enable `requests`/`urllib3` DEBUG
logging during diagnosis: URLs such as `debug_token` may contain credential material.

## Public ingress edge

The database admission quota is a final per-endpoint/request-class safeguard. It
does not provide per-client fairness. Configure and test rate limiting at the actual
reverse proxy (the documented Soloz edge is Traefik), using a trusted forwarded-IP
chain. Strip untrusted forwarding headers before applying identity-based limits.

Test legitimate NAT users and bursts, abuse by one client, payload limits and Meta
redelivery behavior. Unknown routing keys should fail before body/HMAC work; valid
public routing capabilities still need admission control. Do not use absence of a
Nginx `limit_req` directive as evidence that a Traefik router is unprotected.
