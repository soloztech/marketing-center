"""Private incident state; monitoring never invalidates company-wide caches."""
from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

MONITOR_TOKEN = object()
MONITOR_NAMESPACE = 1135593802


class AdmissionMonitor(models.Model):
    _name = "marketing.crm.admission.monitor"
    _description = "Private CRM Admission Incidents"

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="cascade"
    )
    data_json = fields.Json(readonly=True)
    _sql_constraints = [
        ("company_unique", "unique(company_id)", "One admission monitor per company.")
    ]

    @api.model_create_multi
    def create(self, vals_list):
        self._check_service()
        return super().create(vals_list)

    def write(self, values):
        self._check_service()
        return super().write(values)

    def unlink(self):
        self._check_service()
        return super().unlink()

    def _check_service(self):
        if self.env.context.get("crm_admission_monitor_service") is not MONITOR_TOKEN:
            raise AccessError(_("Admission incidents are managed internally."))

    @api.model
    def _locked_company(self, company):
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s,%s)", [MONITOR_NAMESPACE, company.id]
        )
        if not self.env.cr.fetchone()[0]:
            return self.browse()
        service = self.sudo().with_context(crm_admission_monitor_service=MONITOR_TOKEN)
        row = service.search([("company_id", "=", company.id)], limit=1)
        if not row:
            try:
                with self.env.cr.savepoint():
                    row = service.create({"company_id": company.id, "data_json": {}})
            except UniqueViolation:
                # A transaction whose RR snapshot predates another bootstrap
                # must defer to the next monitor transaction.
                return self.browse()
        row.invalidate_recordset()
        return row
