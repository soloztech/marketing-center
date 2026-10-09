"""Internal-only delivery and effective backlog monitoring."""

import datetime
import logging

from psycopg2.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .dedup_policy import DEDUP_GROUPS

_logger = logging.getLogger(__name__)


class AdmissionSignals(models.AbstractModel):
    _inherit = "marketing.crm.service"

    @api.model
    def _crm_admission_reviewer(self, company):
        user = company.sudo().crm_cross_source_reviewer_id
        if not (
            user
            and user.active
            and not user.share
            and company in user.company_ids
            and user.has_group("sales_team.group_sale_manager")
        ):
            return self.env["res.users"].browse()
        return user

    @api.model
    def _crm_admission_can_read(self, user, lead):
        if not (
            user
            and user.active
            and not user.share
            and lead.company_id in user.company_ids
        ):
            return False
        native = lead.with_user(user).with_context(
            allowed_company_ids=lead.company_id.ids, active_test=False
        )
        try:
            native.check_access_rights("read")
            native.check_access_rule("read")
        except AccessError:
            return False
        return True

    @api.model
    def _crm_admission_inbox(self, company, body=None):
        reviewer = self._crm_admission_reviewer(company)
        if not reviewer:
            reviewer = (
                self.env["res.users"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("share", "=", False),
                        ("company_ids", "in", company.ids),
                    ]
                )
                .filtered(
                    lambda user: all(
                        self.env.ref(group, raise_if_not_found=False)
                        and user.has_group(group)
                        for group in DEDUP_GROUPS
                    )
                )
            )
            _logger.warning(
                "CRM admission reviewer unavailable for company %s; "
                "administrator recipients: %s",
                company.id,
                len(reviewer),
            )
            if not reviewer:
                return self.env["mail.message"].browse()
        message = (
            self.env["mail.message"]
            .sudo()
            .create(
                {
                    "message_type": "user_notification",
                    "subject": _("Entradas CRM aguardando revisão"),
                    "body": body
                    or _(
                        "Há uma entrada comercial aguardando revisão. Abra CRM → "
                        "Entradas a revisar."
                    ),
                    "author_id": self.env.user.partner_id.id,
                    "partner_ids": [(6, 0, reviewer.partner_id.ids)],
                }
            )
        )
        self.env["mail.thread"]._notify_thread_by_inbox(
            message,
            [
                {
                    "id": user.partner_id.id,
                    "active": True,
                    "share": False,
                    "notif": "inbox",
                    "type": "user",
                    "groups": user.groups_id.ids,
                }
                for user in reviewer
            ],
        )
        return message

    @api.model
    def _crm_admission_monitor(self):
        """Monotonic incident IDs; daily reminders and same-day new incidents."""
        Company = self.env["res.company"].sudo()
        Projection = (
            self.env["marketing.center.meta.crm.projection"].sudo()
            if "marketing.center.meta.crm.projection" in self.env.registry
            else False
        )
        companies = Company.search([("crm_cross_source_enabled_at", "!=", False)])
        now = fields.Datetime.now()
        for company in companies:
            try:
                with self.env.cr.savepoint():
                    self._crm_admission_monitor_company(company, Projection, now)
            except (DeadlockDetected, LockNotAvailable, SerializationFailure):
                _logger.info(
                    "CRM admission monitor deferred busy company %s", company.id
                )
        return True

    @api.model
    def _crm_admission_monitor_company(self, company, Projection, now):
        monitor = self.env["marketing.crm.admission.monitor"]._locked_company(company)
        if not monitor:
            return False
        data = dict(monitor.data_json or {})
        data["reviewer_unavailable"] = not bool(self._crm_admission_reviewer(company))
        counts = self._crm_cross_source_intake_counts(company)
        metrics = {"intake_review": counts}
        if Projection is not False:
            for kind, extra in (
                (
                    "meta_review",
                    [
                        ("state", "=", "review"),
                        ("id", ">", company.crm_cross_source_projection_watermark),
                        ("create_date", ">=", company.crm_cross_source_enabled_at),
                    ],
                ),
                (
                    "technical_hold",
                    [
                        ("state", "=", "pending"),
                        ("technical_hold_reason", "!=", False),
                    ],
                ),
            ):
                domain = [("company_id", "=", company.id)] + extra
                oldest = Projection.search(domain, order="create_date", limit=1)
                metrics[kind] = {
                    "count": Projection.search_count(domain),
                    "oldest": oldest.create_date if oldest else False,
                }
        for kind, metric in metrics.items():
            previous = dict(data.get(kind) or {})
            count, oldest = metric["count"], metric["oldest"]
            alarming = bool(
                count
                and (
                    kind == "technical_hold"
                    or count >= 10
                    or (oldest and now - oldest >= datetime.timedelta(hours=24))
                )
            )
            incident = int(previous.get("incident", 0)) + int(
                alarming and not previous.get("active")
            )
            key = "%s:%s:%s:%s" % (
                company.id,
                kind,
                incident,
                now.date().isoformat(),
            )
            alerted = previous.get("alert_key")
            if alarming and alerted != key:
                message = self._crm_admission_inbox(
                    company,
                    _(
                        "A fila de entradas CRM precisa de atenção. Abra CRM → "
                        "Entradas a revisar."
                    ),
                )
                if message:
                    alerted = key
            data[kind] = {
                "active": alarming,
                "incident": incident,
                "alert_key": alerted,
                "count": count,
                "oldest": fields.Datetime.to_string(oldest) if oldest else False,
            }
        if data != (monitor.data_json or {}):
            monitor.write({"data_json": data})
            company.invalidate_recordset(["crm_cross_source_monitor_json"])
        return True
