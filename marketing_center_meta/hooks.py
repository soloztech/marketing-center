from odoo import SUPERUSER_ID, api


def crm_post_init_hook(cr, _registry):
    """Seed durable, paged convergence without scanning Lead Ads history."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        company._enqueue_marketing_meta_crm_backfill()


def post_init_hook(cr, registry):
    crm_post_init_hook(cr, registry)


def uninstall_hook(cr, registry):
    from odoo.addons.marketing_center_base.integration_migration import (
        remove_owner_aliases,
    )

    remove_owner_aliases(api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_meta")
