from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.mapper import MAPPING_VERSION, ContactCenterAttributionMapper
from ..services.tokens import MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN


class MarketingAttributionContactCenterLink(models.Model):
    _name = "marketing.attribution.contact.center.link"
    _table = "marketing_attr_cc_link"
    _description = "Marketing Attribution Contact Center Link"
    _order = "synced_at desc, id desc"
    _rec_name = "source_public_ref"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    source_touchpoint_id = fields.Many2one(
        "contact.center.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    marketing_touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    source_public_ref = fields.Char(required=True, size=36, index=True, readonly=True)
    source_content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    mapping_version = fields.Integer(required=True, readonly=True)
    synced_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "source_revision_unique",
            "unique(company_id, source_touchpoint_id, source_content_hash, mapping_version)",
            "This Contact Center attribution revision is already linked.",
        ),
        (
            "source_hash_sha256",
            "check(char_length(source_content_hash) = 64)",
            "The source content hash must be a SHA-256 digest.",
        ),
        (
            "mapping_version_positive",
            "check(mapping_version > 0)",
            "The mapper version must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_contact_center_link_write_token")
            is not MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN
        ):
            raise AccessError(_("Attribution links are created only by the bridge."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Attribution links cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Attribution links cannot be deleted."))

    @api.constrains("company_id", "source_touchpoint_id", "marketing_touchpoint_id")
    def _check_company_scope(self):
        for link in self:
            if (
                link.source_touchpoint_id.company_id != link.company_id
                or link.marketing_touchpoint_id.company_id != link.company_id
            ):
                raise ValidationError(
                    _("The attribution link cannot cross company boundaries.")
                )


class ContactCenterAttributionTouchpoint(models.Model):
    _inherit = "contact.center.attribution.touchpoint"

    @api.model_create_multi
    def create(self, vals_list):
        touchpoints = super().create(vals_list)
        if not self.env.context.get("marketing_contact_center_skip_enqueue"):
            touchpoints._enqueue_marketing_attribution_sync()
        return touchpoints

    def write(self, values):
        result = super().write(values)
        if not self.env.context.get("marketing_contact_center_skip_enqueue"):
            self._enqueue_marketing_attribution_sync()
        return result

    def _enqueue_marketing_attribution_sync(self):
        for touchpoint in self.sudo():
            touchpoint.with_company(touchpoint.company_id).with_delay(
                identity_key="marketing_contact_center:touchpoint:%s"
                % touchpoint.public_ref,
                max_retries=0,
                priority=40,
                description="Marketing attribution bridge %s" % touchpoint.public_ref,
            )._job_sync_marketing_touchpoint()
        return True

    def _job_sync_marketing_touchpoint(self):
        self.ensure_one()
        touchpoint = self.sudo().exists()
        if not touchpoint:
            return True
        (
            self.env["marketing.contact.center.attribution.service"]
            .sudo()
            .with_company(touchpoint.company_id)
            ._sync_touchpoint(touchpoint)
        )
        return True


class MarketingContactCenterAttributionService(models.AbstractModel):
    _name = "marketing.contact.center.attribution.service"
    _description = "Marketing Contact Center Attribution Bridge Service"

    @api.model
    def _sync_touchpoint(self, source):
        source = source.sudo().exists()
        if (
            not source
            or source._name != "contact.center.attribution.touchpoint"
            or len(source) != 1
        ):
            raise ValidationError(_("A single Contact Center touchpoint is required."))
        company = source.company_id
        dto = ContactCenterAttributionMapper.to_dto(source)
        marketing_result = (
            self.env["marketing.attribution.service"]
            .sudo()
            .with_company(company)
            ._ingest_touchpoint(company, dto)
        )
        link_model = self.env["marketing.attribution.contact.center.link"].sudo()
        existing = link_model.search(
            [
                ("company_id", "=", company.id),
                ("source_touchpoint_id", "=", source.id),
                ("source_content_hash", "=", marketing_result.content_hash),
                ("mapping_version", "=", MAPPING_VERSION),
            ],
            limit=1,
        )
        if existing:
            return existing
        marketing_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            marketing_result.touchpoint_id
        )
        internal_context = {
            "marketing_contact_center_link_write_token": (
                MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN
            )
        }
        return (
            link_model.with_company(company)
            .with_context(**internal_context)
            .create(
                {
                    "company_id": company.id,
                    "source_touchpoint_id": source.id,
                    "marketing_touchpoint_id": marketing_touchpoint.id,
                    "source_public_ref": source.public_ref,
                    "source_content_hash": marketing_result.content_hash,
                    "mapping_version": MAPPING_VERSION,
                }
            )
        )

    @api.model
    def _enqueue_backfill(self, company=None, after_id=0, limit=200):
        company = company or self.env.company
        if (
            company._name != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The backfill company is not available."))
        limit = min(max(int(limit), 1), 1000)
        after_id = max(int(after_id), 0)
        sources = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search(
                [("company_id", "=", company.id), ("id", ">", after_id)],
                order="id asc",
                limit=limit,
            )
        )
        sources._enqueue_marketing_attribution_sync()
        return {
            "enqueued": len(sources),
            "last_id": sources[-1:].id or after_id,
            "has_more": len(sources) == limit,
        }
