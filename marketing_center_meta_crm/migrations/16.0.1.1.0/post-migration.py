from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_meta_crm.models.tokens import (
    MARKETING_META_CRM_MIGRATION_TOKEN,
)


def migrate(cr, version):
    """Preserve the historical intake already enabled before this policy existed."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    Route = env["marketing.center.meta.lead.route"].with_context(active_test=False)
    after_id = 0
    while True:
        routes = Route.search(
            [("id", ">", after_id), ("crm_auto_create_lead", "=", True)],
            order="id",
            limit=200,
        )
        if not routes:
            return
        routes.with_context(
            marketing_meta_crm_migration=MARKETING_META_CRM_MIGRATION_TOKEN
        ).write({"crm_history_policy": "all"})
        after_id = routes[-1].id
