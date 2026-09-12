import hashlib

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO
from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)

from ..services.retry import (
    MAX_BRIDGE_RETRIES,
    retry_database_error,
    retry_transient_database,
)

_LIFECYCLE_TYPES = frozenset({"conversation_started", "first_human_response"})
_CONFIRMED_DELIVERY_STATES = ("sent", "delivered", "read")


def _bounded_hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class ContactCenterMessageBinding(models.Model):
    _inherit = "contact.center.message.binding"

    @api.model_create_multi
    def create(self, vals_list):
        bindings = super().create(vals_list)
        if self.env.context.get("marketing_contact_center_skip_lifecycle_enqueue"):
            return bindings
        for binding in bindings.sudo():
            if binding._marketing_is_external_conversation_message():
                binding.channel_binding_id._enqueue_marketing_lifecycle(
                    "conversation_started"
                )
            if binding._marketing_is_confirmed_human_response():
                binding.channel_binding_id._enqueue_marketing_lifecycle(
                    "first_human_response"
                )
        return bindings

    def _contact_center_apply_delivery(self, state, **kwargs):
        result = super()._contact_center_apply_delivery(state, **kwargs)
        if state in _CONFIRMED_DELIVERY_STATES and not self.env.context.get(
            "marketing_contact_center_skip_lifecycle_enqueue"
        ):
            for binding in self.sudo().filtered(
                lambda item: item._marketing_is_confirmed_human_response()
            ):
                binding.channel_binding_id._enqueue_marketing_lifecycle(
                    "first_human_response"
                )
        return result

    def _marketing_is_external_conversation_message(self):
        self.ensure_one()
        return bool(
            (self.direction == "inbound" and self.origin == "provider")
            or self.origin == "external_device"
        )

    def _marketing_is_confirmed_human_response(self):
        self.ensure_one()
        return bool(
            self.direction == "outbound"
            and self.origin == "agent"
            and self.delivery_state in _CONFIRMED_DELIVERY_STATES
        )


class ContactCenterChannelBinding(models.Model):
    _inherit = "contact.center.channel.binding"

    def _marketing_lifecycle_identity_key(self, event_type, wake_scope="live"):
        self.ensure_one()
        if event_type not in _LIFECYCLE_TYPES:
            raise ValidationError(_("The Contact Center lifecycle type is invalid."))
        if wake_scope == "live":
            # The same transaction can observe the binding more than once, but a
            # later transaction must never be suppressed by a job that is already
            # running on an older database snapshot.
            self.env.cr.execute("SELECT txid_current()")
            wake_ref = "tx:%s" % self.env.cr.fetchone()[0]
        elif wake_scope == "backfill":
            wake_ref = "backfill"
        else:
            raise ValidationError(_("The Contact Center lifecycle wake-up is invalid."))
        return "marketing_contact_center:lifecycle:%s:%s:%s" % (
            event_type,
            self.channel_id.uuid,
            wake_ref,
        )

    def _enqueue_marketing_lifecycle(self, event_type, *, wake_scope="live"):
        for binding in self.sudo().exists():
            company = binding.company_id
            binding.with_context(allowed_company_ids=[company.id]).with_company(
                company
            ).with_delay(
                identity_key=binding._marketing_lifecycle_identity_key(
                    event_type, wake_scope=wake_scope
                ),
                max_retries=MAX_BRIDGE_RETRIES,
                priority=42,
                description="Marketing Contact Center lifecycle %s %s"
                % (event_type, binding.channel_id.uuid),
            )._job_sync_marketing_lifecycle(
                event_type
            )
        return True

    @retry_transient_database
    def _job_sync_marketing_lifecycle(self, event_type):
        self.ensure_one()
        service = self.env["marketing.contact.center.lifecycle.service"].sudo()
        try:
            # The record id is sufficient for the process-wide lock key. Acquire
            # it before ``exists()`` establishes a REPEATABLE READ snapshot.
            service._lock_event(self.sudo(), event_type)
            binding = self.sudo().exists()
            if not binding:
                return True
            (
                service.with_context(allowed_company_ids=[binding.company_id.id])
                .with_company(binding.company_id)
                ._sync_event(binding, event_type)
            )
        except OperationalError as error:
            retry_database_error(
                error,
                "Marketing Contact Center lifecycle hit a concurrent database "
                "operation",
            )
        return True


class MarketingContactCenterLifecycleService(models.AbstractModel):
    _name = "marketing.contact.center.lifecycle.service"
    _description = "Marketing Contact Center Lifecycle Bridge Service"

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
    def _lock_event(self, binding, event_type):
        if (
            event_type not in _LIFECYCLE_TYPES
            or getattr(binding, "_name", "") != "contact.center.channel.binding"
            or len(binding) != 1
        ):
            raise ValidationError(_("The Contact Center lifecycle type is invalid."))
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_contact_center_lifecycle:binding:%s:%s"
            % (binding.id, event_type),
            "Concurrent Contact Center lifecycle projection requires a fresh snapshot",
        )

    @api.model
    def _existing_event(self, binding, event_type):
        return (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", binding.company_id.id),
                    ("source_system", "=", "contact_center"),
                    ("source_model", "=", "mail.channel"),
                    ("source_res_id", "=", binding.channel_id.id),
                    ("event_type", "=", event_type),
                ],
                limit=1,
            )
        )

    @api.model
    def _first_external_message(self, binding):
        self.env.cr.execute(
            """
            SELECT message_binding.id
              FROM contact_center_message_binding AS message_binding
              JOIN mail_message AS message
                ON message.id = message_binding.message_id
             WHERE message_binding.channel_binding_id = %s
               AND (
                    (message_binding.direction = 'inbound'
                     AND message_binding.origin = 'provider')
                    OR message_binding.origin = 'external_device'
               )
             ORDER BY message.date ASC, message_binding.id ASC
             LIMIT 1
            """,
            [binding.id],
        )
        row = self.env.cr.fetchone()
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .browse(row[0] if row else [])
        )

    @api.model
    def _first_confirmed_human_response(self, binding, started_at):
        self.env.cr.execute(
            """
            SELECT candidate.source_kind,
                   candidate.source_id,
                   candidate.occurred_at
              FROM (
                    SELECT 'delivery'::text AS source_kind,
                           delivery.id AS source_id,
                           delivery.occurred_at,
                           delivery.id AS tie_breaker
                      FROM contact_center_delivery_event AS delivery
                      JOIN contact_center_message_binding AS message_binding
                        ON message_binding.id = delivery.message_binding_id
                     WHERE message_binding.channel_binding_id = %s
                       AND message_binding.direction = 'outbound'
                       AND message_binding.origin = 'agent'
                       AND delivery.state IN ('sent', 'delivered', 'read')

                    UNION ALL

                    SELECT 'binding'::text AS source_kind,
                           message_binding.id AS source_id,
                           message.date AS occurred_at,
                           message_binding.id AS tie_breaker
                      FROM contact_center_message_binding AS message_binding
                      JOIN mail_message AS message
                        ON message.id = message_binding.message_id
                     WHERE message_binding.channel_binding_id = %s
                       AND message_binding.direction = 'outbound'
                       AND message_binding.origin = 'agent'
                       AND message_binding.delivery_state
                           IN ('sent', 'delivered', 'read')
                       AND NOT EXISTS (
                            SELECT 1
                              FROM contact_center_delivery_event AS delivery
                             WHERE delivery.message_binding_id = message_binding.id
                               AND delivery.state IN ('sent', 'delivered', 'read')
                       )
              ) AS candidate
             WHERE candidate.occurred_at >= %s
             ORDER BY candidate.occurred_at ASC, candidate.tie_breaker ASC
             LIMIT 1
            """,
            [binding.id, binding.id, started_at],
        )
        row = self.env.cr.fetchone()
        if not row:
            return self.env["contact.center.message.binding"], False
        model_name = (
            "contact.center.delivery.event"
            if row[0] == "delivery"
            else "contact.center.message.binding"
        )
        return self.env[model_name].sudo().browse(row[1]), fields.Datetime.to_datetime(
            row[2]
        )

    @api.model
    def _message_public_ref(self, message_binding):
        message_binding.ensure_one()
        message = message_binding.message_id
        stable_source = (
            message.message_id
            or message_binding.client_message_id
            or message_binding.external_message_id
            or "binding:%s" % message_binding.id
        )
        return "sha256:%s" % _bounded_hash(stable_source)

    @api.model
    def _event_source(self, binding, event_type):
        external = self._first_external_message(binding)
        if not external:
            return self.env["contact.center.message.binding"], False
        started_at = fields.Datetime.to_datetime(
            external.message_id.date or external.message_id.create_date
        )
        if event_type == "conversation_started":
            return external, started_at
        return self._first_confirmed_human_response(binding, started_at)

    @api.model
    def _dto(self, binding, event_type, source, occurred_at):
        channel_ref = str(binding.channel_id.uuid)
        if event_type == "conversation_started":
            message_binding = source
            message_ref = self._message_public_ref(message_binding)
            occurrence_ref = "contact.center:%s:first_external:%s" % (
                channel_ref,
                message_ref,
            )
            business_key = "contact.center:%s:started" % channel_ref
            observed_at = message_binding.create_date or occurred_at
            evidence_level = "provider_asserted"
        else:
            message_binding = (
                source.message_binding_id
                if source._name == "contact.center.delivery.event"
                else source
            )
            message_ref = self._message_public_ref(message_binding)
            occurrence_ref = "contact.center:%s:first_response:%s" % (
                channel_ref,
                message_ref,
            )
            business_key = occurrence_ref
            observed_at = source.create_date or occurred_at
            evidence_level = "first_party"
        return MarketingBusinessEventDTO(
            event_class="lifecycle",
            event_type=event_type,
            source_system="contact_center",
            source_model="mail.channel",
            source_res_id=binding.channel_id.id,
            source_occurrence_ref=occurrence_ref,
            source_evidence_ref="contact.center.message:%s" % message_ref,
            business_event_key=business_key,
            occurred_at=occurred_at,
            observed_at=observed_at,
            evidence_level=evidence_level,
            extensions={
                "contact_center.account_ref": binding.account_id.external_ref,
                "contact_center.channel_ref": channel_ref,
                "contact_center.conversation_type": binding.conversation_type,
                "contact_center.message_ref": message_ref,
            },
        )

    @api.model
    def _sync_event(self, binding, event_type):
        if event_type not in _LIFECYCLE_TYPES:
            raise ValidationError(_("The Contact Center lifecycle type is invalid."))
        self._lock_event(binding, event_type)
        binding = self._validated_binding(binding)
        existing = self._existing_event(binding, event_type)
        if existing:
            return existing
        source, occurred_at = self._event_source(binding, event_type)
        if not source or not occurred_at:
            return self.env["marketing.business.event"]
        dto = self._dto(binding, event_type, source, occurred_at)
        result = (
            self.env["marketing.business.event.service"]
            .sudo()
            .with_context(allowed_company_ids=[binding.company_id.id])
            .with_company(binding.company_id)
            ._ingest_event(binding.company_id, dto)
        )
        return self.env["marketing.business.event"].sudo().browse(result.event_id)

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
        for binding in bindings:
            binding._enqueue_marketing_lifecycle(
                "conversation_started", wake_scope="backfill"
            )
            binding._enqueue_marketing_lifecycle(
                "first_human_response", wake_scope="backfill"
            )
        return {
            "enqueued_conversations": len(bindings),
            "last_id": bindings[-1:].id or after_id,
            "has_more": len(bindings) == limit,
        }
