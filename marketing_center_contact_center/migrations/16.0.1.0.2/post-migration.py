from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Queue bounded projections of retained legacy response episodes."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].search([]):
        env["marketing.contact.center.response"].with_context(
            allowed_company_ids=[company.id]
        ).with_company(company)._enqueue_answered_backfill()
