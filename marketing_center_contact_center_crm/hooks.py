from odoo import SUPERUSER_ID, api


def post_init_hook(cr, _registry):
    """Seed paged convergence without scanning all cases during installation."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        company._enqueue_marketing_contact_center_crm_backfill()
