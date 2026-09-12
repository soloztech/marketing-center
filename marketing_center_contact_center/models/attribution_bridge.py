from psycopg2 import OperationalError, errors as pg_errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.mapper import (
    MAPPED_TOUCHPOINT_WRITE_FIELDS,
    MAPPING_VERSION,
    ContactCenterAttributionMapper,
)
from ..services.retry import (
    MAX_BRIDGE_RETRIES,
    retry_database_error,
    retry_transient_database,
)
from ..services.tokens import MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN

_LIVE_ATTRIBUTION_JOB_PRIORITY = 40
_BACKFILL_ATTRIBUTION_JOB_PRIORITY = 55
_RETRYABLE_REVISION_CONSTRAINTS = frozenset(
    {
        "marketing_attribution_touchpoint_canonical_revision_unique",
        "marketing_attr_cc_link_source_revision_unique",
        "marketing_attr_cc_link_target_mapping_uniq",
    }
)


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
            "mapping_version_supported",
            "check(mapping_version >= 2)",
            "The mapper version predates the canonical bridge contract.",
        ),
    ]

    def init(self):
        # Every supported mapping derives the Marketing occurrence from the
        # complete Contact Center canonical key. Enforce one source per target
        # revision at the database boundary.
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "marketing_attr_cc_link_target_mapping_uniq "
            "ON marketing_attr_cc_link "
            "(company_id, marketing_touchpoint_id, mapping_version)"
        )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_contact_center_link_write_token")
            is not MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN
        ):
            raise AccessError(_("Attribution links are created only by the bridge."))
        if any(
            values.get("mapping_version") != MAPPING_VERSION for values in vals_list
        ):
            raise ValidationError(
                _("Attribution links must use the current bridge mapper version.")
            )
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
        should_enqueue = bool(set(values) & MAPPED_TOUCHPOINT_WRITE_FIELDS)
        result = super().write(values)
        if should_enqueue and not self.env.context.get(
            "marketing_contact_center_skip_enqueue"
        ):
            self.sudo()._enqueue_marketing_attribution_sync()
        return result

    def _marketing_attribution_identity_key(self, wake_scope="live"):
        self.ensure_one()
        if wake_scope == "live":
            # A fixed per-touchpoint identity can lose a write committed while an
            # older job is already running on its REPEATABLE READ snapshot.  One
            # identity per source transaction coalesces all writes made together
            # while preserving a durable wake-up for every later transaction.
            self.env.cr.execute("SELECT txid_current()")
            wake_ref = "tx:%s" % self.env.cr.fetchone()[0]
        elif wake_scope == "backfill":
            wake_ref = "backfill"
        else:
            raise ValidationError(
                _("The Marketing attribution wake-up scope is invalid.")
            )
        return "marketing_contact_center:touchpoint:%s:v%s:%s" % (
            self.public_ref,
            MAPPING_VERSION,
            wake_ref,
        )

    def _enqueue_marketing_attribution_sync(
        self,
        *,
        priority=_LIVE_ATTRIBUTION_JOB_PRIORITY,
        wake_scope="live",
    ):
        for touchpoint in self.sudo():
            company = touchpoint.company_id
            touchpoint.with_context(allowed_company_ids=[company.id]).with_company(
                company
            ).with_delay(
                identity_key=touchpoint._marketing_attribution_identity_key(wake_scope),
                max_retries=MAX_BRIDGE_RETRIES,
                priority=priority,
                description="Marketing attribution bridge %s" % touchpoint.public_ref,
            )._job_sync_marketing_touchpoint()
        return True

    @retry_transient_database
    def _job_sync_marketing_touchpoint(self):
        self.ensure_one()
        touchpoint = self.sudo().exists()
        if not touchpoint:
            return True
        company = touchpoint.company_id
        try:
            (
                self.env["marketing.contact.center.attribution.service"]
                .sudo()
                .with_context(allowed_company_ids=[company.id])
                .with_company(company)
                ._sync_touchpoint(touchpoint)
            )
        except pg_errors.UniqueViolation as error:
            if (
                getattr(getattr(error, "diag", None), "constraint_name", None)
                not in _RETRYABLE_REVISION_CONSTRAINTS
            ):
                raise
            raise RetryableJobError(
                "Marketing attribution bridge observed a concurrent revision",
                seconds=None,
            ) from None
        except OperationalError as error:
            retry_database_error(
                error,
                "Marketing attribution bridge hit a concurrent database operation",
            )
        return True

    @retry_transient_database
    def _job_sync_marketing_touchpoint_batch(self):
        touchpoints = self.sudo().exists().sorted("id")
        touchpoints._enqueue_marketing_attribution_sync(
            priority=_BACKFILL_ATTRIBUTION_JOB_PRIORITY
        )
        return {"processed": 0, "enqueued": len(touchpoints)}


class ContactCenterAttributionIdentifier(models.Model):
    _inherit = "contact.center.attribution.identifier"

    @api.model_create_multi
    def create(self, vals_list):
        identifiers = super().create(vals_list)
        if not self.env.context.get("marketing_contact_center_skip_enqueue"):
            identifiers.mapped("touchpoint_id")._enqueue_marketing_attribution_sync()
        return identifiers


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
        link_model = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        dto = ContactCenterAttributionMapper.to_dto(source)
        marketing_result = (
            self.env["marketing.attribution.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
            ._ingest_touchpoint(company, dto)
        )
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
        sources = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search(
                [("company_id", "=", company.id), ("id", ">", after_id)],
                order="id asc",
                limit=limit,
            )
        )
        if sources:
            sources._enqueue_marketing_attribution_sync(
                priority=_BACKFILL_ATTRIBUTION_JOB_PRIORITY,
                wake_scope="backfill",
            )
        return {
            "enqueued": len(sources),
            "last_id": sources[-1:].id or after_id,
            "has_more": len(sources) == limit,
        }
