"""Optional point lookup with the same authorization fences as catalog reads."""

from psycopg2 import Error as DatabaseError

from odoo import api, fields, models

from ..services.adapter import (
    META_ADAPTER_KEY,
    META_ADS_SERVICE,
    MetaMarketingReadAdapter,
)


class AuthorizedMetaPreview(dict):
    """Transient scope snapshot; dictionary serialization includes copy only."""

    def __init__(self, values, fingerprint):
        super().__init__(values)
        self.fingerprint = fingerprint


class MarketingCenterMetaAdPreviewService(models.AbstractModel):
    _name = "marketing.center.meta.ad.preview.service"
    _description = "Authorized Meta Ad Preview Read Service"

    @api.model
    def _preview_scope(self, entity):
        if (
            getattr(entity, "_name", "") != "marketing.center.external.entity"
            or len(entity) != 1
            or entity.company_id not in self.env.companies
        ):
            return False
        entity = entity.sudo().exists()
        if not entity or entity.entity_type != "ad" or entity.remote_missing_at:
            return False
        source = entity.source_id
        if (
            source.company_id != entity.company_id
            or source.service != META_ADS_SERVICE
            or not source.active
            or not source.read_enabled
            or source.state != "active"
            or not isinstance(source.effective_capabilities_json, dict)
            or source.effective_capabilities_json.get("read_entities") is not True
        ):
            return False
        connections = (
            self.env["marketing.center.connection"]
            .sudo()
            .search(
                [
                    ("source_id", "=", source.id),
                    ("company_id", "=", source.company_id.id),
                    ("adapter_key", "=", META_ADAPTER_KEY),
                    ("purpose", "=", "reader"),
                    ("active", "=", True),
                    ("state", "=", "ready"),
                ],
                limit=2,
            )
        )
        if len(connections) != 1:
            return False
        connection = connections
        profile = connection.meta_profile_id
        now = fields.Datetime.now()
        if (
            not profile
            or not profile.active
            or profile.reader_kind != "ads_reader"
            or profile.company_id != source.company_id
            or not profile.meta_app_id.active
            or profile.meta_app_id.company_id != source.company_id
            or profile.profile_revision != connection.profile_revision
            or profile.public_ref != connection.profile_public_ref
            or profile.health_state != "healthy"
            or not isinstance(profile.verified_scopes_json, list)
            or "ads_read" not in profile.verified_scopes_json
            or "ads_read" not in profile.required_scope_keys()
            or not isinstance(connection.effective_capabilities_json, dict)
            or connection.effective_capabilities_json.get("read_entities") is not True
            or (connection.cooldown_until and connection.cooldown_until > now)
            or (profile.token_expires_at and profile.token_expires_at <= now)
            or (
                profile.data_access_expires_at and profile.data_access_expires_at <= now
            )
        ):
            return False
        return entity, source, connection, profile, profile.meta_app_id

    @api.model
    def _preview_fingerprint(self, scope):
        entity, source, connection, profile, app = scope
        return (
            entity.id,
            entity.company_id.id,
            entity.external_ref,
            entity.current_content_hash,
            source.id,
            source.external_account_ref,
            source.configuration_revision,
            connection.id,
            connection.binding_revision,
            connection.profile_revision,
            profile.id,
            profile.profile_revision,
            app.id,
            app.revision,
        )

    @api.model
    def _fetch_ad_preview(self, entity):
        scope = self._preview_scope(entity)
        if not scope:
            return {}
        expected = self._preview_fingerprint(scope)
        entity, source, connection, profile, app = scope
        try:
            values = MetaMarketingReadAdapter(
                profile, expected_app_revision=app.revision
            ).fetch_ad_preview(source.external_account_ref, entity.external_ref)
        except DatabaseError:
            raise
        except Exception:
            # This optional read must not leak SDK/HTTP exception text, prepared
            # URLs or credentials to the operational job. No profile is changed.
            return {}
        for record in (source, connection, profile, app, entity):
            record.invalidate_recordset()
        current = self._preview_scope(entity)
        if not current or self._preview_fingerprint(current) != expected:
            return {}
        return AuthorizedMetaPreview(values, expected)

    @api.model
    def _validate_ad_preview(self, entity, fingerprint):
        """Run only after Graph AND thumbnail HTTP have completed."""
        scope = self._preview_scope(entity)
        if not scope or self._preview_fingerprint(scope) != fingerprint:
            return False
        for record in scope:
            self.env.cr.execute(
                'SELECT id FROM "%s" WHERE id = %%s FOR SHARE NOWAIT' % record._table,
                [record.id],
            )
            if not self.env.cr.fetchone():
                return False
            record.invalidate_recordset()
        current = self._preview_scope(entity)
        return bool(current and self._preview_fingerprint(current) == fingerprint)
