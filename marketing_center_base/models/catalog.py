import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import MARKETING_CATALOG_WRITE_TOKEN


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingCenterExternalEntity(models.Model):
    _name = "marketing.center.external.entity"
    _description = "Marketing Center External Entity"
    _order = "source_id, entity_type, external_ref, id"
    _rec_name = "name"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="source_id.company_id", store=True, readonly=True, index=True
    )
    entity_type = fields.Char(required=True, size=128, index=True, readonly=True)
    group_type = fields.Char(size=128, index=True, readonly=True)
    external_ref = fields.Char(required=True, size=1024, index=True, readonly=True)
    external_id = fields.Char(size=512, index=True, readonly=True)
    name = fields.Char(required=True, index=True, readonly=True)
    remote_status = fields.Char(size=128, index=True, readonly=True)
    parent_id = fields.Many2one(
        "marketing.center.external.entity",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    parent_entity_type = fields.Char(size=128, index=True, readonly=True)
    parent_external_ref = fields.Char(size=1024, index=True, readonly=True)
    first_observed_at = fields.Datetime(required=True, index=True, readonly=True)
    last_observed_at = fields.Datetime(required=True, index=True, readonly=True)
    provider_updated_at = fields.Datetime(index=True, readonly=True)
    remote_missing_at = fields.Datetime(index=True, readonly=True)
    current_content_hash = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    current_revision_sequence = fields.Integer(required=True, default=0, readonly=True)
    current_revision_id = fields.Many2one(
        "marketing.center.external.entity.revision",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    revision_ids = fields.One2many(
        "marketing.center.external.entity.revision", "entity_id", readonly=True
    )
    last_sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    attributes_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The external entity public reference must be unique.",
        ),
        (
            "source_type_ref_unique",
            "unique(source_id, entity_type, external_ref)",
            "This external entity already exists in the marketing source.",
        ),
        (
            "current_hash_sha256",
            "check(char_length(current_content_hash) = 64)",
            "The current entity content hash must be a SHA-256 digest.",
        ),
        (
            "revision_sequence_nonnegative",
            "check(current_revision_sequence >= 0)",
            "The current entity revision sequence cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_catalog_write_token")
            is not MARKETING_CATALOG_WRITE_TOKEN
        ):
            raise AccessError(_("External entities are created only by catalog sync."))
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("marketing_catalog_write_token")
            is not MARKETING_CATALOG_WRITE_TOKEN
        ):
            raise AccessError(_("External entities are updated only by catalog sync."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("External entities cannot be deleted."))

    @api.constrains("parent_id", "source_id")
    def _check_parent_scope(self):
        for entity in self:
            if entity.parent_id and entity.parent_id.source_id != entity.source_id:
                raise ValidationError(
                    _("An external entity parent must belong to the same source.")
                )
        if not self._check_recursion():
            raise ValidationError(_("External entity hierarchy cannot contain cycles."))

    @api.constrains("current_revision_id")
    def _check_current_revision(self):
        for entity in self:
            if (
                entity.current_revision_id
                and entity.current_revision_id.entity_id != entity
            ):
                raise ValidationError(
                    _("The current revision must belong to its external entity.")
                )


class MarketingCenterExternalEntityRevision(models.Model):
    _name = "marketing.center.external.entity.revision"
    _description = "Marketing Center External Entity Revision"
    _order = "entity_id, revision_sequence desc, id desc"
    _rec_name = "content_hash"
    _check_company_auto = True

    entity_id = fields.Many2one(
        "marketing.center.external.entity",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    source_id = fields.Many2one(
        related="entity_id.source_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="entity_id.company_id", store=True, readonly=True, index=True
    )
    revision_sequence = fields.Integer(required=True, readonly=True)
    content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    schema_version = fields.Integer(required=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    provider_updated_at = fields.Datetime(readonly=True)
    sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    name = fields.Char(required=True, readonly=True)
    remote_status = fields.Char(size=128, index=True, readonly=True)
    is_tombstone = fields.Boolean(
        required=True, default=False, index=True, readonly=True
    )
    remote_missing_at = fields.Datetime(readonly=True)
    snapshot_json = fields.Json(
        required=True,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "rev_unique",
            "unique(entity_id, revision_sequence)",
            "This external entity revision sequence already exists.",
        ),
        (
            "content_hash_sha256",
            "check(char_length(content_hash) = 64)",
            "The external entity content hash must be a SHA-256 digest.",
        ),
        (
            "seq_positive",
            "check(revision_sequence > 0)",
            "The external entity revision sequence must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_catalog_write_token")
            is not MARKETING_CATALOG_WRITE_TOKEN
        ):
            raise AccessError(_("Entity revisions are created only by catalog sync."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("External entity revisions cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("External entity revisions cannot be deleted."))
