import hashlib

from odoo import _, models
from odoo.exceptions import AccessError, ValidationError

_BACKFILL_PAGE_SIZE = 200


class ResCompany(models.Model):
    _inherit = "res.company"

    def _crm_backfill_route_ids(self, route_ids):
        self.ensure_one()
        if route_ids is None:
            return None
        if (
            not isinstance(route_ids, (tuple, list))
            or not route_ids
            or any(
                not isinstance(route_id, int)
                or isinstance(route_id, bool)
                or route_id <= 0
                for route_id in route_ids
            )
            or tuple(route_ids) != tuple(sorted(set(route_ids)))
        ):
            raise ValidationError(_("The Meta CRM route scope must contain sorted IDs."))
        # OCA persists kwargs as JSON, which reloads tuples as lists.
        route_ids = tuple(route_ids)
        routes = (
            self.env["marketing.center.meta.lead.route"]
            .sudo()
            .with_context(active_test=False)
            .browse(route_ids)
            .exists()
        )
        if len(routes) != len(route_ids) or any(
            route.company_id != self for route in routes
        ):
            raise AccessError(_("The Meta CRM routes must belong to the backfill company."))
        return route_ids

    def _enqueue_marketing_meta_crm_backfill(
        self,
        after_submission_id=0,
        limit=_BACKFILL_PAGE_SIZE,
        route_ids=None,
    ):
        """Schedule one durable Meta CRM convergence page for this company."""

        self.ensure_one()
        after_submission_id = max(int(after_submission_id or 0), 0)
        limit = min(max(int(limit or _BACKFILL_PAGE_SIZE), 1), 1000)
        route_ids = self._crm_backfill_route_ids(route_ids)
        identity_key = "marketing_meta_crm:bootstrap:company:%s:after:%s" % (
            self.id, after_submission_id
        )
        job_kwargs = {}
        if route_ids is not None:
            scope = ",".join(str(route_id) for route_id in route_ids)
            identity_key += ":routes:%s" % hashlib.sha256(scope.encode()).hexdigest()
            job_kwargs["route_ids"] = route_ids
        return self.with_delay(
            identity_key=identity_key,
            max_retries=0,
            priority=50,
            description="Marketing Meta CRM bootstrap after %s" % after_submission_id,
        )._job_marketing_meta_crm_backfill(after_submission_id, limit, **job_kwargs)

    def _job_marketing_meta_crm_backfill(
        self,
        after_submission_id=0,
        limit=_BACKFILL_PAGE_SIZE,
        route_ids=None,
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
        route_ids = company._crm_backfill_route_ids(route_ids)
        scope_kwargs = {"route_ids": route_ids} if route_ids is not None else {}
        result = service._enqueue_backfill(
            company=company,
            after_submission_id=after_submission_id,
            limit=limit,
            **scope_kwargs,
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
                **scope_kwargs,
            )
        return {
            "done": not has_more,
            "last_submission_id": last_id,
        }
