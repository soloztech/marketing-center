from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.company"]._marketing_initialize_capture_stamp(
        "marketing_crm_events_enabled", "marketing_crm_events_changed_at"
    )
