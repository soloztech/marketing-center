import uuid

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from .tokens import MARKETING_META_CRM_WRITE_TOKEN

_ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")


def _internal(recordset):
    return (
        recordset.env.context.get("marketing_meta_crm_write_token")
        is MARKETING_META_CRM_WRITE_TOKEN
    )


class MarketingCenterMetaCrmProjection(models.Model):
    _name = "marketing.center.meta.crm.projection"
    _description = "Meta Lead Ads CRM Projection"
    _order = "id desc"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=lambda self: str(uuid.uuid4()),
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    submission_id = fields.Many2one(
        "marketing.center.meta.lead.submission",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
        readonly=True,
    )
    route_id = fields.Many2one(
        "marketing.center.meta.lead.route",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
        readonly=True,
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
        readonly=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("skipped", "Disabled"),
        ],
        required=True,
        default="pending",
        readonly=True,
        index=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        check_company=True,
        ondelete="set null",
        readonly=True,
    )
    lead_model = fields.Char(readonly=True, size=64)
    lead_res_id = fields.Integer(readonly=True, index=True)
    lead_display_ref = fields.Char(readonly=True, size=256)
    assertion_id = fields.Many2one(
        "marketing.attribution.crm.link",
        index=True,
        check_company=True,
        ondelete="restrict",
        readonly=True,
    )
    attempts = fields.Integer(required=True, default=0, readonly=True)
    queue_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)
    processed_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta CRM projection reference must be unique.",
        ),
        (
            "submission_unique",
            "unique(submission_id)",
            "The Meta lead submission already has a CRM projection.",
        ),
        (
            "attempts_nonnegative",
            "check(attempts >= 0)",
            "The Meta CRM projection attempt count is invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta CRM projections are created internally."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Meta CRM projections are managed internally."))
        mutable = {
            "state",
            "lead_id",
            "lead_model",
            "lead_res_id",
            "lead_display_ref",
            "assertion_id",
            "attempts",
            "queue_job_uuid",
            "processed_at",
            "last_error_class",
            "last_error_message",
        }
        if set(values) - mutable:
            raise AccessError(_("Meta CRM projection identity is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta CRM projections cannot be deleted."))

    @api.constrains(
        "company_id",
        "submission_id",
        "route_id",
        "touchpoint_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
        "assertion_id",
        "state",
    )
    def _check_scope(self):
        for projection in self:
            assertion = projection.assertion_id
            completed = bool(assertion)
            has_snapshot = bool(
                projection.lead_model == "crm.lead"
                and projection.lead_res_id > 0
                and (projection.lead_display_ref or "").strip()
            )
            if (
                projection.submission_id.company_id != projection.company_id
                or projection.route_id != projection.submission_id.route_id
                or projection.route_id.company_id != projection.company_id
                or projection.touchpoint_id != projection.submission_id.touchpoint_id
                or projection.touchpoint_id.company_id != projection.company_id
                or (
                    projection.lead_id.company_id
                    and projection.lead_id.company_id != projection.company_id
                )
                or (projection.state == "done") != completed
                or completed != has_snapshot
                or (
                    projection.lead_id
                    and projection.lead_id.id != projection.lead_res_id
                )
                or (
                    assertion
                    and (
                        assertion.company_id != projection.company_id
                        or assertion.touchpoint_id != projection.touchpoint_id
                        or assertion.lead_model != projection.lead_model
                        or assertion.lead_res_id != projection.lead_res_id
                        or assertion.lead_display_ref != projection.lead_display_ref
                        or (
                            projection.lead_id
                            and assertion.lead_id != projection.lead_id
                        )
                    )
                )
            ):
                raise ValidationError(
                    _(
                        "The Meta submission, touchpoint, CRM lead and assertion must "
                        "share one complete scope."
                    )
                )

    def _internal_write(self, values):
        return self.with_context(
            marketing_meta_crm_write_token=MARKETING_META_CRM_WRITE_TOKEN
        ).write(values)

    def _identity_key(self):
        self.ensure_one()
        return "marketing_meta_crm:projection:%s" % self.id

    def _active_job(self):
        self.ensure_one()
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", self._identity_key()),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                limit=1,
            )
        )

    def _enqueue(self, retry_terminal=False):
        for projection in self.sudo().exists().sorted("id"):
            if projection.state == "done":
                continue
            if not projection.route_id.crm_auto_create_lead:
                projection._internal_write(
                    {
                        "state": "skipped",
                        "queue_job_uuid": False,
                        "last_error_class": False,
                        "last_error_message": False,
                    }
                )
                continue
            if projection.state in {"failed", "skipped"} and not retry_terminal:
                continue
            active = projection._active_job()
            if active:
                projection._internal_write({"queue_job_uuid": active.uuid})
                continue
            projection._internal_write(
                {
                    "state": "pending",
                    "queue_job_uuid": False,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            delayed = projection.with_delay(
                identity_key=projection._identity_key(),
                max_retries=8,
                priority=32,
                description="Project Meta lead %s to CRM" % projection.public_ref,
            )._job_project_to_crm()
            projection._internal_write({"queue_job_uuid": delayed.uuid})
        return True

    def _job_project_to_crm(self):
        self.ensure_one()
        projection = self.sudo().exists()
        if not projection or projection.state == "done":
            return True
        attempt = projection._claim_current_job()
        if not attempt:
            return False
        projection._internal_write(
            {
                "state": "processing",
                "attempts": attempt,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        try:
            with self.env.cr.savepoint():
                return self.env["marketing.center.meta.crm.service"]._project(
                    projection
                )
        except OperationalError:
            if projection._queue_job_attempt_is_terminal():
                projection._internal_write(
                    {
                        "state": "failed",
                        "queue_job_uuid": False,
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "ConcurrentDatabaseRetryLimit",
                        "last_error_message": (
                            "CRM projection exhausted its bounded concurrency "
                            "retry policy."
                        ),
                    }
                )
                return False
            raise RetryableJobError(
                "Meta CRM projection hit a concurrent database operation"
            ) from None
        except (AccessError, UserError, ValidationError) as error:
            projection._internal_write(
                {
                    "state": "failed",
                    "queue_job_uuid": False,
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": type(error).__name__[:128],
                    "last_error_message": (
                        "CRM rejected the configured projection. Review the Lead "
                        "Ads route settings."
                    ),
                }
            )
            return False

    def _claim_current_job(self):
        """Fence projection side effects to the exact persisted OCA job."""

        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not isinstance(job_uuid, str) or not job_uuid:
            return 0
        self.flush_recordset(["state", "attempts", "queue_job_uuid"])
        self.env.cr.execute(
            "SELECT state, attempts, queue_job_uuid "
            "FROM marketing_center_meta_crm_projection "
            "WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        self.invalidate_recordset(["state", "attempts", "queue_job_uuid"])
        if not row or row[0] not in {"pending", "processing"} or row[2] != job_uuid:
            return 0
        job = self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        return max(int(row[1] or 0) + 1, (job.retry + 1) if job else 1)

    def _queue_job_attempt_is_terminal(self):
        """Return whether the exact running projection job exhausted retries."""

        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not job_uuid:
            return False
        job = (
            self.env["queue.job"].sudo().search([("uuid", "=", str(job_uuid))], limit=1)
        )
        if (
            not job
            or job.state != "started"
            or job.model_name != self._name
            or job.method_name != "_job_project_to_crm"
            or not job.max_retries
            or job.retry + 1 < job.max_retries
        ):
            return False
        records = job.records
        return bool(
            records
            and getattr(records, "_name", "") == self._name
            and records.exists().ids == self.ids
        )

    def action_retry(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can retry CRM projection.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.filtered(lambda item: item.state in {"failed", "skipped"})._enqueue(
            retry_terminal=True
        )
        return True

    def action_open_lead(self):
        self.ensure_one()
        if not self.lead_id:
            return False
        self.lead_id.check_access_rights("read")
        self.lead_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.lead_id.display_name,
            "res_model": "crm.lead",
            "res_id": self.lead_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }


class MarketingCenterMetaLeadSubmission(models.Model):
    _inherit = "marketing.center.meta.lead.submission"

    crm_projection_id = fields.Many2one(
        "marketing.center.meta.crm.projection",
        compute="_compute_crm_projection_id",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    crm_lead_id = fields.Many2one(
        "crm.lead",
        compute="_compute_crm_projection_id",
        groups="marketing_center_base.group_marketing_center_admin",
    )

    @api.depends_context("uid")
    def _compute_crm_projection_id(self):
        projections = (
            self.env["marketing.center.meta.crm.projection"]
            .sudo()
            .search([("submission_id", "in", self.ids)])
        )
        by_submission = {item.submission_id.id: item for item in projections}
        for submission in self:
            projection = by_submission.get(submission.id) or projections.browse()
            submission.crm_projection_id = projection
            submission.crm_lead_id = projection.lead_id if projection else False

    def write(self, values):
        result = super().write(values)
        if {"state", "touchpoint_id"}.intersection(values):
            ready = self.filtered(
                lambda submission: submission.state == "ingested"
                and submission.touchpoint_id
                and submission.route_id.crm_auto_create_lead
            )
            self.env["marketing.center.meta.crm.service"].sudo()._ensure_and_enqueue(
                ready
            )
        return result

    def action_open_crm_lead(self):
        self.ensure_one()
        return self.crm_projection_id.action_open_lead()
