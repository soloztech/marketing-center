from odoo import SUPERUSER_ID, api


def post_init_hook(cr, _registry):
    """Seed durable, paged convergence without scanning history at install time."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].sudo().search([]):
        for phase in (
            "attribution",
            "lifecycle",
            "response_episode",
        ):
            company._enqueue_marketing_contact_center_backfill(phase)
