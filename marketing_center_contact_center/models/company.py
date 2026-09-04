from odoo import _, models
from odoo.exceptions import ValidationError

_BACKFILL_PHASES = {
    "attribution": "marketing.contact.center.attribution.service",
    "lifecycle": "marketing.contact.center.lifecycle.service",
    "response_episode": "marketing.contact.center.response.episode.service",
}
_BACKFILL_PAGE_SIZE = 200


class ResCompany(models.Model):
    _inherit = "res.company"

    def _enqueue_marketing_contact_center_backfill(
        self,
        phase,
        after_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Schedule one durable bootstrap page without blocking installation."""

        self.ensure_one()
        if phase not in _BACKFILL_PHASES:
            raise ValidationError(_("The Marketing Contact Center phase is invalid."))
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 1000)
        return self.with_delay(
            identity_key=(
                "marketing_contact_center:bootstrap:%s:company:%s:after:%s"
                % (phase, self.id, after_id)
            ),
            max_retries=0,
            priority=50,
            description=(
                "Marketing Contact Center %s bootstrap after %s" % (phase, after_id)
            ),
        )._job_marketing_contact_center_backfill(phase, after_id, limit)

    def _job_marketing_contact_center_backfill(
        self,
        phase,
        after_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Fan out one bounded page, then durably chain the next cursor."""

        self.ensure_one()
        company = self.sudo().exists()
        if not company:
            return {"done": True, "last_id": max(int(after_id or 0), 0)}
        service_model = _BACKFILL_PHASES.get(phase)
        if not service_model:
            raise ValidationError(_("The Marketing Contact Center phase is invalid."))
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 1000)
        service = (
            self.env[service_model]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        result = service._enqueue_backfill(
            company=company,
            after_id=after_id,
            limit=limit,
        )
        last_id = max(int(result.get("last_id") or after_id), 0)
        has_more = bool(result.get("has_more"))
        if has_more:
            if last_id <= after_id:
                raise ValidationError(
                    _("The Marketing Contact Center backfill cursor did not advance.")
                )
            company._enqueue_marketing_contact_center_backfill(
                phase,
                after_id=last_id,
                limit=limit,
            )
        return {
            "done": not has_more,
            "last_id": last_id,
            "phase": phase,
        }
