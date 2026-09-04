from odoo import _, models
from odoo.exceptions import ValidationError

_BACKFILL_PAGE_SIZE = 200


class ResCompany(models.Model):
    _inherit = "res.company"

    def _enqueue_marketing_meta_crm_backfill(
        self,
        after_submission_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Schedule one durable Meta CRM convergence page for this company."""

        self.ensure_one()
        after_submission_id = max(int(after_submission_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 1000)
        return self.with_delay(
            identity_key=(
                "marketing_meta_crm:bootstrap:company:%s:after:%s"
                % (self.id, after_submission_id)
            ),
            max_retries=0,
            priority=50,
            description="Marketing Meta CRM bootstrap after %s" % after_submission_id,
        )._job_marketing_meta_crm_backfill(after_submission_id, limit)

    def _job_marketing_meta_crm_backfill(
        self,
        after_submission_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Reconcile one bounded page and durably chain its next cursor."""

        self.ensure_one()
        company = self.sudo().exists()
        after_submission_id = max(int(after_submission_id or 0), 0)
        if not company:
            return {
                "done": True,
                "last_submission_id": after_submission_id,
            }
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 1000)
        service = (
            self.env["marketing.center.meta.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        result = service._enqueue_backfill(
            company=company,
            after_submission_id=after_submission_id,
            limit=limit,
        )
        last_id = max(
            int(result.get("last_submission_id") or after_submission_id),
            0,
        )
        has_more = bool(result.get("has_more"))
        if has_more:
            if last_id <= after_submission_id:
                raise ValidationError(
                    _("The Meta CRM backfill cursor did not advance.")
                )
            company._enqueue_marketing_meta_crm_backfill(
                after_submission_id=last_id,
                limit=limit,
            )
        return {
            "done": not has_more,
            "last_submission_id": last_id,
        }
