from odoo import SUPERUSER_ID, _, api


def uninstall_hook(cr, registry):
    from odoo.addons.marketing_center_base.integration_migration import (
        remove_owner_aliases,
    )

    from .ingress_migration import remove_owner_aliases as remove_ingress_aliases

    remove_ingress_aliases(
        api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_website"
    )
    remove_owner_aliases(
        api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_website"
    )


def pre_init_hook(cr):
    from odoo.exceptions import ValidationError

    env = api.Environment(cr, SUPERUSER_ID, {})
    legacy = (
        env["ir.module.module"]
        .sudo()
        .search([("name", "=", "marketing_center_web_ingress")])
    )
    aliases = (
        env["ir.model.data"]
        .sudo()
        .search_count([("module", "=", "marketing_center_web_ingress")])
    )
    schema = any(
        env[model].sudo().search_count([("module", "in", legacy.ids)])
        for model in ("ir.model.constraint", "ir.model.relation")
    )
    if aliases or schema or (legacy and legacy.state != "uninstallable"):
        raise ValidationError(
            _(
                "Existing Web Ingress requires its predecessor Website 16.0.2.0.0 "
                "and prepared ownership migration before this installation."
            )
        )
