# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
import uuid

from odoo import _, fields, models


class Projection(models.Model):
    _inherit = "marketing.center.meta.crm.projection"

    admission_signals_json = fields.Json(readonly=True, copy=False)

    def _admission_signal(self, lead=None):
        self.ensure_one()
        purpose = (
            "technical"
            if self.technical_hold_reason
            else "review"
            if self.state == "review"
            else self.admission_decision
            if self.admission_decision == "reused"
            else "unverified"
        )
        signals = dict(self.admission_signals_json or {})
        if purpose in signals:
            return False
        service = self.env["marketing.crm.service"]
        reviewer = service._crm_admission_reviewer(self.company_id)
        if lead is None and len(self.review_candidate_ids) == 1:
            lead = self.review_candidate_ids
        recipient = self.env["res.users"].browse()
        if lead:
            for candidate in (lead.sudo().user_id, reviewer):
                if service._crm_admission_can_read(candidate, lead):
                    recipient = candidate
                    break
        activity = self.env["mail.activity"].browse()
        message = self.env["mail.message"].browse()
        if recipient:
            activity = (
                self.env["mail.activity"]
                .sudo()
                .with_context(mail_activity_quick_update=True)
                .create(
                    {
                        "res_model_id": self.env["ir.model"]._get_id("crm.lead"),
                        "res_id": lead.id,
                        "activity_type_id": self.env.ref(
                            "mail.mail_activity_data_todo"
                        ).id,
                        "summary": _("Nova entrada Meta; revisar atendimento"),
                        "note": _(
                            "Revise o atendimento e, quando indicado, a identidade ou "
                            "o vínculo comercial. A conversa e as evidências "
                            "permanecem disponíveis com as permissões atuais."
                        ),
                        "user_id": recipient.id,
                        "date_deadline": fields.Date.today(),
                    }
                )
            )
        else:
            message = service._crm_admission_inbox(self.company_id)
        if not activity and not message:
            return False
        signals[purpose] = {
            "ref": str(uuid.uuid4()),
            "activity_id": activity.id or False,
            "message_id": message.id or False,
        }
        self._internal_write(
            {
                "admission_signals_json": signals,
                "signal_activity_id": activity.id or False,
                "signal_message_id": message.id or False,
            }
        )
        return True


class Touchpoint(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    def _erase_private_values(self, *, token, now):
        result = super()._erase_private_values(token=token, now=now)
        for point in self:
            projections = (
                self.env["marketing.center.meta.crm.projection"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", point.company_id.id),
                        (
                            "submission_id.touchpoint_id.canonical_key",
                            "=",
                            point.canonical_key,
                        ),
                        ("comparison_erased_at", "=", False),
                    ]
                )
            )
            projections._internal_write(
                {
                    "comparison_exact": False,
                    "comparison_variant": False,
                    "comparison_erased_at": now,
                }
            )
        return result
