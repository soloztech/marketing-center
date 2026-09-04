import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.scheduler import fair_scheduler_batch
from odoo.addons.marketing_center_website.services.contracts import sha256_text

from .tokens import WEBSITE_CRM_WRITE_TOKEN


class MarketingWebsiteCrmIntent(models.Model):
    _name = "marketing.website.crm.intent"
    _description = "Durable Website Form CRM Intent"
    _order = "created_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda _self: str(uuid.uuid4()),
        size=36,
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True, ondelete="restrict"
    )
    website_id = fields.Many2one(
        "website",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    action_id = fields.Many2one(
        "marketing.website.action",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        readonly=True,
        index=True,
        ondelete="set null",
        check_company=True,
    )
    lead_model = fields.Char(required=True, readonly=True, size=64)
    lead_res_id = fields.Integer(required=True, readonly=True, index=True)
    lead_display_ref = fields.Char(required=True, readonly=True, size=256)
    event_ref = fields.Char(required=True, readonly=True, index=True, size=36)
    session_ref = fields.Char(required=True, readonly=True, index=True, size=36)
    session_hash = fields.Char(required=True, readonly=True, index=True, size=64)
    origin = fields.Char(required=True, readonly=True, size=512)
    occurred_at = fields.Datetime(required=True, readonly=True, index=True)
    session_reconcile_until = fields.Datetime(required=True, readonly=True, index=True)
    session_reconcile_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("watching", "Watching"),
            ("complete", "Complete"),
            ("failed", "Failed"),
        ],
        required=True,
        readonly=True,
        default="pending",
        index=True,
    )
    session_reconciled_at = fields.Datetime(readonly=True, index=True)
    next_session_reconcile_at = fields.Datetime(readonly=True, index=True)
    session_reconcile_error_class = fields.Char(readonly=True, size=128)
    session_reconcile_error_message = fields.Char(readonly=True, size=256)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("retry", "Retry"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("tombstoned", "CRM lead removed"),
        ],
        required=True,
        readonly=True,
        default="pending",
        index=True,
    )
    attempts = fields.Integer(required=True, readonly=True, default=0)
    next_retry_at = fields.Datetime(readonly=True, index=True)
    queue_job_uuid = fields.Char(readonly=True, index=True, size=64)
    last_error_class = fields.Char(readonly=True, size=128)
    last_error_message = fields.Char(readonly=True, size=256)
    correlation_id = fields.Many2one(
        "marketing.website.crm.correlation",
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    created_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now, index=True
    )
    processed_at = fields.Datetime(readonly=True, index=True)

    _sql_constraints = [
        ("public_ref_unique", "unique(public_ref)", "Intent reference must be unique."),
        (
            "endpoint_event_unique",
            "unique(endpoint_id, event_ref)",
            "A Website form event can create only one CRM intent.",
        ),
        (
            "attempts_bounded",
            "check(attempts between 0 and 8)",
            "Website CRM intent attempts are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not self._internal():
            raise AccessError(_("Website CRM intents are managed internally."))
        return super().create(vals_list)

    def write(self, values):
        if not self._internal():
            raise AccessError(_("Website CRM intents are managed internally."))
        mutable = {
            "state",
            "attempts",
            "next_retry_at",
            "queue_job_uuid",
            "last_error_class",
            "last_error_message",
            "correlation_id",
            "processed_at",
            "session_reconcile_state",
            "session_reconciled_at",
            "next_session_reconcile_at",
            "session_reconcile_error_class",
            "session_reconcile_error_message",
        }
        if set(values) - mutable:
            raise AccessError(_("Website CRM intent identity is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Website CRM intents cannot be deleted."))

    @api.model
    def _internal(self):
        return (
            self.env.context.get("marketing_website_crm_write_token")
            is WEBSITE_CRM_WRITE_TOKEN
        )

    def _internal_write(self, values):
        return self.with_context(
            marketing_website_crm_write_token=WEBSITE_CRM_WRITE_TOKEN
        ).write(values)

    def _identity_key(self):
        self.ensure_one()
        return "marketing_website_crm:intent:%s" % self.public_ref

    def _active_job(self):
        self.ensure_one()
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", self._identity_key()),
                    (
                        "state",
                        "in",
                        ("pending", "enqueued", "started", "wait_dependencies"),
                    ),
                ],
                limit=1,
            )
        )

    def _enqueue(self):
        for intent in self.sudo().exists().sorted("id"):
            intent.flush_recordset(["state", "queue_job_uuid"])
            intent.env.cr.execute(
                "SELECT state, queue_job_uuid "
                "FROM marketing_website_crm_intent WHERE id = %s FOR UPDATE",
                [intent.id],
            )
            row = intent.env.cr.fetchone()
            if not row or row[0] not in {"pending", "retry"}:
                continue
            intent.invalidate_recordset(["state", "queue_job_uuid"])
            active = intent._active_job()
            if active:
                if intent.queue_job_uuid != str(active.uuid):
                    intent._internal_write({"queue_job_uuid": str(active.uuid)})
                continue
            eta_seconds = 0
            if intent.next_retry_at:
                eta_seconds = max(
                    int((intent.next_retry_at - fields.Datetime.now()).total_seconds()),
                    0,
                )
            delayed = (
                intent.with_company(intent.company_id)
                .with_delay(
                    identity_key=intent._identity_key(),
                    max_retries=8,
                    priority=28,
                    eta=eta_seconds or None,
                    description="Process Website CRM intent %s" % intent.public_ref,
                )
                ._job_process()
            )
            intent._internal_write({"queue_job_uuid": str(delayed.uuid)})
        return True

    def action_retry(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only Marketing Center administrators can retry."))
        # The role is global, while the evidence is company-scoped. Authorize
        # the caller-owned recordset before crossing the internal sudo boundary;
        # knowing an intent id from another company must not grant an operation
        # on it. The model is deliberately read-only by ACL, so this explicit
        # service action requires read visibility rather than generic ORM write.
        self.check_access_rights("read")
        self.check_access_rule("read")
        for intent in self.sudo().exists():
            if intent.state != "failed":
                continue
            intent._internal_write(
                {
                    "state": "retry",
                    "attempts": 0,
                    "next_retry_at": fields.Datetime.now(),
                    "queue_job_uuid": False,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            intent._enqueue()
        return True

    def action_retry_session(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can retry sessions.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        now = fields.Datetime.now()
        for intent in self.sudo().exists():
            if intent.state != "done" or intent.session_reconcile_state != "failed":
                continue
            expired = intent.session_reconcile_until < now
            intent._internal_write(
                {
                    "session_reconcile_state": "watching",
                    "next_session_reconcile_at": now,
                    "session_reconcile_error_class": False,
                    "session_reconcile_error_message": False,
                }
            )
            self.env["marketing.website.crm.service"].with_company(
                intent.company_id
            )._recover_session_intent(intent, now=now, final=expired)
        return True

    def _job_process(self):
        self.ensure_one()
        intent = self.sudo().exists()
        if not intent or intent.state in {"done", "failed", "tombstoned"}:
            return True
        job_uuid = self.env.context.get("job_uuid")
        intent.flush_recordset(["queue_job_uuid"])
        self.env.cr.execute(
            "SELECT queue_job_uuid FROM marketing_website_crm_intent "
            "WHERE id = %s FOR UPDATE",
            [intent.id],
        )
        ownership = self.env.cr.fetchone()
        if not ownership or not ownership[0] or ownership[0] != job_uuid:
            return False
        intent.invalidate_recordset(["queue_job_uuid"])
        return (
            self.env["marketing.website.crm.service"]
            .with_company(intent.company_id)
            ._attempt_intent(intent)
        )

    @api.model
    def _cron_enqueue_due(self, now=None, limit=200):
        now = now or fields.Datetime.now()
        limit = min(max(int(limit), 1), 1000)
        due = fair_scheduler_batch(
            self.env,
            self._name,
            [
                ("state", "in", ("pending", "retry")),
                "|",
                ("next_retry_at", "=", False),
                ("next_retry_at", "<=", now),
            ],
            cursor_key="website_crm.intent_due",
            limit=limit,
        )
        enqueueable = self.browse()
        for intent in due:
            intent.flush_recordset(["state", "next_retry_at", "queue_job_uuid"])
            intent.env.cr.execute(
                "SELECT state, next_retry_at, queue_job_uuid "
                "FROM marketing_website_crm_intent WHERE id = %s FOR UPDATE",
                [intent.id],
            )
            row = intent.env.cr.fetchone()
            if not row:
                continue
            state, next_retry_at, _queue_job_uuid = row
            if state not in {"pending", "retry"} or (
                next_retry_at and next_retry_at > now
            ):
                continue
            intent.invalidate_recordset(["state", "next_retry_at", "queue_job_uuid"])
            active_job = intent._active_job()
            if active_job:
                if intent.queue_job_uuid != str(active_job.uuid):
                    intent._internal_write({"queue_job_uuid": str(active_job.uuid)})
                continue
            terminal_job = False
            if intent.queue_job_uuid:
                terminal_job = (
                    self.env["queue.job"]
                    .sudo()
                    .search(
                        [
                            ("uuid", "=", intent.queue_job_uuid),
                            ("identity_key", "=", intent._identity_key()),
                            ("state", "in", ("failed", "cancelled")),
                        ],
                        limit=1,
                    )
                )
            if terminal_job:
                intent._internal_write(
                    {
                        "state": "failed",
                        "next_retry_at": False,
                        "queue_job_uuid": False,
                        "last_error_class": "QueueJobTerminal",
                        "last_error_message": (
                            "Website CRM queue job requires manual retry"
                        ),
                    }
                )
            else:
                enqueueable |= intent
        enqueueable._enqueue()
        return len(enqueueable)

    @api.model
    def _cron_reconcile_sessions(self, now=None, limit=200):
        now = now or fields.Datetime.now()
        limit = min(max(int(limit), 1), 1000)
        due = fair_scheduler_batch(
            self.env,
            self._name,
            [
                ("state", "=", "done"),
                ("session_reconcile_state", "in", ("pending", "watching")),
                "|",
                ("session_reconcile_until", "<", now),
                "&",
                ("session_reconcile_until", ">=", now),
                "|",
                ("next_session_reconcile_at", "=", False),
                ("next_session_reconcile_at", "<=", now),
            ],
            cursor_key="website_crm.session_reconcile",
            limit=limit,
        )
        service = self.env["marketing.website.crm.service"]
        for intent in due:
            scoped_service = service.with_company(intent.company_id).with_context(
                allowed_company_ids=[intent.company_id.id]
            )
            scoped_service._recover_session_intent(
                intent.with_env(scoped_service.env),
                now,
                final=intent.session_reconcile_until < now,
            )
        return len(due)

    @api.model
    def _cron_recover(self, now=None, limit=200):
        return {
            "intents_enqueued": self._cron_enqueue_due(now=now, limit=limit),
            "sessions_reconciled": self._cron_reconcile_sessions(now=now, limit=limit),
        }

    @api.constrains(
        "company_id",
        "website_id",
        "action_id",
        "endpoint_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
    )
    def _check_scope(self):
        for intent in self:
            companies = {
                intent.website_id.company_id,
                intent.action_id.company_id,
                intent.endpoint_id.company_id,
            }
            lead_company = (
                intent.lead_id.marketing_event_company_id or intent.lead_id.company_id
            )
            if lead_company:
                companies.add(lead_company)
            if companies != {intent.company_id}:
                raise ValidationError(_("Website CRM intents cannot cross companies."))
            if (
                intent.action_id.website_id != intent.website_id
                or intent.action_id.form_model_name != "crm.lead"
                or intent.action_id.binding_id.endpoint_id != intent.endpoint_id
                or intent.lead_model != "crm.lead"
                or intent.lead_res_id <= 0
                or not (intent.lead_display_ref or "").strip()
                or (intent.lead_id and intent.lead_id.id != intent.lead_res_id)
            ):
                raise ValidationError(_("The Website CRM intent action is invalid."))

    @api.constrains("session_hash")
    def _check_session_hash(self):
        for intent in self:
            if not re.fullmatch(r"[0-9a-f]{64}", intent.session_hash or ""):
                raise ValidationError(_("The Website session hash is invalid."))

    @api.constrains(
        "state",
        "next_retry_at",
        "queue_job_uuid",
        "correlation_id",
        "processed_at",
    )
    def _check_processing_contract(self):
        for intent in self:
            correlation = intent.correlation_id
            if intent.state == "done":
                valid_projection = (
                    correlation
                    and intent.processed_at
                    and correlation.company_id == intent.company_id
                    and correlation.action_id == intent.action_id
                    and correlation.event_id.event_key_hash
                    == sha256_text(intent.event_ref)
                    and correlation.lead_id == intent.lead_id
                    and correlation.lead_model == intent.lead_model
                    and correlation.lead_res_id == intent.lead_res_id
                    and correlation.lead_display_ref == intent.lead_display_ref
                )
                if not valid_projection:
                    raise ValidationError(
                        _("A completed Website CRM intent needs its exact projection.")
                    )
            elif correlation or intent.processed_at:
                raise ValidationError(
                    _("Only completed Website CRM intents can own a projection.")
                )
            if bool(intent.next_retry_at) != (intent.state == "retry"):
                raise ValidationError(
                    _("Website CRM retry scheduling is inconsistent with its state.")
                )
            if intent.state in {"done", "failed", "tombstoned"} and (
                intent.queue_job_uuid
            ):
                raise ValidationError(
                    _("Terminal Website CRM intents cannot own an active job.")
                )
