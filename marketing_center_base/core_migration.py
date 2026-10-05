"""Own legacy CRM/dashboard records in core without changing their identities.

The compatibility catalog was captured with Odoo 16 from commit 675632982f9f,
base 1.4.0 / CRM 1.5.0 / dashboard 1.1.0. It also covers the older local
1.2.0 / 1.3.1 / 1.0.2 catalog. Unknown metadata requires reconciliation.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

from odoo import _
from odoo.exceptions import ValidationError
from odoo.tools import parse_version

_logger = logging.getLogger(__name__)
CORE = "marketing_center_base"
CORE_VERSION = "16.0.2.0.0"
LEGACY_MODULES = ("marketing_center_crm", "marketing_center_dashboard")
SUPPORTED_VERSIONS = {
    CORE: "16.0.1.4.0",
    "marketing_center_crm": "16.0.1.5.0",
    "marketing_center_dashboard": "16.0.1.1.0",
}
ACTIVE_STATES = ("installed", "to upgrade")


@lru_cache(maxsize=1)
def legacy_catalog():
    path = Path(__file__).parent / "data" / "legacy_xmlids.json"
    return json.loads(path.read_text(encoding="utf-8"))


def require_fused_core(env):
    core = env["ir.module.module"].sudo().search([("name", "=", CORE)], limit=1)
    # Odoo calls the stored database version latest_version. installed_version
    # is computed from the manifest on disk and cannot guard this transition.
    if not core.latest_version or parse_version(core.latest_version) < parse_version(
        CORE_VERSION
    ):
        raise ValidationError(
            _(
                "Upgrade marketing_center_base to 16.0.2.0.0 first. "
                "Upgrading or installing a legacy compatibility package alone is "
                "unsafe before the core migration. Include the core in the upgrade."
            )
        )


def _validate_lineage(env):
    modules = env["ir.module.module"].sudo()
    core = modules.search([("name", "=", CORE)], limit=1)
    if core.latest_version and parse_version(core.latest_version) >= parse_version(
        CORE_VERSION
    ):
        return
    for module in modules.search([("name", "in", list(SUPPORTED_VERSIONS))]):
        version = module.latest_version
        supported = SUPPORTED_VERSIONS[module.name]
        if version and parse_version(version) > parse_version(supported):
            raise ValidationError(
                _(
                    "Unsupported Marketing Center lineage: %(module)s is at %(version)s; "
                    "this migration was verified through %(supported)s. Reconcile source "
                    "and regenerate the compatibility catalog before upgrading."
                )
                % {"module": module.name, "version": version, "supported": supported}
            )


def _matching_canonical(model_data, row):
    canonical = model_data.search(
        [("module", "=", CORE), ("name", "=", row.name)], limit=1
    )
    if canonical and (canonical.model, canonical.res_id) != (row.model, row.res_id):
        raise ValidationError(
            _(
                "Marketing Center XML-id collision: %(core)s.%(name)s and %(legacy)s.%(name)s "
                "refer to different records. Reconcile the conflict before upgrading."
            )
            % {"core": CORE, "name": row.name, "legacy": row.module}
        )
    return canonical


def adopt_legacy_records(env):
    """Pre-migration: preserve record IDs, freeze aliases, transfer ownership."""
    _validate_lineage(env)
    model_data = env["ir.model.data"].sudo()
    rows = model_data.search([("module", "in", list(LEGACY_MODULES))])
    catalog = legacy_catalog()
    unknown = rows.filtered(lambda row: catalog[row.module].get(row.name) != row.model)
    if unknown:
        raise ValidationError(
            _(
                "Undeclared legacy Marketing Center metadata: %s. "
                "Reconcile its source and compatibility catalog before upgrading; "
                "no legacy records have been adopted."
            )
            % ", ".join("%s.%s (%s)" % (r.module, r.name, r.model) for r in unknown)
        )

    # Validate the complete set before modifying it. Metadata models and
    # queue_job are already available before core's registry initialization.
    dangling = rows.filtered(lambda row: not env[row.model].browse(row.res_id).exists())
    for row in rows - dangling:
        _matching_canonical(model_data, row)
    for row in dangling:
        _logger.warning(
            "Removing dangling legacy XML-id metadata %s.%s -> %s,%s; "
            "core data/reflection will recreate its declared record",
            row.module,
            row.name,
            row.model,
            row.res_id,
        )
    dangling.unlink()

    modules = env["ir.module.module"].sudo()
    core = modules.search([("name", "=", CORE)], limit=1)
    legacy = modules.search([("name", "in", list(LEGACY_MODULES))])
    for model in ("ir.model.constraint", "ir.model.relation"):
        records = env[model].sudo().search([("module", "in", legacy.ids)])
        for record in records:
            conflicting = (
                env[model]
                .sudo()
                .search([("module", "=", core.id), ("name", "=", record.name)], limit=1)
            )
            if conflicting:
                raise ValidationError(
                    _(
                        "Conflicting Marketing Center schema ownership: %(model)s %(name)s. "
                        "Reconcile it before upgrading."
                    )
                    % {"model": model, "name": record.name}
                )
        records.write({"module": core.id})

    for row in rows - dangling:
        canonical = _matching_canonical(model_data, row)
        noupdate = False if row.model == "ir.rule" else row.noupdate
        if not canonical:
            model_data.create(
                {
                    "module": CORE,
                    "name": row.name,
                    "model": row.model,
                    "res_id": row.res_id,
                    "noupdate": noupdate,
                }
            )
        elif row.model == "ir.rule" and canonical.noupdate:
            canonical.noupdate = False
        row.noupdate = True


def ensure_legacy_aliases(env, modules=None):
    """Only installed/upgrading shims co-own records; new shim hooks opt in."""
    if modules is None:
        modules = (
            env["ir.module.module"]
            .sudo()
            .search(
                [("name", "in", list(LEGACY_MODULES)), ("state", "in", ACTIVE_STATES)]
            )
            .mapped("name")
        )
    model_data = env["ir.model.data"].sudo()
    catalog = legacy_catalog()
    for module in modules:
        for name, model in catalog[module].items():
            canonical = model_data.search(
                [("module", "=", CORE), ("name", "=", name)], limit=1
            )
            if not canonical or canonical.model != model:
                raise ValidationError(
                    _(
                        "Missing canonical Marketing Center XML-id: "
                        "%(core)s.%(name)s (%(model)s). "
                        "Reconcile source and complete the core upgrade first."
                    )
                    % {"core": CORE, "name": name, "model": model}
                )
            alias = model_data.search(
                [("module", "=", module), ("name", "=", name)], limit=1
            )
            if alias:
                _matching_canonical(model_data, alias)
                alias.noupdate = True
            else:
                model_data.create(
                    {
                        "module": module,
                        "name": name,
                        "model": model,
                        "res_id": canonical.res_id,
                        "noupdate": True,
                    }
                )


def initialize_legacy_crm_capture_stamp(env):
    crm = (
        env["ir.module.module"]
        .sudo()
        .search(
            [("name", "=", "marketing_center_crm"), ("state", "in", ACTIVE_STATES)],
            limit=1,
        )
    )
    if crm.latest_version and parse_version(crm.latest_version) < parse_version(
        "16.0.1.4.0"
    ):
        env["res.company"]._marketing_initialize_capture_stamp(
            "marketing_crm_events_enabled", "marketing_crm_events_changed_at"
        )


def remove_legacy_aliases(env):
    """Uninstall core: aliases must not retain views for removed CRM fields."""
    model_data = env["ir.model.data"].sudo()
    rows = model_data.search([("module", "in", list(LEGACY_MODULES))])
    owned = model_data.browse()
    for row in rows:
        canonical = _matching_canonical(model_data, row)
        if canonical:
            owned |= row
    owned.unlink()
