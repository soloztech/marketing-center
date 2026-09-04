import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN

REQUEST_KIND_SELECTION = [
    ("public_ingress", "Public web ingress"),
    ("website_form", "Odoo Website form"),
    ("website_whatsapp", "Odoo Website WhatsApp handoff"),
]
REQUEST_KINDS = frozenset(value for value, _label in REQUEST_KIND_SELECTION)
_RETENTION = datetime.timedelta(minutes=5)
_GC_BATCH = 50_000


class MarketingWebIngressAdmission(models.Model):
    """Small, payload-free admission ledger used as an application safety net.

    Counting first and inserting without a shared counter row avoids serializing
    all visitors behind one database lock. A simultaneous burst can exceed the
    configured ceiling by at most the number of concurrent Odoo transactions;
    the reverse proxy remains the authoritative edge limiter.
    """

    _name = "marketing.web.ingress.admission"
    _description = "Marketing Web Ingress Admission"
    _order = "admitted_at desc, id desc"
    _check_company_auto = True

    endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="endpoint_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    request_kind = fields.Selection(
        REQUEST_KIND_SELECTION,
        required=True,
        readonly=True,
        index=True,
    )
    admitted_at = fields.Datetime(required=True, readonly=True, index=True)
    key_revision = fields.Integer(required=True, readonly=True)

    _sql_constraints = [
        (
            "key_revision_positive",
            "check(key_revision > 0)",
            "The admitted key revision must be positive.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS "
            "marketing_web_ingress_admission_window_idx "
            "ON marketing_web_ingress_admission "
            "(endpoint_id, request_kind, admitted_at DESC)"
        )

    @api.model_create_multi
    def create(self, vals_list):
        if not self._internal():
            raise AccessError(_("Web ingress admissions are managed internally."))
        return super().create(vals_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("Web ingress admissions are immutable."))

    def unlink(self):
        if not self._internal():
            raise AccessError(_("Web ingress admissions are managed internally."))
        return super().unlink()

    @api.model
    def _internal(self):
        return (
            self.env.context.get("marketing_web_ingress_internal")
            is WEB_INGRESS_INTERNAL_TOKEN
        )

    @api.model
    def _admit(self, endpoint, request_kind, now=None):
        if (
            not endpoint
            or len(endpoint) != 1
            or getattr(endpoint, "_name", "") != "marketing.web.ingress.endpoint"
        ):
            raise ValidationError(_("A single web ingress endpoint is required."))
        endpoint = (
            self.env["marketing.web.ingress.endpoint"].browse(endpoint.id).exists()
        )
        if not endpoint:
            raise ValidationError(_("A single web ingress endpoint is required."))
        # Make this primitive safe for internal adapters too instead of
        # depending on every caller to remember a configuration fence.  The
        # public controllers already hold the same compatible row lock.
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint " "WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("A single web ingress endpoint is required."))
        endpoint.invalidate_recordset(
            ["active", "company_id", "key_revision", "rate_limit_per_minute"]
        )
        if endpoint.company_id not in self.env.companies:
            raise AccessError(_("The web ingress endpoint belongs to another company."))
        if not endpoint.active:
            raise AccessError(_("The web ingress endpoint is inactive."))
        if request_kind not in REQUEST_KINDS:
            raise ValidationError(_("The web ingress request kind is invalid."))
        now = now or fields.Datetime.now()
        if not isinstance(now, datetime.datetime):
            raise ValidationError(_("The web ingress admission time is invalid."))
        if now.tzinfo:
            now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        now = now.replace(microsecond=0)
        cutoff = now - datetime.timedelta(minutes=1)
        limit = endpoint.rate_limit_per_minute
        self.env.cr.execute(
            """
            SELECT count(*)
              FROM (
                    SELECT id
                      FROM marketing_web_ingress_admission
                     WHERE endpoint_id = %s
                       AND request_kind = %s
                       AND admitted_at > %s
                     LIMIT %s
                   ) AS recent
            """,
            [endpoint.id, request_kind, cutoff, limit],
        )
        if self.env.cr.fetchone()[0] >= limit:
            return False
        self.sudo().with_context(
            allowed_company_ids=[endpoint.company_id.id],
            marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN,
        ).with_company(endpoint.company_id).create(
            {
                "endpoint_id": endpoint.id,
                "request_kind": request_kind,
                "admitted_at": now,
                "key_revision": endpoint.key_revision,
            }
        )
        return True

    @api.autovacuum
    def _gc_old_admissions(self):
        cutoff = fields.Datetime.now() - _RETENTION
        self.env.cr.execute(
            """
            DELETE FROM marketing_web_ingress_admission
             WHERE id IN (
                    SELECT id
                      FROM marketing_web_ingress_admission
                     WHERE admitted_at < %s
                     ORDER BY id
                     LIMIT %s
             )
            """,
            [cutoff, _GC_BATCH],
        )
