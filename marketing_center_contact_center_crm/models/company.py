from odoo import _, models
from odoo.exceptions import ValidationError

_BACKFILL_PAGE_SIZE = 50
_CONVERGENCE_PAGE_SIZE = 100


class ResCompany(models.Model):
    _inherit = "res.company"

    def _enqueue_marketing_contact_center_crm_backfill(
        self,
        after_case_link_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Schedule one durable convergence page for this company."""

        self.ensure_one()
        after_case_link_id = max(int(after_case_link_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 200)
        return self.with_delay(
            identity_key=(
                "marketing_contact_center_crm:bootstrap:company:%s:after:%s"
                % (self.id, after_case_link_id)
            ),
            max_retries=0,
            priority=50,
            description=(
                "Marketing Contact Center CRM bootstrap after %s" % after_case_link_id
            ),
        )._job_marketing_contact_center_crm_backfill(after_case_link_id, limit)

    def _job_marketing_contact_center_crm_backfill(
        self,
        after_case_link_id=0,
        limit=_BACKFILL_PAGE_SIZE,
    ):
        """Reconcile a bounded page and chain only a monotonic next cursor."""

        self.ensure_one()
        company = self.sudo().exists()
        if not company:
            return {
                "done": True,
                "last_case_link_id": max(int(after_case_link_id or 0), 0),
            }
        after_case_link_id = max(int(after_case_link_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 200)
        service = (
            self.env["marketing.contact.center.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        result = service._reconcile_existing(
            company=company,
            after_case_link_id=after_case_link_id,
            limit=limit,
        )
        last_id = max(
            int(result.get("last_case_link_id") or after_case_link_id),
            0,
        )
        has_more = bool(result.get("has_more"))
        if has_more:
            if last_id <= after_case_link_id:
                raise ValidationError(
                    _("The Contact Center CRM backfill cursor did not advance.")
                )
            company._enqueue_marketing_contact_center_crm_backfill(
                after_case_link_id=last_id,
                limit=limit,
            )
        return {"done": not has_more, "last_case_link_id": last_id}

    def _enqueue_marketing_contact_center_crm_case_link(
        self,
        case_link_id,
        after_attribution_link_id=0,
        limit=_CONVERGENCE_PAGE_SIZE,
    ):
        """Schedule one bounded side of a case/touchpoint cross-product."""

        self.ensure_one()
        case_link_id = max(int(case_link_id or 0), 0)
        after_attribution_link_id = max(int(after_attribution_link_id or 0), 0)
        limit = min(max(int(limit or _CONVERGENCE_PAGE_SIZE), 1), 500)
        return self.with_delay(
            identity_key=(
                "marketing_contact_center_crm:case:%s:after_attribution:%s"
                % (case_link_id, after_attribution_link_id)
            ),
            max_retries=0,
            priority=40,
            description=(
                "Marketing CRM convergence for Contact Center case link %s after %s"
                % (case_link_id, after_attribution_link_id)
            ),
        )._job_marketing_contact_center_crm_case_link(
            case_link_id,
            after_attribution_link_id,
            limit,
        )

    def _job_marketing_contact_center_crm_case_link(
        self,
        case_link_id,
        after_attribution_link_id=0,
        limit=_CONVERGENCE_PAGE_SIZE,
    ):
        """Link one case to at most ``limit`` attribution projections."""

        self.ensure_one()
        company = self.sudo().exists()
        case_link_id = max(int(case_link_id or 0), 0)
        after_attribution_link_id = max(int(after_attribution_link_id or 0), 0)
        limit = min(max(int(limit or _CONVERGENCE_PAGE_SIZE), 1), 500)
        if not company or not case_link_id:
            return {"done": True, "processed": 0}
        service = (
            self.env["marketing.contact.center.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        case_link = service._active_case_link(company, case_link_id)
        if not case_link:
            return {"done": True, "processed": 0}
        attribution_links = service._attribution_link_page(
            case_link.channel_id,
            after_id=after_attribution_link_id,
            limit=limit,
        )
        linked = service._reconcile_channel(
            case_link.channel_id,
            case_links=case_link,
            attribution_links=attribution_links,
        )
        last_id = attribution_links[-1:].id or after_attribution_link_id
        has_more = len(attribution_links) == limit
        if has_more:
            company._enqueue_marketing_contact_center_crm_case_link(
                case_link.id,
                after_attribution_link_id=last_id,
                limit=limit,
            )
        return {
            "done": not has_more,
            "processed": len(attribution_links),
            "linked_pairs": len(linked),
            "last_attribution_link_id": last_id,
        }

    def _enqueue_marketing_contact_center_crm_attribution_link(
        self,
        attribution_link_id,
        after_case_link_id=0,
        limit=_CONVERGENCE_PAGE_SIZE,
    ):
        """Schedule the inverse bounded side of the convergence graph."""

        self.ensure_one()
        attribution_link_id = max(int(attribution_link_id or 0), 0)
        after_case_link_id = max(int(after_case_link_id or 0), 0)
        limit = min(max(int(limit or _CONVERGENCE_PAGE_SIZE), 1), 500)
        return self.with_delay(
            identity_key=(
                "marketing_contact_center_crm:attribution:%s:after_case:%s"
                % (attribution_link_id, after_case_link_id)
            ),
            max_retries=0,
            priority=40,
            description=(
                "Marketing CRM convergence for attribution link %s after %s"
                % (attribution_link_id, after_case_link_id)
            ),
        )._job_marketing_contact_center_crm_attribution_link(
            attribution_link_id,
            after_case_link_id,
            limit,
        )

    def _job_marketing_contact_center_crm_attribution_link(
        self,
        attribution_link_id,
        after_case_link_id=0,
        limit=_CONVERGENCE_PAGE_SIZE,
    ):
        """Link one attribution projection to at most ``limit`` cases."""

        self.ensure_one()
        company = self.sudo().exists()
        attribution_link_id = max(int(attribution_link_id or 0), 0)
        after_case_link_id = max(int(after_case_link_id or 0), 0)
        limit = min(max(int(limit or _CONVERGENCE_PAGE_SIZE), 1), 500)
        if not company or not attribution_link_id:
            return {"done": True, "processed": 0}
        service = (
            self.env["marketing.contact.center.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        attribution_link = service._current_attribution_link(
            company,
            attribution_link_id,
        )
        if not attribution_link:
            return {"done": True, "processed": 0}
        channel = attribution_link.source_touchpoint_id.channel_binding_id.channel_id
        case_links = service._case_link_page(
            channel,
            after_id=after_case_link_id,
            limit=limit,
        )
        linked = service._reconcile_channel(
            channel,
            case_links=case_links,
            attribution_links=attribution_link,
        )
        last_id = case_links[-1:].id or after_case_link_id
        has_more = len(case_links) == limit
        if has_more:
            company._enqueue_marketing_contact_center_crm_attribution_link(
                attribution_link.id,
                after_case_link_id=last_id,
                limit=limit,
            )
        return {
            "done": not has_more,
            "processed": len(case_links),
            "linked_pairs": len(linked),
            "last_case_link_id": last_id,
        }
