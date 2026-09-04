from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from ..services.catalog_dto import (
    CatalogDTOValidationError,
    EntityIngestResult,
    ExternalEntityDTO,
)
from ..services.serialization import acquire_advisory_xact_lock
from ..services.tokens import MARKETING_CATALOG_WRITE_TOKEN


class MarketingCenterCatalogService(models.AbstractModel):
    _name = "marketing.center.catalog.service"
    _description = "Marketing Center Catalog Service"

    @api.model
    def _upsert_entity(self, company, source, payload, sync_run=None):
        company_id, source_id = self._scope_ids_for_lock(company, source)
        try:
            dto = (
                payload
                if isinstance(payload, ExternalEntityDTO)
                else ExternalEntityDTO.from_dict(payload)
            )
        except CatalogDTOValidationError as error:
            raise ValidationError(_("Invalid external entity: %s") % error) from error

        lock_key = "marketing_catalog:%s:%s:%s" % (
            source_id,
            dto.entity_type,
            dto.external_ref,
        )
        acquire_advisory_xact_lock(
            self.env.cr,
            lock_key,
            "Concurrent marketing catalog ingestion requires a fresh snapshot",
        )
        company, source = self._validated_scope(company, source)
        sync_run = sync_run.sudo().exists() if sync_run else sync_run
        if sync_run and (
            len(sync_run) != 1
            or sync_run.source_id != source
            or sync_run.company_id != company
        ):
            raise ValidationError(_("The sync run does not match the entity source."))
        entity_model = self.env["marketing.center.external.entity"].sudo()
        entity = entity_model.search(
            [
                ("source_id", "=", source.id),
                ("entity_type", "=", dto.entity_type),
                ("external_ref", "=", dto.external_ref),
            ],
            limit=1,
        )
        parent = self._resolve_parent(source, dto)
        if entity and entity.current_content_hash == dto.content_hash:
            values = {
                "last_observed_at": max(entity.last_observed_at, dto.observed_at),
                "last_sync_run_id": (
                    sync_run.id if sync_run else entity.last_sync_run_id.id
                ),
            }
            if dto.provider_updated_at and (
                not entity.provider_updated_at
                or dto.provider_updated_at > entity.provider_updated_at
            ):
                values["provider_updated_at"] = dto.provider_updated_at
            if parent and entity.parent_id != parent:
                values["parent_id"] = parent.id
            self._write_entity(entity, values)
            return self._result(entity, "duplicate")

        if not entity:
            entity = self._create_entity(company, source, dto, parent, sync_run)
        sequence = entity.current_revision_sequence + 1
        revision = self._create_revision(entity, dto, sequence, sync_run)
        self._write_entity(
            entity,
            {
                "external_id": dto.external_id or False,
                "name": dto.name or dto.external_ref,
                "remote_status": dto.remote_status or False,
                "group_type": dto.group_type or False,
                "parent_id": parent.id if parent else False,
                "parent_entity_type": dto.parent_entity_type or False,
                "parent_external_ref": dto.parent_external_ref or False,
                "last_observed_at": max(entity.last_observed_at, dto.observed_at),
                "provider_updated_at": (
                    max(entity.provider_updated_at, dto.provider_updated_at)
                    if entity.provider_updated_at and dto.provider_updated_at
                    else dto.provider_updated_at or entity.provider_updated_at or False
                ),
                "remote_missing_at": dto.remote_missing_at or False,
                "current_content_hash": dto.content_hash,
                "current_revision_sequence": sequence,
                "current_revision_id": revision.id,
                "last_sync_run_id": sync_run.id if sync_run else False,
                "attributes_json": dto.attributes,
            },
        )
        disposition = "tombstone" if dto.remote_missing_at else "accepted"
        if sequence > 1 and disposition == "accepted":
            disposition = "updated"
        return self._result(entity, disposition)

    @api.model
    def _scope_ids_for_lock(self, company, source):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        if (
            not source
            or getattr(source, "_name", "") != "marketing.center.source"
            or len(source) != 1
        ):
            raise ValidationError(_("A single valid marketing source is required."))
        return company.id, source.id

    @api.model
    def _validated_scope(self, company, source):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        company = company.exists()
        if not company or company not in self.env.companies:
            raise AccessError(_("The marketing catalog company is not available."))
        if (
            not source
            or getattr(source, "_name", "") != "marketing.center.source"
            or len(source) != 1
        ):
            raise ValidationError(_("A single valid marketing source is required."))
        source = source.sudo().exists()
        if not source or source.company_id != company:
            raise AccessError(_("The marketing source belongs to another company."))
        return company, source

    @api.model
    def _resolve_parent(self, source, dto):
        if not dto.parent_external_ref:
            return self.env["marketing.center.external.entity"]
        return (
            self.env["marketing.center.external.entity"]
            .sudo()
            .search(
                [
                    ("source_id", "=", source.id),
                    ("entity_type", "=", dto.parent_entity_type),
                    ("external_ref", "=", dto.parent_external_ref),
                ],
                limit=1,
            )
        )

    @api.model
    def _create_entity(self, company, source, dto, parent, sync_run):
        return (
            self.env["marketing.center.external.entity"]
            .sudo()
            .with_company(company)
            .with_context(marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN)
            .create(
                {
                    "source_id": source.id,
                    "entity_type": dto.entity_type,
                    "group_type": dto.group_type or False,
                    "external_ref": dto.external_ref,
                    "external_id": dto.external_id or False,
                    "name": dto.name or dto.external_ref,
                    "remote_status": dto.remote_status or False,
                    "parent_id": parent.id if parent else False,
                    "parent_entity_type": dto.parent_entity_type or False,
                    "parent_external_ref": dto.parent_external_ref or False,
                    "first_observed_at": dto.observed_at,
                    "last_observed_at": dto.observed_at,
                    "provider_updated_at": dto.provider_updated_at or False,
                    "remote_missing_at": dto.remote_missing_at or False,
                    "current_content_hash": dto.content_hash,
                    "current_revision_sequence": 0,
                    "last_sync_run_id": sync_run.id if sync_run else False,
                    "attributes_json": dto.attributes,
                }
            )
        )

    @api.model
    def _create_revision(self, entity, dto, sequence, sync_run):
        return (
            self.env["marketing.center.external.entity.revision"]
            .sudo()
            .with_company(entity.company_id)
            .with_context(marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN)
            .create(
                {
                    "entity_id": entity.id,
                    "revision_sequence": sequence,
                    "content_hash": dto.content_hash,
                    "schema_version": dto.schema_version,
                    "observed_at": dto.observed_at,
                    "provider_updated_at": dto.provider_updated_at or False,
                    "sync_run_id": sync_run.id if sync_run else False,
                    "name": dto.name or dto.external_ref,
                    "remote_status": dto.remote_status or False,
                    "is_tombstone": bool(dto.remote_missing_at),
                    "remote_missing_at": dto.remote_missing_at or False,
                    "snapshot_json": dto.canonical_content(),
                }
            )
        )

    @api.model
    def _write_entity(self, entity, values):
        entity.with_context(
            marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN
        ).write(values)

    @api.model
    def _result(self, entity, disposition):
        return EntityIngestResult(
            entity_id=entity.id,
            public_ref=entity.public_ref,
            disposition=disposition,
            revision_sequence=entity.current_revision_sequence,
            content_hash=entity.current_content_hash,
        )
