from odoo import SUPERUSER_ID, api


def attribution_post_init_hook(cr, _registry):
    """Seed durable, paged convergence without scanning history at install time."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        for phase in (
            "attribution",
            "lifecycle",
            "response_episode",
        ):
            company._enqueue_marketing_contact_center_backfill(phase)


def crm_post_init_hook(cr, _registry):
    """Seed paged convergence without scanning all cases during installation."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        company._enqueue_marketing_contact_center_crm_backfill()


def post_init_hook(cr, registry):
    attribution_post_init_hook(cr, registry)
    crm_post_init_hook(cr, registry)


def uninstall_hook(cr, registry):
    from odoo.addons.marketing_center_base.integration_migration import (
        remove_owner_aliases,
    )

    remove_owner_aliases(
        api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_contact_center"
    )
