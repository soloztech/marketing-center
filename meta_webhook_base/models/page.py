import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services import (
    META_API_RUNTIME_CONTEXT_KEY,
    META_API_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.credentials import (
    MetaCredentialResolutionError,
    resolve_secret,
    validate_secret_reference,
)

from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN, META_WEBHOOK_RUNTIME_TOKEN

_PAGE_ID_RE = re.compile(r"^[0-9]{1,40}$")
PAGE_OWNER = "page"
WHATSAPP_OWNER = "whatsapp_business_account"
# Owner kind -> the routing asset created with the owner.
_OWNER_ASSETS = {
    PAGE_OWNER: {"platform": "facebook", "object_type": "page", "transport": "page"},
    WHATSAPP_OWNER: {
        "platform": "whatsapp",
        "object_type": "whatsapp_business_account",
        "transport": "whatsapp_cloud",
    },
}


def _uuid(_recordset):
    return str(uuid.uuid4())


class MetaWebhookPage(models.Model):
    _name = "meta.webhook.page"
    _description = "Shared Meta Webhook Page"
    _order = "company_id, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    owner_kind = fields.Selection(
        [
            (PAGE_OWNER, "Facebook Page"),
            (WHATSAPP_OWNER, "WhatsApp Business Account"),
        ],
        required=True,
        default=PAGE_OWNER,
        index=True,
        help=(
            "A WhatsApp Business Account owner keeps the account ID and a system "
            "user token reference; its webhook fields are configured on activation."
        ),
    )
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    endpoint_id = fields.Many2one(
        "meta.webhook.endpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    app_id = fields.Many2one(
        related="endpoint_id.app_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="endpoint_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    external_page_id = fields.Char(required=True, size=40, index=True, copy=False)
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
    )
    access_token_ref = fields.Char(
        required=True,
        size=128,
        groups="base.group_system",
        help="Environment key or mounted secret filename; never the Page token.",
    )
    revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
    )
    subscription_state = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("in_sync", "In Sync"),
            ("drift", "Drift"),
            ("error", "Error"),
            ("uncertain", "Uncertain"),
            ("manual", "Configured on activation"),
        ],
        required=True,
        default="unknown",
        readonly=True,
        index=True,
    )
    observed_fields_json = fields.Json(readonly=True, copy=False)
    verified_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)
    subscription_ids = fields.One2many(
        "meta.webhook.subscription", "page_id", readonly=True
    )
    asset_ids = fields.One2many("meta.webhook.asset", "page_id", readonly=True)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta webhook Page public reference must be unique.",
        ),
        (
            "endpoint_page_unique",
            "unique(endpoint_id, external_page_id)",
            "This Page already exists on the Meta webhook endpoint.",
        ),
        (
            "revision_positive",
            "check(revision > 0)",
            "The Meta webhook Page revision must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        managed_fields = {
            "public_ref",
            "revision",
            "subscription_state",
            "observed_fields_json",
            "verified_at",
            "last_error_class",
            "last_error_message",
        }
        for incoming in vals_list:
            values = dict(incoming)
            if managed_fields.intersection(values):
                raise AccessError(
                    _(
                        "Meta webhook Page identity and runtime state are managed internally."
                    )
                )
            values["external_page_id"] = str(
                values.get("external_page_id") or ""
            ).strip()
            values["access_token_ref"] = str(
                values.get("access_token_ref") or ""
            ).strip()
            if values.get("owner_kind", PAGE_OWNER) not in _OWNER_ASSETS:
                raise ValidationError(_("The Meta webhook owner kind is invalid."))
            normalized.append(values)
        pages = super().create(normalized)
        asset_model = (
            self.env["meta.webhook.asset"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
        )
        for page in pages:
            asset_model.create(
                dict(
                    _OWNER_ASSETS[page.owner_kind],
                    page_id=page.id,
                    external_asset_id=page.external_page_id,
                )
            )
        manual = pages.filtered(lambda page: page.owner_kind != PAGE_OWNER)
        if manual:
            manual.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(manual._manual_subscription_state())
        paused = pages.filtered(lambda page: not page.app_id.active)
        if paused:
            paused.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
                {
                    "subscription_state": "error",
                    "last_error_class": "AppPaused",
                    "last_error_message": "The shared Meta App is paused.",
                }
            )
        return pages

    def write(self, values):
        values = dict(values)
        internal = (
            self.env.context.get("meta_webhook_internal") is META_WEBHOOK_INTERNAL_TOKEN
        )
        runtime_fields = {
            "subscription_state",
            "observed_fields_json",
            "verified_at",
            "last_error_class",
            "last_error_message",
        }
        if runtime_fields.intersection(values) and not internal:
            raise AccessError(_("Meta Page runtime state is managed internally."))
        if "public_ref" in values or "revision" in values:
            raise AccessError(_("Meta webhook Page identity is managed internally."))
        if "endpoint_id" in values and any(
            page.endpoint_id.id != values["endpoint_id"] for page in self
        ):
            raise AccessError(_("A Meta webhook Page cannot change endpoint."))
        if "external_page_id" in values and any(
            page.external_page_id != str(values["external_page_id"] or "").strip()
            for page in self
        ):
            raise AccessError(_("The external Meta Page ID is immutable."))
        if "owner_kind" in values and any(
            page.owner_kind != values["owner_kind"] for page in self
        ):
            raise AccessError(_("The Meta webhook owner kind is immutable."))
        if "access_token_ref" in values:
            values["access_token_ref"] = str(
                values.get("access_token_ref") or ""
            ).strip()
        endpoints = self.mapped("endpoint_id")
        revision_fields = ("active", "credential_backend", "access_token_ref")
        if not set(revision_fields).intersection(values):
            return super().write(values)
        # Reconciliation always locks Endpoint before Page.  Configuration
        # mutations follow the same global order to avoid Endpoint/Page lock
        # inversion under concurrent subscription refresh.
        endpoints.flush_recordset(["revision"])
        if endpoints:
            self.env.cr.execute(
                "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(endpoints.ids)],
            )
        self.flush_recordset(["revision"])
        changed_endpoints = self.env["meta.webhook.endpoint"]
        for page in self.sorted("id"):
            self.env.cr.execute(
                "SELECT revision, active, credential_backend, access_token_ref "
                "FROM meta_webhook_page WHERE id = %s FOR UPDATE",
                [page.id],
            )
            row = self.env.cr.fetchone()
            if not row:
                raise AccessError(_("The Meta webhook Page no longer exists."))
            persisted = dict(zip(revision_fields, row[1:]))
            changed = any(
                values[field_name] != persisted[field_name]
                for field_name in set(revision_fields).intersection(values)
            )
            page_values = dict(values)
            if changed:
                page_values["revision"] = row[0] + 1
                changed_endpoints |= page.endpoint_id
            if changed and not internal:
                page_values.update(page._reset_subscription_state())
            super(MetaWebhookPage, page).write(page_values)
            page.flush_recordset(list(page_values))
            page.invalidate_recordset(["revision"])
        if changed_endpoints and not internal:
            changed_endpoints._meta_subscription_configuration_changed()
        return True

    @api.constrains("external_page_id", "credential_backend", "access_token_ref")
    def _check_configuration(self):
        for page in self:
            if not _PAGE_ID_RE.fullmatch(page.external_page_id or ""):
                raise ValidationError(_("The external Meta Page ID is invalid."))
            try:
                validate_secret_reference(
                    page.credential_backend,
                    page.access_token_ref,
                )
            except MetaCredentialResolutionError:
                raise ValidationError(
                    _("The Meta Page token reference is invalid.")
                ) from None

    def _manual_subscription_state(self):
        return {
            "subscription_state": "manual",
            "observed_fields_json": False,
            "verified_at": False,
            "last_error_class": False,
            "last_error_message": False,
        }

    def _reset_subscription_state(self):
        """Runtime state after a configuration change of this owner."""

        self.ensure_one()
        if self.owner_kind != PAGE_OWNER:
            # Not reconciled here: its webhook fields are set on activation.
            return self._manual_subscription_state()
        return {
            "subscription_state": "unknown",
            "observed_fields_json": False,
            "verified_at": False,
            "last_error_class": False,
            "last_error_message": False,
        }

    def _resolved_access_token(self, expected_revision=None, expected_owner_kind=None):
        """Resolve only the Page token for the subscription reconciler."""

        _app_id, backend, token_ref, _revision = self._locked_configuration(
            expected_revision, expected_owner_kind=expected_owner_kind
        )
        return resolve_secret(backend, token_ref)

    def _locked_configuration(self, expected_revision=None, expected_owner_kind=None):
        """Return one coherent Page configuration under a shared row lock.

        Page consumers receive only Page owners: another owner kind is refused
        before any secret is resolved, unless the caller asks for that kind.
        """

        self.ensure_one()
        expected_owner_kind = expected_owner_kind or PAGE_OWNER
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise MetaCredentialResolutionError(
                "Meta Page expected revision is invalid"
            )
        self.flush_recordset(
            [
                "active",
                "endpoint_id",
                "owner_kind",
                "credential_backend",
                "access_token_ref",
                "revision",
            ]
        )
        endpoint = self.endpoint_id
        endpoint.flush_recordset(["active", "app_id"])
        self.env.cr.execute(
            "SELECT active, app_id FROM meta_webhook_endpoint "
            "WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        endpoint_row = self.env.cr.fetchone()
        if not endpoint_row:
            raise MetaCredentialResolutionError("Meta webhook endpoint is unavailable")
        endpoint_active, app_id = endpoint_row
        if not endpoint_active:
            raise MetaCredentialResolutionError("Meta webhook endpoint is paused")
        self.env.cr.execute(
            "SELECT active, endpoint_id, credential_backend, access_token_ref, "
            "revision, owner_kind "
            "FROM meta_webhook_page WHERE id = %s FOR SHARE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            raise MetaCredentialResolutionError("Meta Page is unavailable")
        page_active, endpoint_id, backend, token_ref, revision, owner_kind = row
        if owner_kind != expected_owner_kind:
            raise MetaCredentialResolutionError(
                "Meta webhook owner is not of the expected kind"
            )
        if endpoint_id != endpoint.id:
            raise MetaCredentialResolutionError("Meta Page configuration changed")
        if expected_revision is not None and revision != expected_revision:
            raise MetaCredentialResolutionError("Meta Page configuration changed")
        if not page_active:
            raise MetaCredentialResolutionError("Meta Page is paused")
        return app_id, backend, token_ref, revision

    def _resolve_graph_runtime(
        self,
        expected_page_revision=None,
        expected_app_revision=None,
        expected_owner_kind=None,
    ):
        """Resolve a fenced App/Page runtime without persisting either secret.

        The Page and endpoint rows remain share-locked for the transaction while
        the shared App resolver obtains its own coherent lock.  Callers receive
        only an immutable in-memory App runtime, the resolved Page token and the
        Page revision that fenced the snapshot.
        """

        self.ensure_one()
        if (
            self.env.context.get("meta_webhook_runtime")
            is not META_WEBHOOK_RUNTIME_TOKEN
        ):
            raise AccessError(_("Meta Page runtime is available only server-side."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        app_id, backend, token_ref, page_revision = self._locked_configuration(
            expected_page_revision, expected_owner_kind=expected_owner_kind
        )
        runtime = (
            self.env["meta.api.app"]
            .browse(app_id)
            .with_context(**{META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN})
            ._resolve_runtime(expected_revision=expected_app_revision)
        )
        return runtime, resolve_secret(backend, token_ref), page_revision

    def _meta_subscription_configuration_changed(self):
        endpoints = self.mapped("endpoint_id")
        endpoints.flush_recordset(["revision"])
        if endpoints:
            self.env.cr.execute(
                "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(endpoints.ids)],
            )
        self.flush_recordset(["revision"])
        for page in self.sorted("id"):
            self.env.cr.execute(
                "SELECT revision FROM meta_webhook_page WHERE id = %s FOR UPDATE",
                [page.id],
            )
            row = self.env.cr.fetchone()
            if not row:
                continue
            # Bypass this override, but keep the mutation in the canonical ORM
            # lifecycle so cache invalidation, modified fields and write metadata
            # stay coherent with the revision fence.
            super(MetaWebhookPage, page).write(
                dict(page._reset_subscription_state(), revision=row[0] + 1)
            )
        endpoints._meta_subscription_configuration_changed()
        return True

    def action_enqueue_subscription_reconcile(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        job = self.env["meta.webhook.subscription.service"]._enqueue_endpoint(
            self.endpoint_id
        )
        return str(job.uuid)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook Pages must be archived."))
