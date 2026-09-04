from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)

from ..services.observability import (
    GOOGLE_CHANGE_CONTRACT_VERSION,
    GOOGLE_CHANGE_RUN_ENTITY_TYPE,
    GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
    GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE,
    GoogleChangeObservationDTO,
    GoogleDiagnosticObservationDTO,
    GoogleObservationIngestResult,
)
from ..services.tokens import MARKETING_GOOGLE_OBSERVATION_TOKEN


class GoogleObservationMixin(models.AbstractModel):
    _name = "marketing.center.google.observation.mixin"
    _description = "Google Ads Observation Scope"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    connection_id = fields.Many2one(
        "marketing.center.connection",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    profile_id = fields.Many2one(
        "marketing.center.google.profile",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    profile_revision = fields.Integer(
        required=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    identity_revision = fields.Integer(
        required=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    customer_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    # This digest intentionally covers the complete normalized observation.  A
    # change-event digest therefore also commits to the (separately protected)
    # actor hash.  Keep it technical/admin-only so a manager cannot use digest
    # comparisons as an oracle for candidate Google account e-mail addresses.
    content_hash = fields.Char(
        required=True,
        size=64,
        index=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    contract_version = fields.Char(required=True, size=128, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        source_ids = sorted(
            {
                int(values["source_id"])
                for values in vals_list
                if values.get("source_id")
            }
        )
        sources = (
            self.env["marketing.center.source"]
            .browse(source_ids)
            ._lock_identity_scope()
        )
        if (
            self.env.context.get("marketing_google_observation_token")
            is not MARKETING_GOOGLE_OBSERVATION_TOKEN
        ):
            raise AccessError(_("Google Ads observations are created only by service."))
        sources._mark_identity_evidence()
        return super().create(vals_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("Google Ads observations are immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Google Ads observations cannot be deleted."))

    @api.constrains(
        "company_id",
        "source_id",
        "connection_id",
        "profile_id",
        "profile_revision",
        "identity_revision",
        "customer_ref",
        "sync_run_id",
    )
    def _check_observation_scope(self):
        for record in self:
            if (
                record.source_id.company_id != record.company_id
                or record.connection_id.company_id != record.company_id
                or record.connection_id.source_id != record.source_id
                or record.profile_id.company_id != record.company_id
                or record.connection_id.google_profile_id != record.profile_id
                or record.sync_run_id.company_id != record.company_id
                or record.sync_run_id.source_id != record.source_id
                or record.sync_run_id.connection_id != record.connection_id
                or record.profile_revision != record.sync_run_id.profile_revision
                or record.identity_revision
                != record.connection_id.google_identity_revision
                or record.customer_ref != record.source_id.external_account_ref
            ):
                raise ValidationError(
                    _("A Google Ads observation cannot cross its fenced scope.")
                )


class GoogleChangeObservation(models.Model):
    _name = "marketing.center.google.change.observation"
    _inherit = "marketing.center.google.observation.mixin"
    _description = "Immutable Google Ads Change Observation"
    _order = "occurred_at desc, id desc"
    _rec_name = "event_ref"

    event_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    resource_type = fields.Char(required=True, size=128, index=True, readonly=True)
    resource_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    operation = fields.Char(required=True, size=128, index=True, readonly=True)
    client_type = fields.Char(required=True, size=128, index=True, readonly=True)
    changed_fields_json = fields.Json(readonly=True, copy=False)
    actor_hash = fields.Char(
        size=64,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "source_event_unique",
            "unique(source_id, event_ref)",
            "This Google Ads change event is already observed.",
        ),
        (
            "hashes_sha256",
            "check(char_length(content_hash) = 64 "
            "and (actor_hash is null or char_length(actor_hash) = 64))",
            "Google Ads change hashes must be SHA-256 digests.",
        ),
    ]


class GoogleDiagnosticObservation(models.Model):
    _name = "marketing.center.google.diagnostic.observation"
    _inherit = "marketing.center.google.observation.mixin"
    _description = "Immutable Google Ads Delivery Diagnostic"
    _order = "observed_at desc, id desc"
    _rec_name = "asset_ref"

    observation_key = fields.Char(required=True, size=64, index=True, readonly=True)
    asset_type = fields.Char(required=True, size=128, index=True, readonly=True)
    asset_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    configured_status = fields.Char(required=True, size=128, index=True, readonly=True)
    primary_status = fields.Char(required=True, size=128, index=True, readonly=True)
    severity = fields.Selection(
        [("ok", "OK"), ("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        required=True,
        index=True,
        readonly=True,
    )
    status_reasons_json = fields.Json(readonly=True, copy=False)
    policy_approval_status = fields.Char(size=128, index=True, readonly=True)
    policy_review_status = fields.Char(size=128, index=True, readonly=True)
    policy_topics_json = fields.Json(readonly=True, copy=False)

    _sql_constraints = [
        (
            "source_observation_unique",
            "unique(source_id, observation_key)",
            "This Google Ads diagnostic observation already exists.",
        ),
        (
            "hashes_sha256",
            "check(char_length(observation_key) = 64 "
            "and char_length(content_hash) = 64)",
            "Google Ads diagnostic hashes must be SHA-256 digests.",
        ),
    ]


class GoogleObservationService(models.AbstractModel):
    _name = "marketing.center.google.observation.service"
    _description = "Google Ads Immutable Observation Service"

    @api.model
    def _ingest_change(self, run, item):
        run = self._validated_run(run, GOOGLE_CHANGE_RUN_ENTITY_TYPE)
        if not isinstance(item, GoogleChangeObservationDTO):
            raise ValidationError(_("The Google Ads change observation is invalid."))
        return self._upsert(
            run,
            self.env["marketing.center.google.change.observation"],
            [("event_ref", "=", item.event_ref)],
            item.content_hash,
            {
                **self._scope_values(run, item.observed_at),
                "contract_version": GOOGLE_CHANGE_CONTRACT_VERSION,
                "event_ref": item.event_ref,
                "occurred_at": item.occurred_at,
                "resource_type": item.resource_type,
                "resource_ref": item.resource_ref,
                "operation": item.operation,
                "client_type": item.client_type,
                "changed_fields_json": list(item.changed_fields),
                "actor_hash": item.actor_hash or False,
                "content_hash": item.content_hash,
            },
            "change:%s" % item.event_ref,
        )

    @api.model
    def _ingest_diagnostic(self, run, item):
        run = self._validated_run(run, GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE)
        if not isinstance(item, GoogleDiagnosticObservationDTO):
            raise ValidationError(
                _("The Google Ads diagnostic observation is invalid.")
            )
        observation_key = sha256_text(
            canonical_json(
                {
                    "asset_ref": item.asset_ref,
                    "run_key": run.run_key,
                    "source_ref": run.source_id.public_ref,
                }
            )
        )
        return self._upsert(
            run,
            self.env["marketing.center.google.diagnostic.observation"],
            [("observation_key", "=", observation_key)],
            item.content_hash,
            {
                **self._scope_values(run, item.observed_at),
                "contract_version": GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
                "observation_key": observation_key,
                "asset_type": item.asset_type,
                "asset_ref": item.asset_ref,
                "configured_status": item.configured_status,
                "primary_status": item.primary_status,
                "severity": item.severity,
                "status_reasons_json": list(item.status_reasons),
                "policy_approval_status": item.policy_approval_status or False,
                "policy_review_status": item.policy_review_status or False,
                "policy_topics_json": list(item.policy_topics),
                "content_hash": item.content_hash,
            },
            "diagnostic:%s" % observation_key,
        )

    @api.model
    def _upsert(self, run, model, domain, content_hash, values, lock_suffix):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["marketing_google_observation:%s:%s" % (run.source_id.id, lock_suffix)],
        )
        existing = model.sudo().search(
            [("source_id", "=", run.source_id.id)] + domain,
            limit=1,
        )
        if existing:
            if existing.content_hash != content_hash:
                raise ValidationError(
                    _("A Google Ads observation conflicts with its immutable key.")
                )
            return GoogleObservationIngestResult(
                disposition="duplicate",
                content_hash=content_hash,
                record_id=existing.id,
            )
        record = (
            model.sudo()
            .with_company(run.company_id)
            .with_context(
                marketing_google_observation_token=MARKETING_GOOGLE_OBSERVATION_TOKEN
            )
            .create(values)
        )
        return GoogleObservationIngestResult(
            disposition="created",
            content_hash=content_hash,
            record_id=record.id,
        )

    @api.model
    def _validated_run(self, run, entity_type):
        run = run.sudo().exists()
        if (
            not run
            or len(run) != 1
            or run.company_id not in self.env.companies
            or run.sync_kind != "catalog"
            or run.entity_type != entity_type
            or run.source_id.service != "google.ads"
            or run.connection_id.source_id != run.source_id
            or not run.connection_id.google_profile_id
        ):
            raise ValidationError(_("The Google Ads observation run is invalid."))
        return run

    @api.model
    def _scope_values(self, run, observed_at):
        connection = run.connection_id
        return {
            "company_id": run.company_id.id,
            "source_id": run.source_id.id,
            "connection_id": connection.id,
            "profile_id": connection.google_profile_id.id,
            "profile_revision": run.profile_revision,
            "identity_revision": connection.google_identity_revision,
            "customer_ref": run.source_id.external_account_ref,
            "sync_run_id": run.id,
            "observed_at": observed_at,
        }
