from odoo import SUPERUSER_ID, api


def post_init_hook(cr, _registry):
    """Seed durable, paged convergence without scanning Lead Ads history."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        company._enqueue_marketing_meta_crm_backfill()
