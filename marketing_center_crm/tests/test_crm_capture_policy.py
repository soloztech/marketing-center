from unittest.mock import patch

from odoo import fields
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services import capture_policy

_FLAG = "marketing_crm_events_enabled"
_STAMP = "marketing_crm_events_changed_at"


class TestMarketingCrmCapturePolicyStamp(SavepointCase):
    def test_default_enabled_company_is_stamped_and_changes_are_tracked(self):
        created = fields.Datetime.from_string("2026-09-01 10:00:00")
        with patch.object(fields.Datetime, "now", return_value=created):
            company = self.env["res.company"].create({"name": "CRM capture stamp"})
        self.assertTrue(company[_FLAG])
        self.assertEqual(company[_STAMP], created)
        disabled_at = fields.Datetime.from_string("2026-09-03 10:00:00")
        with patch.object(fields.Datetime, "now", return_value=disabled_at):
            company.write({_FLAG: False})
            company.write({_FLAG: False})
        self.assertEqual(company[_STAMP], disabled_at)
        # The general policy is independent and leaves the CRM stamp alone.
        company.write({"marketing_business_events_enabled": True})
        self.assertEqual(company[_STAMP], disabled_at)

    def test_upgrade_initialization_stamps_only_enabled_companies(self):
        company = self.env["res.company"].create({"name": "CRM capture upgrade"})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_company SET marketing_crm_events_changed_at = NULL "
            "WHERE id = %s",
            [company.id],
        )
        company.invalidate_recordset([_STAMP])
        self.assertFalse(company[_STAMP])
        self.assertGreaterEqual(
            capture_policy.initialize_stamp(self.env, _FLAG, _STAMP), 1
        )
        self.assertTrue(company[_STAMP])

    def test_context_default_cannot_inject_the_stamp(self):
        forged = fields.Datetime.from_string("2000-01-01 00:00:00")
        companies = self.env["res.company"].with_context(
            **{"default_%s" % _STAMP: forged}
        )
        disabled = companies.create({"name": "CRM context disabled", _FLAG: False})
        self.assertFalse(disabled[_STAMP])
        enabled = companies.create({"name": "CRM context enabled"})
        self.assertTrue(enabled[_STAMP])
        self.assertNotEqual(enabled[_STAMP], forged)
