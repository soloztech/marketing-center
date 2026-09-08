import datetime
import hashlib
import uuid

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
)
from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO
from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.tokens import MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN

_CONFIRMED_DELIVERY_STATES = ("sent", "delivered", "read")
_POLICY_VERSION = 1
_RESPONSE_PAGE_SIZE = 200


def _internal(recordset):
    return (
        recordset.env.context.get("marketing_contact_center_episode_write_token")
        is MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
    )


def _sha256(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class ImmutableMarketingContactCenterEpisodeMixin(models.AbstractModel):
    _name = "marketing.contact.center.episode.immutable.mixin"
    _description = "Immutable Marketing Contact Center Response Evidence"

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(
                _("Contact Center response episodes are created only by the bridge.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Contact Center response episodes cannot be edited."))

    def unlink(self):
        if (
            self.env.context.get("contact_center_deletion_token")
            is CONTACT_CENTER_DELETION_TOKEN
        ):
            return super().unlink()
        raise AccessError(_("Contact Center response episodes cannot be deleted."))


class MarketingContactCenterResponseSignal(models.Model):
    """First durable observation that a message affects response episodes."""

    _name = "marketing.contact.center.response.signal"
    _description = "Marketing Contact Center Response Signal"
    _inherit = "marketing.contact.center.episode.immutable.mixin"
    _order = "id"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    signal_kind = fields.Selection(
        [("inbound", "Inbound request"), ("response", "Confirmed human response")],
        required=True,
        index=True,
        readonly=True,
    )
    response_origin = fields.Selection(
        [("agent", "Odoo agent"), ("external_device", "External device")],
        index=True,
        readonly=True,
    )
    delivery_event_id = fields.Many2one(
        "contact.center.delivery.event",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)

    _sql_constraints = [
        (
            "message_unique",
            "unique(message_binding_id)",
            "The Contact Center message already has a response signal.",
        ),
    ]

    def init(self):
        """Keep the durable conversation timeline page seek index-only."""
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS
                marketing_cc_response_signal_timeline_idx
            ON marketing_contact_center_response_signal
                (channel_binding_id, observed_at, message_binding_id, id)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS
                marketing_cc_message_binding_seek_idx
            ON contact_center_message_binding (channel_binding_id, id)
            """
        )

    @api.constrains(
        "company_id",
        "channel_binding_id",
        "message_binding_id",
        "signal_kind",
        "response_origin",
        "delivery_event_id",
    )
    def _check_scope(self):
        for signal in self:
            message = signal.message_binding_id
            delivery = signal.delivery_event_id
            common_invalid = (
                message.company_id != signal.company_id
                or message.channel_binding_id != signal.channel_binding_id
                or signal.channel_binding_id.company_id != signal.company_id
                or (delivery and delivery.message_binding_id != message)
            )
            inbound_invalid = signal.signal_kind == "inbound" and (
                message.direction != "inbound"
                or message.origin != "provider"
                or signal.response_origin
                or delivery
            )
            response_invalid = signal.signal_kind == "response" and (
                message.direction != "outbound"
                or message.origin not in {"agent", "external_device"}
                or signal.response_origin != message.origin
                or (message.origin == "external_device" and delivery)
                or (
                    message.origin == "agent"
                    and not delivery
                    and message.delivery_state not in _CONFIRMED_DELIVERY_STATES
                )
            )
            if common_invalid or inbound_invalid or response_invalid:
                raise ValidationError(
                    _("The Contact Center response signal conflicts with its message.")
                )


class MarketingContactCenterResponseCursor(models.Model):
    """Mutable operational checkpoint over the immutable response signal ledger."""

    _name = "marketing.contact.center.response.cursor"
    _description = "Marketing Contact Center Response Cursor"
    _order = "id"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    last_signal_id = fields.Many2one(
        "marketing.contact.center.response.signal",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    last_observed_at = fields.Datetime(index=True, readonly=True)
    last_message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    last_sequence = fields.Integer(required=True, default=0, readonly=True)
    pending_episode_id = fields.Many2one(
        "marketing.contact.center.response.episode",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    backfill_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("materializing", "Materializing"),
            ("processing", "Processing"),
            ("done", "Done"),
        ],
        required=True,
        default="pending",
        index=True,
        readonly=True,
    )
    backfill_cutoff_message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    backfill_after_message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )

    _sql_constraints = [
        (
            "channel_unique",
            "unique(channel_binding_id)",
            "The response cursor already exists for this conversation.",
        ),
        (
            "sequence_nonnegative",
            "check(last_sequence >= 0)",
            "The response cursor sequence is invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Contact Center response cursors are internal."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Contact Center response cursors are internal."))
        mutable = {
            "last_signal_id",
            "last_observed_at",
            "last_message_binding_id",
            "last_sequence",
            "pending_episode_id",
            "backfill_state",
            "backfill_cutoff_message_binding_id",
            "backfill_after_message_binding_id",
        }
        if set(values) - mutable:
            raise AccessError(_("Contact Center response cursor scope is immutable."))
        return super().write(values)

    def unlink(self):
        if (
            self.env.context.get("contact_center_deletion_token")
            is CONTACT_CENTER_DELETION_TOKEN
        ):
            return super().unlink()
        raise AccessError(_("Contact Center response cursors cannot be deleted."))

    @api.constrains(
        "company_id",
        "channel_binding_id",
        "last_signal_id",
        "pending_episode_id",
        "last_message_binding_id",
        "backfill_cutoff_message_binding_id",
        "backfill_after_message_binding_id",
    )
    def _check_scope(self):
        for cursor in self:
            if (
                cursor.channel_binding_id.company_id != cursor.company_id
                or (
                    cursor.last_signal_id
                    and cursor.last_signal_id.channel_binding_id
                    != cursor.channel_binding_id
                )
                or (
                    cursor.pending_episode_id
                    and cursor.pending_episode_id.channel_binding_id
                    != cursor.channel_binding_id
                )
                or (
                    cursor.last_message_binding_id
                    and cursor.last_message_binding_id.channel_binding_id
                    != cursor.channel_binding_id
                )
                or (
                    cursor.backfill_cutoff_message_binding_id
                    and cursor.backfill_cutoff_message_binding_id.channel_binding_id
                    != cursor.channel_binding_id
                )
                or (
                    cursor.backfill_after_message_binding_id
                    and cursor.backfill_after_message_binding_id.channel_binding_id
                    != cursor.channel_binding_id
                )
            ):
                raise ValidationError(
                    _("The response cursor must stay inside one conversation.")
                )

    @api.constrains(
        "last_signal_id",
        "last_observed_at",
        "last_message_binding_id",
        "backfill_state",
        "backfill_cutoff_message_binding_id",
        "backfill_after_message_binding_id",
    )
    def _check_frontiers(self):
        for cursor in self:
            processing_frontier = (
                bool(cursor.last_signal_id),
                bool(cursor.last_observed_at),
                bool(cursor.last_message_binding_id),
            )
            if len(set(processing_frontier)) != 1:
                raise ValidationError(
                    _("The response processing frontier is incomplete.")
                )
            if (
                cursor.backfill_state
                in {
                    "materializing",
                    "processing",
                }
                and not cursor.backfill_cutoff_message_binding_id
            ):
                raise ValidationError(_("The response backfill cutoff is missing."))
            if cursor.backfill_after_message_binding_id and (
                not cursor.backfill_cutoff_message_binding_id
                or cursor.backfill_after_message_binding_id.id
                > cursor.backfill_cutoff_message_binding_id.id
            ):
                raise ValidationError(
                    _("The response backfill frontier exceeds its cutoff.")
                )


class MarketingContactCenterResponseEpisode(models.Model):
    _name = "marketing.contact.center.response.episode"
    _description = "Marketing Contact Center Response Episode"
    _inherit = "marketing.contact.center.episode.immutable.mixin"
    _order = "started_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True, size=36, index=True, readonly=True, copy=False
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    sequence = fields.Integer(required=True, readonly=True)
    start_message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    started_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    started_event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    policy_version = fields.Integer(required=True, readonly=True)
    response_ids = fields.One2many(
        "marketing.contact.center.response", "episode_id", readonly=True
    )
    response_id = fields.Many2one(
        "marketing.contact.center.response", compute="_compute_response_id"
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The response episode reference must be unique.",
        ),
        (
            "channel_sequence_unique",
            "unique(channel_binding_id, sequence)",
            "The response episode sequence is already present.",
        ),
        (
            "start_message_unique",
            "unique(start_message_binding_id)",
            "The inbound message already starts a response episode.",
        ),
        (
            "sequence_policy_positive",
            "check(sequence > 0 and policy_version > 0)",
            "The response episode sequence and policy must be positive.",
        ),
    ]

    @api.depends("response_ids")
    def _compute_response_id(self):
        for episode in self:
            episode.response_id = episode.response_ids[:1]

    @api.constrains("company_id", "channel_binding_id", "start_message_binding_id")
    def _check_scope(self):
        for episode in self:
            start = episode.start_message_binding_id
            if (
                episode.channel_binding_id.company_id != episode.company_id
                or start.company_id != episode.company_id
                or start.channel_binding_id != episode.channel_binding_id
                or start.direction != "inbound"
                or start.origin != "provider"
                or episode.started_event_id.company_id != episode.company_id
            ):
                raise ValidationError(
                    _(
                        "A response episode must start from one inbound provider message."
                    )
                )


class MarketingContactCenterResponse(models.Model):
    _name = "marketing.contact.center.response"
    _description = "Marketing Contact Center First Human Response"
    _inherit = "marketing.contact.center.episode.immutable.mixin"
    _order = "responded_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True, size=36, index=True, readonly=True, copy=False
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    episode_id = fields.Many2one(
        "marketing.contact.center.response.episode",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    delivery_event_id = fields.Many2one(
        "contact.center.delivery.event",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    response_origin = fields.Selection(
        [("agent", "Odoo agent"), ("external_device", "External device")],
        required=True,
        index=True,
        readonly=True,
    )
    responded_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    response_key = fields.Char(required=True, size=64, index=True, readonly=True)
    response_event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    policy_version = fields.Integer(required=True, readonly=True)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The response reference must be unique.",
        ),
        (
            "episode_unique",
            "unique(episode_id)",
            "The response episode already has a human response.",
        ),
        (
            "message_unique",
            "unique(message_binding_id)",
            "The message already closes a response episode.",
        ),
        (
            "response_key_unique",
            "unique(response_key)",
            "The human response evidence already exists.",
        ),
        (
            "response_key_sha256",
            "check(char_length(response_key) = 64)",
            "The response key must be a SHA-256 digest.",
        ),
        (
            "policy_positive",
            "check(policy_version > 0)",
            "The response policy must be positive.",
        ),
    ]

    @api.constrains(
        "company_id",
        "episode_id",
        "message_binding_id",
        "delivery_event_id",
        "response_origin",
    )
    def _check_scope(self):
        for response in self:
            message = response.message_binding_id
            delivery = response.delivery_event_id
            if (
                response.episode_id.company_id != response.company_id
                or message.company_id != response.company_id
                or message.channel_binding_id != response.episode_id.channel_binding_id
                or message.direction != "outbound"
                or message.origin != response.response_origin
                or response.response_event_id.company_id != response.company_id
                or (delivery and delivery.message_binding_id != message)
                or response.responded_at < response.episode_id.started_at
            ):
                raise ValidationError(
                    _("The human response evidence does not match its episode.")
                )


# These extensions are intentionally colocated with the response-episode aggregate.
# pylint: disable=consider-merging-classes-inherited
class ContactCenterMessageBinding(models.Model):
    _inherit = "contact.center.message.binding"

    @api.model_create_multi
    def create(self, vals_list):
        bindings = super().create(vals_list)
        if self.env.context.get("marketing_contact_center_skip_lifecycle_enqueue"):
            return bindings
        signals = (
            self.env["marketing.contact.center.response.episode.service"]
            .sudo()
            ._record_signals(bindings)
        )
        signals.mapped("message_binding_id")._enqueue_marketing_response_episode()
        return bindings

    def _contact_center_apply_delivery(self, state, **kwargs):
        result = super()._contact_center_apply_delivery(state, **kwargs)
        if state in _CONFIRMED_DELIVERY_STATES and not self.env.context.get(
            "marketing_contact_center_skip_lifecycle_enqueue"
        ):
            signals = (
                self.env["marketing.contact.center.response.episode.service"]
                .sudo()
                ._record_signals(self)
            )
            signals.mapped("message_binding_id")._enqueue_marketing_response_episode()
        return result

    def _marketing_is_response_episode_signal(self):
        self.ensure_one()
        return bool(
            self.env["marketing.contact.center.response.signal"]
            .sudo()
            .search_count([("message_binding_id", "=", self.id)])
        )

    def _enqueue_marketing_response_episode(self):
        for message_binding in self.sudo().exists():
            company = message_binding.company_id
            message_binding.with_context(allowed_company_ids=[company.id]).with_company(
                company
            ).with_delay(
                identity_key=(
                    "marketing_contact_center:response_episode:message:%s"
                    % message_binding.id
                ),
                max_retries=0,
                priority=41,
                description="Marketing response episode signal %s" % message_binding.id,
            )._job_sync_marketing_response_episode()
        return True

    def _job_sync_marketing_response_episode(self):
        self.ensure_one()
        message_binding = self.sudo().exists()
        if not message_binding:
            return True
        try:
            result = (
                self.env["marketing.contact.center.response.episode.service"]
                .sudo()
                .with_context(allowed_company_ids=[message_binding.company_id.id])
                .with_company(message_binding.company_id)
                ._reconcile_channel(message_binding.channel_binding_id)
            )
            if result["has_more"]:
                channel_binding = message_binding.channel_binding_id
                channel_binding._enqueue_marketing_response_episode_continuation(
                    result["continuation_token"]
                )
        except OperationalError:
            raise RetryableJobError(
                "Marketing response episode hit a concurrent database operation"
            ) from None
        return True


class ContactCenterChannelBinding(models.Model):
    _inherit = "contact.center.channel.binding"

    def _enqueue_marketing_response_episodes(self, *, priority=43):
        for binding in self.sudo().exists():
            company = binding.company_id
            binding.with_context(allowed_company_ids=[company.id]).with_company(
                company
            ).with_delay(
                identity_key="marketing_contact_center:response_episode:channel:%s"
                % binding.id,
                max_retries=0,
                priority=priority,
                description="Marketing response episode backfill %s" % binding.id,
            )._job_sync_marketing_response_episodes()
        return True

    def _enqueue_marketing_response_episode_continuation(self, continuation_token):
        """Queue the next bounded page without colliding with the active job."""
        continuation_token = str(continuation_token or "").strip()
        if not continuation_token or len(continuation_token) > 160:
            raise ValidationError(_("A valid response continuation token is required."))
        for binding in self.sudo().exists():
            company = binding.company_id
            binding.with_context(allowed_company_ids=[company.id]).with_company(
                company
            ).with_delay(
                identity_key=(
                    "marketing_contact_center:response_episode:channel:%s:page:%s"
                    % (binding.id, continuation_token)
                ),
                max_retries=0,
                priority=43,
                description="Marketing response episode page %s" % binding.id,
            )._job_sync_marketing_response_episodes()
        return True

    def _job_sync_marketing_response_episodes(self):
        self.ensure_one()
        service = self.env["marketing.contact.center.response.episode.service"].sudo()
        try:
            # Fence the channel before ``exists()`` establishes the job's
            # REPEATABLE READ snapshot. A busy key is retried in a new job
            # transaction instead of waiting with stale state.
            service._lock_channel(self.sudo())
            binding = self.sudo().exists()
            if not binding:
                return True
            result = (
                service.with_context(allowed_company_ids=[binding.company_id.id])
                .with_company(binding.company_id)
                ._reconcile_channel(binding)
            )
            if result["has_more"]:
                binding._enqueue_marketing_response_episode_continuation(
                    result["continuation_token"]
                )
        except OperationalError:
            raise RetryableJobError(
                "Marketing response episode backfill hit a concurrent database "
                "operation"
            ) from None
        return True


class MarketingContactCenterResponseEpisodeService(models.AbstractModel):
    _name = "marketing.contact.center.response.episode.service"
    _description = "Marketing Contact Center Response Episode Service"

    @api.model
    def _validated_binding(self, binding):
        binding = binding.sudo().exists()
        if (
            getattr(binding, "_name", "") != "contact.center.channel.binding"
            or len(binding) != 1
            or not binding.company_id
            or binding.channel_id.channel_type != "contact_center"
        ):
            raise ValidationError(
                _("A single valid Contact Center conversation is required.")
            )
        if binding.company_id not in self.env.companies:
            raise AccessError(_("The Contact Center conversation is not available."))
        return binding

    @api.model
    def _lock_channel(self, binding):
        if (
            getattr(binding, "_name", "") != "contact.center.channel.binding"
            or len(binding) != 1
        ):
            raise ValidationError(
                _("A single valid Contact Center conversation is required.")
            )
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_contact_center_response_episode:binding:%s" % binding.id,
            "Concurrent Contact Center response projection requires a fresh snapshot",
        )

    @api.model
    def _page_size(self, page_size=None):
        return min(max(int(page_size or _RESPONSE_PAGE_SIZE), 1), 1000)

    @api.model
    def _source_message_ids(
        self,
        binding,
        after_message_id=0,
        upper_message_id=0,
        limit=None,
    ):
        filters = ["message_binding.channel_binding_id = %s"]
        params = [binding.id]
        if after_message_id:
            filters.append("message_binding.id > %s")
            params.append(after_message_id)
        if upper_message_id:
            filters.append("message_binding.id <= %s")
            params.append(upper_message_id)
        params.append(self._page_size(limit))
        self.env.cr.execute(
            """
            SELECT message_binding.id
              FROM contact_center_message_binding AS message_binding
             WHERE __WHERE_FILTER__
             ORDER BY message_binding.id ASC
             LIMIT %s
            """.replace(
                "__WHERE_FILTER__", " AND ".join(filters)
            ),
            params,
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _signal_candidate_rows(
        self,
        binding,
        message_ids=None,
        limit=None,
    ):
        """Return one stable seek page of messages eligible for the signal ledger."""
        page_size = self._page_size(limit)
        if message_ids is None:
            message_ids = self._source_message_ids(binding, limit=page_size)
        message_ids = sorted(set(message_ids))
        if len(message_ids) > page_size:
            raise ValidationError(
                _("The response signal source page exceeds its bounded size.")
            )
        if not message_ids:
            return []
        params = [binding.id]
        params.append(message_ids)
        params.append(page_size)
        self.env.cr.execute(
            """
            SELECT message_binding.id AS message_binding_id,
                   CASE
                       WHEN message_binding.direction = 'inbound'
                       THEN 'inbound'
                       ELSE 'response'
                   END AS signal_kind,
                   CASE
                       WHEN message_binding.direction = 'outbound'
                       THEN message_binding.origin
                       ELSE NULL
                   END AS response_origin,
                   CASE
                       WHEN message_binding.origin = 'agent'
                       THEN COALESCE(
                           delivery.occurred_at,
                           message.date,
                           message_binding.create_date
                       )
                       ELSE COALESCE(message.date, message_binding.create_date)
                   END AS occurred_at,
                   CASE
                       WHEN signal.id IS NOT NULL
                       THEN signal.observed_at
                       WHEN message_binding.origin = 'agent'
                       THEN COALESCE(
                           delivery.create_date,
                           message_binding.create_date
                       )
                       ELSE message_binding.create_date
                   END AS observed_at,
                   delivery.id AS delivery_event_id,
                   signal.id AS signal_id
              FROM contact_center_message_binding AS message_binding
              JOIN mail_message AS message
                ON message.id = message_binding.message_id
              LEFT JOIN LATERAL (
                    SELECT delivery_candidate.id,
                           delivery_candidate.occurred_at,
                           delivery_candidate.create_date
                      FROM contact_center_delivery_event AS delivery_candidate
                     WHERE delivery_candidate.message_binding_id = message_binding.id
                       AND delivery_candidate.state IN ('sent', 'delivered', 'read')
                     ORDER BY delivery_candidate.create_date ASC,
                              delivery_candidate.id ASC
                     LIMIT 1
              ) AS delivery ON message_binding.origin = 'agent'
              LEFT JOIN marketing_contact_center_response_signal AS signal
                ON signal.message_binding_id = message_binding.id
             WHERE message_binding.channel_binding_id = %s
               AND (
                    (message_binding.direction = 'inbound'
                     AND message_binding.origin = 'provider')
                    OR
                    (message_binding.direction = 'outbound'
                     AND message_binding.origin = 'external_device')
                    OR
                    (message_binding.direction = 'outbound'
                     AND message_binding.origin = 'agent'
                     AND (
                          delivery.id IS NOT NULL
                          OR message_binding.delivery_state
                             IN ('sent', 'delivered', 'read')
                     ))
               )
               AND message_binding.id = ANY(%s)
             ORDER BY message_binding.id ASC
             LIMIT %s
            """,
            params,
        )
        return self.env.cr.fetchall()

    @api.model
    def _create_signal_rows(self, binding, rows, clamp_to_cursor=False):
        Signal = self.env["marketing.contact.center.response.signal"].sudo()
        observed_floor = False
        if clamp_to_cursor:
            cursor = (
                self.env["marketing.contact.center.response.cursor"]
                .sudo()
                .search([("channel_binding_id", "=", binding.id)], limit=1)
            )
            if cursor.backfill_state in {"processing", "done"}:
                observed_floor = cursor.last_observed_at
                message_floor_id = cursor.last_message_binding_id.id
            else:
                message_floor_id = 0
        values = []
        message_ids = []
        for row in rows:
            message_ids.append(row[0])
            if row[6]:
                continue
            observed_at = fields.Datetime.to_datetime(row[4])
            if observed_floor and (observed_at, row[0]) <= (
                observed_floor,
                message_floor_id,
            ):
                # A source transaction can start before the snapshot and commit
                # after its historical position was consumed.  Move only the
                # bridge observation beyond that durable frontier; the provider
                # occurrence timestamp remains untouched.
                # Odoo 16 serializes Datetime values at second precision.
                observed_at = observed_floor + datetime.timedelta(seconds=1)
            values.append(
                {
                    "company_id": binding.company_id.id,
                    "channel_binding_id": binding.id,
                    "message_binding_id": row[0],
                    "signal_kind": row[1],
                    "response_origin": row[2] or False,
                    "occurred_at": row[3],
                    "observed_at": observed_at,
                    "delivery_event_id": row[5] or False,
                }
            )
        if values:
            Signal.with_company(binding.company_id).with_context(
                marketing_contact_center_episode_write_token=(
                    MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
                )
            ).create(values)
        if not message_ids:
            return Signal
        return Signal.search(
            [
                ("channel_binding_id", "=", binding.id),
                ("message_binding_id", "in", message_ids),
            ],
            order="observed_at, id",
        )

    @api.model
    def _record_channel_signals(
        self,
        binding,
        message_ids=None,
        lock=True,
        limit=None,
    ):
        binding = self._validated_binding(binding)
        if lock:
            self._lock_channel(binding)
        rows = self._signal_candidate_rows(
            binding,
            message_ids=message_ids,
            limit=limit,
        )
        return self._create_signal_rows(
            binding,
            rows,
            # This is the live/explicit ingestion path.  Even a maintenance
            # caller that asks for the first bounded source page must not insert
            # a historical observation behind a completed processing cursor.
            # Snapshot materialization calls ``_create_signal_rows`` directly
            # and deliberately leaves clamping disabled until its barrier ends.
            clamp_to_cursor=True,
        )

    @api.model
    def _record_signals(self, message_bindings):
        message_bindings = message_bindings.sudo().exists()
        if getattr(message_bindings, "_name", "") != "contact.center.message.binding":
            raise ValidationError(_("Valid Contact Center messages are required."))
        result = self.env["marketing.contact.center.response.signal"]
        for binding in message_bindings.mapped("channel_binding_id").sorted("id"):
            scoped = message_bindings.filtered(
                lambda message: message.channel_binding_id == binding
            )
            self._lock_channel(self._validated_binding(binding))
            page_size = self._page_size()
            message_ids = sorted(scoped.ids)
            for offset in range(0, len(message_ids), page_size):
                result |= self._record_channel_signals(
                    binding,
                    message_ids[offset : offset + page_size],
                    lock=False,
                    limit=page_size,
                )
        return result

    @api.model
    def _timeline(
        self,
        binding,
        after_order=None,
        limit=None,
    ):
        filters = ["signal.channel_binding_id = %s"]
        params = [binding.id]
        if after_order:
            filters.append(
                "(signal.observed_at, signal.message_binding_id, signal.id) "
                "> (%s, %s, %s)"
            )
            params.extend(after_order)
        params.append(self._page_size(limit))
        self.env.cr.execute(
            """
            SELECT signal.id,
                   signal.message_binding_id,
                   signal.signal_kind,
                   signal.response_origin,
                   signal.occurred_at,
                   signal.observed_at,
                   signal.delivery_event_id
              FROM marketing_contact_center_response_signal AS signal
             WHERE __WHERE_FILTER__
             ORDER BY signal.observed_at ASC,
                      signal.message_binding_id ASC,
                      signal.id ASC
             LIMIT %s
            """.replace(
                "__WHERE_FILTER__", " AND ".join(filters)
            ),
            params,
        )
        return self.env.cr.fetchall()

    @api.model
    def _cursor(self, binding):
        Cursor = self.env["marketing.contact.center.response.cursor"].sudo()
        cursor = Cursor.search([("channel_binding_id", "=", binding.id)], limit=1)
        if cursor:
            return cursor
        return (
            Cursor.with_company(binding.company_id)
            .with_context(
                marketing_contact_center_episode_write_token=(
                    MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
                )
            )
            .create(
                {
                    "company_id": binding.company_id.id,
                    "channel_binding_id": binding.id,
                }
            )
        )

    @api.model
    def _write_cursor(self, cursor, values):
        return cursor.with_context(
            marketing_contact_center_episode_write_token=(
                MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
            )
        ).write(values)

    @api.model
    def _message_ref(self, message_binding):
        return self.env[
            "marketing.contact.center.lifecycle.service"
        ]._message_public_ref(message_binding)

    @api.model
    def _ingest_event(self, binding, dto):
        result = (
            self.env["marketing.business.event.service"]
            .sudo()
            .with_context(allowed_company_ids=[binding.company_id.id])
            .with_company(binding.company_id)
            ._ingest_event(binding.company_id, dto)
        )
        return self.env["marketing.business.event"].sudo().browse(result.event_id)

    @api.model
    def _episode_event(self, binding, public_ref, sequence, message, started_at):
        message_ref = self._message_ref(message)
        event_key = "contact.center:%s:episode:%s:started" % (
            binding.channel_id.uuid,
            sequence,
        )
        return self._ingest_event(
            binding,
            MarketingBusinessEventDTO(
                event_class="lifecycle",
                event_type="interaction_started",
                source_system="contact_center",
                source_model="mail.channel",
                source_res_id=binding.channel_id.id,
                source_occurrence_ref=event_key,
                source_evidence_ref="contact.center.message:%s" % message_ref,
                business_event_key=event_key,
                occurred_at=started_at,
                observed_at=message.create_date or started_at,
                evidence_level="provider_asserted",
                extensions={
                    "contact_center.channel_ref": str(binding.channel_id.uuid),
                    "contact_center.episode_ref": public_ref,
                    "contact_center.episode_sequence": sequence,
                    "contact_center.lifecycle_version": 2,
                    "contact_center.message_ref": message_ref,
                },
            ),
        )

    @api.model
    def _response_event(
        self, binding, episode, message, response_origin, responded_at, observed_at
    ):
        # The conversation-level lifecycle projection and the per-episode
        # projection may be queued by the same delivery receipt.  They must
        # serialize on the same lock before adopting the shared response event.
        self.env["marketing.contact.center.lifecycle.service"]._lock_event(
            binding, "first_human_response"
        )
        message_ref = self._message_ref(message)
        existing = (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", binding.company_id.id),
                    ("source_system", "=", "contact_center"),
                    ("source_model", "=", "mail.channel"),
                    ("source_res_id", "=", binding.channel_id.id),
                    ("event_type", "=", "first_human_response"),
                    (
                        "source_evidence_ref",
                        "=",
                        "contact.center.message:%s" % message_ref,
                    ),
                ],
                limit=1,
            )
        )
        if existing:
            return existing
        event_key = "contact.center:%s:episode:%s:first_response:%s" % (
            binding.channel_id.uuid,
            episode.sequence,
            message_ref,
        )
        return self._ingest_event(
            binding,
            MarketingBusinessEventDTO(
                event_class="lifecycle",
                event_type="first_human_response",
                source_system="contact_center",
                source_model="mail.channel",
                source_res_id=binding.channel_id.id,
                source_occurrence_ref=event_key,
                source_evidence_ref="contact.center.message:%s" % message_ref,
                business_event_key=event_key,
                occurred_at=responded_at,
                observed_at=observed_at,
                evidence_level="first_party",
                extensions={
                    "contact_center.channel_ref": str(binding.channel_id.uuid),
                    "contact_center.episode_ref": episode.public_ref,
                    "contact_center.episode_sequence": episode.sequence,
                    "contact_center.lifecycle_version": 2,
                    "contact_center.message_ref": message_ref,
                    "contact_center.response_origin": response_origin,
                },
            ),
        )

    @api.model
    def _get_or_create_episode(
        self, binding, sequence, message_binding, started_at, observed_at
    ):
        model = self.env["marketing.contact.center.response.episode"].sudo()
        episode = model.search(
            [("start_message_binding_id", "=", message_binding.id)], limit=1
        )
        if episode:
            if (
                episode.channel_binding_id != binding
                or episode.sequence != sequence
                or episode.company_id != binding.company_id
            ):
                raise ValidationError(
                    _("The persisted response episode conflicts with the timeline.")
                )
            return episode
        public_ref = str(uuid.uuid4())
        event = self._episode_event(
            binding, public_ref, sequence, message_binding, started_at
        )
        return (
            model.with_company(binding.company_id)
            .with_context(
                marketing_contact_center_episode_write_token=(
                    MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
                )
            )
            .create(
                {
                    "public_ref": public_ref,
                    "company_id": binding.company_id.id,
                    "channel_binding_id": binding.id,
                    "sequence": sequence,
                    "start_message_binding_id": message_binding.id,
                    "started_at": started_at,
                    "observed_at": observed_at,
                    "started_event_id": event.id,
                    "policy_version": _POLICY_VERSION,
                }
            )
        )

    @api.model
    def _get_or_create_response(
        self,
        binding,
        episode,
        message_binding,
        delivery_event,
        response_origin,
        responded_at,
        observed_at,
    ):
        model = self.env["marketing.contact.center.response"].sudo()
        response = model.search([("episode_id", "=", episode.id)], limit=1)
        if response:
            return response
        event = self._response_event(
            binding,
            episode,
            message_binding,
            response_origin,
            responded_at,
            observed_at,
        )
        response_key = _sha256(
            "%s:%s:%s"
            % (
                episode.public_ref,
                message_binding.id,
                self._message_ref(message_binding),
            )
        )
        return (
            model.with_company(binding.company_id)
            .with_context(
                marketing_contact_center_episode_write_token=(
                    MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
                )
            )
            .create(
                {
                    "public_ref": str(uuid.uuid4()),
                    "company_id": binding.company_id.id,
                    "episode_id": episode.id,
                    "message_binding_id": message_binding.id,
                    "delivery_event_id": delivery_event.id or False,
                    "response_origin": response_origin,
                    "responded_at": responded_at,
                    "observed_at": observed_at,
                    "response_key": response_key,
                    "response_event_id": event.id,
                    "policy_version": _POLICY_VERSION,
                }
            )
        )

    @api.model
    def _initialize_backfill(self, binding, cursor):
        self.env.cr.execute(
            """
            SELECT message_binding.id
              FROM contact_center_message_binding AS message_binding
             WHERE message_binding.channel_binding_id = %s
             ORDER BY message_binding.id DESC
             LIMIT 1
            """,
            [binding.id],
        )
        cutoff_row = self.env.cr.fetchone()
        if not cutoff_row:
            self._write_cursor(
                cursor,
                {"backfill_state": "done"},
            )
            return cursor
        self._write_cursor(
            cursor,
            {
                "backfill_state": "materializing",
                "backfill_cutoff_message_binding_id": cutoff_row[0],
                "backfill_after_message_binding_id": False,
            },
        )
        return cursor

    @api.model
    def _materialize_backfill_page(self, binding, cursor, page_size):
        message_ids = self._source_message_ids(
            binding,
            after_message_id=cursor.backfill_after_message_binding_id.id,
            upper_message_id=cursor.backfill_cutoff_message_binding_id.id,
            limit=page_size,
        )
        rows = self._signal_candidate_rows(
            binding,
            message_ids=message_ids,
            limit=page_size,
        )
        self._create_signal_rows(binding, rows)
        values = {}
        if message_ids:
            values.update(
                {
                    "backfill_after_message_binding_id": message_ids[-1],
                }
            )
        page_is_full = len(message_ids) == page_size
        if not page_is_full:
            values["backfill_state"] = "processing"
        if values:
            self._write_cursor(cursor, values)
        return page_is_full

    @api.model
    def _process_timeline_page(self, binding, cursor, page_size):
        after_order = None
        if cursor.last_observed_at:
            after_order = (
                cursor.last_observed_at,
                cursor.last_message_binding_id.id,
                cursor.last_signal_id.id,
            )
        rows = self._timeline(
            binding,
            after_order=after_order,
            limit=page_size,
        )
        pending = cursor.pending_episode_id
        sequence = cursor.last_sequence
        last_signal_id = cursor.last_signal_id.id or 0
        last_observed_at = cursor.last_observed_at
        last_message_id = cursor.last_message_binding_id.id or 0
        for row in rows:
            signal_id, message_id, signal_kind, response_origin = row[:4]
            occurred_at = fields.Datetime.to_datetime(row[4])
            observed_at = fields.Datetime.to_datetime(row[5] or row[4])
            delivery_id = row[6]
            message = (
                self.env["contact.center.message.binding"].sudo().browse(message_id)
            )
            if signal_kind == "inbound":
                if not pending:
                    sequence += 1
                    pending = self._get_or_create_episode(
                        binding,
                        sequence,
                        message,
                        occurred_at,
                        observed_at,
                    )
            elif (
                pending
                and message.id > pending.start_message_binding_id.id
                and occurred_at >= pending.started_at
            ):
                delivery = (
                    self.env["contact.center.delivery.event"]
                    .sudo()
                    .browse(delivery_id or [])
                )
                self._get_or_create_response(
                    binding,
                    pending,
                    message,
                    delivery,
                    response_origin,
                    occurred_at,
                    observed_at,
                )
                pending = False
            last_signal_id = signal_id
            last_observed_at = observed_at
            last_message_id = message_id
        self._write_cursor(
            cursor,
            {
                "last_signal_id": last_signal_id or False,
                "last_observed_at": last_observed_at or False,
                "last_message_binding_id": last_message_id or False,
                "last_sequence": sequence,
                "pending_episode_id": pending.id if pending else False,
            },
        )
        return {
            "page_is_full": len(rows) == page_size,
            "episode_count": sequence,
            "pending_episode_ref": pending.public_ref if pending else "",
        }

    @api.model
    def _continuation_token(self, cursor):
        return _sha256(
            "%s:%s:%s:%s"
            % (
                cursor.backfill_state,
                cursor.backfill_after_message_binding_id.id or 0,
                cursor.last_signal_id.id or 0,
                cursor.last_sequence,
            )
        )[:24]

    @api.model
    def _reconcile_result(self, cursor, has_more):
        return {
            "episode_count": cursor.last_sequence,
            "pending_episode_ref": (
                cursor.pending_episode_id.public_ref
                if cursor.pending_episode_id
                else ""
            ),
            "has_more": bool(has_more),
            "backfill_state": cursor.backfill_state,
            "continuation_token": self._continuation_token(cursor),
        }

    @api.model
    def _reconcile_channel(self, binding, page_size=None):
        """Advance one bounded, chronologically safe conversation page.

        Every new cursor crosses the same durable snapshot barrier, including a
        cursor first seen by a live-message job.
        """
        self._lock_channel(binding)
        binding = self._validated_binding(binding)
        cursor = self._cursor(binding)
        page_size = self._page_size(page_size)
        if cursor.backfill_state == "pending":
            self._initialize_backfill(binding, cursor)
        if cursor.backfill_state == "materializing":
            if self._materialize_backfill_page(binding, cursor, page_size):
                return self._reconcile_result(cursor, has_more=True)
        if cursor.backfill_state == "processing":
            processed = self._process_timeline_page(
                binding,
                cursor,
                page_size,
            )
            if processed["page_is_full"]:
                return self._reconcile_result(cursor, has_more=True)
            self._write_cursor(cursor, {"backfill_state": "done"})
            return self._reconcile_result(cursor, has_more=False)

        processed = self._process_timeline_page(binding, cursor, page_size)
        return self._reconcile_result(
            cursor,
            has_more=processed["page_is_full"],
        )

    @api.model
    def _enqueue_backfill(self, company=None, after_id=0, limit=200):
        if company is None:
            company = self.env.company
        if (
            getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The backfill company is not available."))
        limit = min(max(int(limit), 1), 1000)
        after_id = max(int(after_id), 0)
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [("company_id", "=", company.id), ("id", ">", after_id)],
                order="id asc",
                limit=limit,
            )
        )
        bindings._enqueue_marketing_response_episodes()
        return {
            "enqueued_conversations": len(bindings),
            "last_id": bindings[-1:].id or after_id,
            "has_more": len(bindings) == limit,
        }
