"""Explicit native UTM classification over accepted catalog resolutions.

External identities remain in the catalog. Native campaigns are reporting
records, not an alternative catalog, and their display names are never keys.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.serialization import acquire_advisory_xact_lock
from ..services.tokens import MARKETING_CATALOG_WRITE_TOKEN
from .attribution_resolution import ASSET_RESOLUTION_WRITE_TOKEN

_NATIVE_UTM_WRITE_TOKEN = object()
_SOURCE_FIELDS = {
    "native_utm_mode",
    "native_utm_source_id",
    "native_utm_medium_id",
    "native_utm_auto_create_campaign",
}
_MAPPING_FIELDS = {"native_utm_campaign_id", "native_utm_blocked"}
_MAPPING_METADATA = {
    "native_utm_mapping_origin",
    "native_utm_mapped_at",
    "native_utm_mapped_by_id",
}
_CAMPAIGN_PATH_TYPES = {"campaign", "ad", "group", "ad_group"}


class MarketingCenterSourceNativeUtm(models.Model):
    _inherit = "marketing.center.source"

    native_utm_mode = fields.Selection(
        [("disabled", "Disabled"), ("simulate", "Simulation"), ("apply", "Apply")],
        required=True,
        default="disabled",
        copy=False,
        help="Controls native classification only; it never changes an external campaign.",
    )
    native_utm_source_id = fields.Many2one(
        "utm.source",
        string="Native UTM Source",
        ondelete="restrict",
        copy=False,
    )
    native_utm_medium_id = fields.Many2one(
        "utm.medium",
        string="Native UTM Medium",
        ondelete="restrict",
        copy=False,
    )
    native_utm_auto_create_campaign = fields.Boolean(
        string="Create Missing Native Campaigns",
        default=True,
        help="In Apply mode, create a native campaign for an unmapped external campaign. "
        "An existing campaign with the same name is never linked automatically.",
    )

    @api.constrains("native_utm_mode", "native_utm_source_id", "native_utm_medium_id")
    def _check_native_utm_configuration(self):
        for source in self:
            if source.native_utm_mode != "disabled" and not (
                source.native_utm_source_id and source.native_utm_medium_id
            ):
                raise ValidationError(
                    _(
                        "Select a native source and medium before enabling native classification."
                    )
                )

    def write(self, values):
        changed = bool(_SOURCE_FIELDS.intersection(values))
        if changed:
            # Same row lock as automatic classification: disabling a source or
            # changing its tuple must serialize with an in-flight writer.
            self.check_access_rights("write")
            self.check_access_rule("write")
            self._lock_identity_scope()
        result = super().write(values)
        if changed:
            self.env["marketing.native.utm.service"]._after_native_utm_change(
                source_ids=self.ids
            )
        return result


class MarketingCenterExternalEntityNativeUtm(models.Model):
    _inherit = "marketing.center.external.entity"

    native_utm_campaign_id = fields.Many2one(
        "utm.campaign",
        string="Native UTM Campaign",
        ondelete="restrict",
        copy=False,
        help="Explicit association. Several external campaigns may share this native campaign.",
    )
    native_utm_blocked = fields.Boolean(
        string="Block Native Classification",
        copy=False,
        help="Prevent automatic classification and campaign creation for this external campaign.",
    )
    native_utm_mapping_origin = fields.Selection(
        [("manual", "Manual"), ("automatic", "Automatic")],
        readonly=True,
        copy=False,
    )
    native_utm_mapped_at = fields.Datetime(readonly=True, copy=False)
    native_utm_mapped_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )

    @api.constrains("native_utm_campaign_id", "native_utm_blocked", "entity_type")
    def _check_native_campaign_mapping(self):
        for entity in self:
            if (
                entity.native_utm_campaign_id or entity.native_utm_blocked
            ) and entity.entity_type != "campaign":
                raise ValidationError(
                    _(
                        "Only external campaigns can be associated with a native campaign."
                    )
                )

    def _lock_native_utm_mapping(self):
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM marketing_center_external_entity WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [self.ids],
        )
        self.invalidate_recordset()
        return self

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            (_MAPPING_FIELDS | _MAPPING_METADATA).intersection(values)
            for values in vals_list
        ):
            raise AccessError(
                _("Native campaign associations are set after catalog ingestion.")
            )
        return super().create(vals_list)

    def write(self, values):
        mapping = (_MAPPING_FIELDS | _MAPPING_METADATA).intersection(values)
        if not mapping:
            return super().write(values)
        internal = (
            self.env.context.get("marketing_native_utm_write_token")
            is _NATIVE_UTM_WRITE_TOKEN
        )
        if not internal:
            if set(values) - _MAPPING_FIELDS:
                raise AccessError(
                    _(
                        "Only the native association and its block setting can be edited here."
                    )
                )
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.mapped("source_id")._check_user_capability("approve")
        self._lock_native_utm_mapping()
        values = dict(values)
        if "native_utm_campaign_id" in values:
            values.update(
                native_utm_mapping_origin="automatic" if internal else "manual",
                native_utm_mapped_at=fields.Datetime.now(),
                native_utm_mapped_by_id=self.env.uid,
            )
            # Clearing an explicit mapping must not silently create a replacement.
            if not values["native_utm_campaign_id"] and not internal:
                values.setdefault("native_utm_blocked", True)
        records = self.with_context(
            marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN
        )
        result = super(MarketingCenterExternalEntityNativeUtm, records).write(values)
        self.env["marketing.native.utm.service"]._after_native_utm_change(
            entity_ids=self.ids
        )
        return result


class MarketingNativeUtmService(models.AbstractModel):
    _name = "marketing.native.utm.service"
    _description = "Native UTM Classification Service"

    @api.model
    def _after_native_utm_change(
        self, source_ids=None, entity_ids=None, touchpoint_ids=None
    ):
        """Downstream addons may schedule reclassification, never write inline."""
        return True

    @api.model
    def _result(self, state, reason, **values):
        result = {
            "state": state,
            "reason": reason,
            "source_id": False,
            "entity_id": False,
            "campaign_id": False,
            "utm_source_id": False,
            "medium_id": False,
            "source_mode": "disabled",
            "created": False,
        }
        result.update(values)
        return result

    @api.model
    def _resolve_touchpoint(self, touchpoint, apply=False, lock=False):
        """Read current resolved identities; never interpret raw UTM/ad text.

        The read-only default creates neither mapping nor campaign, including
        when the source is configured in Apply mode.
        """
        if (
            touchpoint._name
            not in {
                "marketing.attribution.touchpoint",
                "marketing.attribution.effective.touchpoint",
            }
            or len(touchpoint) != 1
        ):
            return self._result("missing", "effective_touchpoint_required")
        touchpoint = touchpoint.exists()
        if not touchpoint:
            return self._result("missing", "touchpoint_missing")
        if touchpoint.company_id not in self.env.companies:
            return self._result("conflict", "company_scope_mismatch")
        if apply or lock:
            acquire_advisory_xact_lock(
                self.env.cr,
                "marketing_attribution:%s:%s"
                % (touchpoint.company_id.id, touchpoint.canonical_key),
            )
            acquire_advisory_xact_lock(
                self.env.cr,
                "marketing_asset_resolution:%s:%s"
                % (touchpoint.company_id.id, touchpoint.canonical_key),
            )
            self.env.cr.execute(
                "SELECT id FROM marketing_attribution_touchpoint WHERE id = %s FOR SHARE",
                [touchpoint.id],
            )
            self.env["marketing.attribution.touchpoint"].browse(
                touchpoint.id
            ).invalidate_recordset()
        effective = (
            self.env["marketing.attribution.effective.touchpoint"]
            .sudo()
            .search(
                [
                    ("id", "=", touchpoint.id),
                    ("company_id", "=", touchpoint.company_id.id),
                ],
                limit=1,
            )
        )
        if not effective:
            return self._result("missing", "effective_revision_required")
        if (
            effective.touchpoint_id.privacy_erased_at
            or effective.touchpoint_id.consent_state == "denied"
        ):
            return self._result("missing", "evidence_privacy_unavailable")
        resolutions = (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("touchpoint_id", "=", effective.id),
                    ("company_id", "=", effective.company_id.id),
                    ("canonical_key", "=", effective.canonical_key),
                    ("target_kind", "=", "entity"),
                    ("mapped_entity_type", "in", sorted(_CAMPAIGN_PATH_TYPES)),
                ]
            )
        )
        if (apply or lock) and resolutions:
            # An advisory lock alone cannot refresh a REPEATABLE READ snapshot.
            # A newer committed projection (including a deleted old row) must
            # force this consumer to retry its complete classification.
            resolutions.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM marketing_attribution_asset_resolution "
                "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [resolutions.ids],
            )
            resolutions.invalidate_recordset()
        campaigns = self.env["marketing.center.external.entity"]
        for resolution in resolutions:
            if resolution.state == "ambiguous":
                return self._result("conflict", "ambiguous_asset_resolution")
            if resolution.state != "resolved":
                # An unresolved campaign reference cannot be substituted by an
                # otherwise resolved ad from a different campaign.
                if resolution.mapped_entity_type == "campaign":
                    return self._result("missing", "campaign_resolution_pending")
                continue
            entity = resolution.entity_id
            source = resolution.source_id
            if (
                not entity
                or not source
                or entity.source_id != source
                or entity.company_id != effective.company_id
                or resolution.service_key != source.service
                or resolution.canonical_external_ref != entity.external_ref
                or resolution.mapped_entity_type != entity.entity_type
            ):
                return self._result("conflict", "resolved_identity_mismatch")
            visited = set()
            while entity and entity.entity_type != "campaign":
                if entity.id in visited or entity.source_id != source:
                    return self._result("conflict", "campaign_hierarchy_conflict")
                visited.add(entity.id)
                entity = entity.parent_id
            if entity:
                if entity.source_id != source:
                    return self._result("conflict", "campaign_hierarchy_conflict")
                campaigns |= entity
        if len(campaigns) != 1:
            return self._result(
                "conflict" if campaigns else "missing",
                "multiple_resolved_campaigns" if campaigns else "campaign_not_resolved",
            )
        result = self._resolve_entity(campaigns, apply=apply, lock=lock)
        result["touchpoint_id"] = effective.id
        return result

    @api.model
    def _resolve_entity(self, entity, apply=False, lock=False):
        entity = entity.exists()
        if len(entity) != 1 or entity.entity_type != "campaign":
            return self._result("missing", "catalog_campaign_required")
        if entity.company_id not in self.env.companies:
            return self._result("conflict", "company_scope_mismatch")
        source = entity.source_id
        if apply or lock:
            # Row locks create an MVCC fence: a stale concurrent transaction
            # must retry instead of creating a second campaign after a commit.
            source._lock_identity_scope()
            entity._lock_native_utm_mapping()
            source.invalidate_recordset(list(_SOURCE_FIELDS) + ["active", "state"])
        values = {
            "source_id": source.id,
            "entity_id": entity.id,
            "source_mode": source.native_utm_mode,
            "campaign_id": entity.native_utm_campaign_id.id,
            "utm_source_id": source.native_utm_source_id.id,
            "medium_id": source.native_utm_medium_id.id,
        }
        if (
            not source.active
            or source.state != "active"
            or source.native_utm_mode == "disabled"
        ):
            return self._result("disabled", "source_disabled", **values)
        if entity.native_utm_blocked:
            return self._result("disabled", "campaign_blocked", **values)
        if entity.remote_missing_at or (
            entity.current_revision_id and entity.current_revision_id.is_tombstone
        ):
            return self._result("missing", "remote_campaign_missing", **values)
        if (entity.remote_status or "").strip().lower() in {
            "removed",
            "deleted",
            "archived",
        }:
            return self._result("missing", "remote_campaign_inactive", **values)
        if not source.native_utm_source_id or not source.native_utm_medium_id:
            return self._result("missing", "native_source_or_medium_missing", **values)
        campaign = entity.native_utm_campaign_id.with_context(
            active_test=False
        ).exists()
        if campaign:
            if "active" in campaign._fields and not campaign.active:
                return self._result("conflict", "native_campaign_archived", **values)
            return self._result("ready", "explicit_campaign_mapping", **values)
        if not source.native_utm_auto_create_campaign:
            return self._result(
                "missing", "automatic_campaign_creation_disabled", **values
            )
        if not apply or source.native_utm_mode != "apply":
            return self._result(
                "would_create", "native_campaign_would_be_created", **values
            )
        # The title is for people; the unique native identifier includes the
        # stable external-entity UUID, independent of names and account labels.
        campaign = (
            self.env["utm.campaign"]
            .sudo()
            .create(
                {
                    "title": entity.name,
                    "name": "%s [mc:%s]" % (entity.name, entity.public_ref),
                    "is_auto_campaign": True,
                }
            )
        )
        entity.sudo().with_context(
            marketing_native_utm_write_token=_NATIVE_UTM_WRITE_TOKEN
        ).write(
            {
                "native_utm_campaign_id": campaign.id,
            }
        )
        values.update(campaign_id=campaign.id, created=True)
        return self._result("ready", "native_campaign_created", **values)


class MarketingAssetResolutionNativeUtm(models.AbstractModel):
    _inherit = "marketing.attribution.asset.resolution.service"

    @api.model
    def _project_effective_touchpoint(self, effective):
        result = super()._project_effective_touchpoint(effective)
        self.env["marketing.native.utm.service"]._after_native_utm_change(
            touchpoint_ids=effective.ids
        )
        return result


class MarketingAttributionNativeUtmFence(models.AbstractModel):
    _inherit = "marketing.attribution.service"

    @api.model
    def _ingest_touchpoint(self, company, payload):
        result = super()._ingest_touchpoint(company, payload)
        if result.disposition not in {"accepted", "enriched", "revised"}:
            return result
        # Resolution is deliberately isolated from ingestion failures. If that
        # projection failed, an older projection can survive a newer accepted
        # ledger revision. Mark its mutable row version so a consumer with an
        # old snapshot cannot apply it. No immutable evidence is changed.
        stale = (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("canonical_key", "=", result.canonical_key),
                    ("touchpoint_id", "!=", result.touchpoint_id),
                ]
            )
        )
        for resolution in stale:
            resolution.with_context(
                marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN,
            ).write({"resolver_version": resolution.resolver_version})
        return result


class MarketingTouchpointNativeUtmRetention(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    def _erase_private_values(self, *, token, now):
        result = super()._erase_private_values(token=token, now=now)
        self.env["marketing.native.utm.service"]._after_native_utm_change(
            touchpoint_ids=self.ids
        )
        return result
