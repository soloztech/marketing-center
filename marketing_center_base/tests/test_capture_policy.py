from unittest.mock import patch

from odoo import fields
from odoo.tests.common import SavepointCase

from ..services import capture_policy

_FLAG = "marketing_business_events_enabled"
_STAMP = "marketing_business_events_changed_at"


class TestMarketingCapturePolicyStamp(SavepointCase):
    def _company(self, **values):
        return self.env["res.company"].create(
            {"name": "Capture %s" % id(values), **values}
        )

    def test_stamp_changes_only_with_the_effective_policy_value(self):
        company = self._company()
        self.assertFalse(company[_STAMP])
        first = fields.Datetime.from_string("2026-09-01 10:00:00")
        with patch.object(fields.Datetime, "now", return_value=first):
            company.write({_FLAG: True})
        self.assertEqual(company[_STAMP], first)
        later = fields.Datetime.from_string("2026-09-02 10:00:00")
        with patch.object(fields.Datetime, "now", return_value=later):
            company.write({_FLAG: True, "name": "Renamed capture"})
        self.assertEqual(company[_STAMP], first)
        with patch.object(fields.Datetime, "now", return_value=later):
            company.write({_FLAG: False})
        self.assertEqual(company[_STAMP], later)

    def test_created_enabled_is_stamped_and_manual_stamp_is_ignored(self):
        forged = fields.Datetime.from_string("2000-01-01 00:00:00")
        enabled = self._company(**{_FLAG: True, _STAMP: forged})
        self.assertTrue(enabled[_STAMP])
        self.assertNotEqual(enabled[_STAMP], forged)
        disabled = self._company(**{_STAMP: forged})
        self.assertFalse(disabled[_STAMP])
        enabled.write({_STAMP: forged})
        self.assertNotEqual(enabled[_STAMP], forged)

    def test_context_and_ir_default_cannot_inject_the_stamp(self):
        forged = fields.Datetime.from_string("2000-01-01 00:00:00")
        companies = self.env["res.company"].with_context(
            **{"default_%s" % _STAMP: forged}
        )
        disabled = companies.create({"name": "Capture context disabled"})
        self.assertFalse(disabled[_STAMP])
        enabled = companies.create({"name": "Capture context enabled", _FLAG: True})
        self.assertTrue(enabled[_STAMP])
        self.assertNotEqual(enabled[_STAMP], forged)
        self.env["ir.default"].set("res.company", _STAMP, "2000-01-01 00:00:00")
        stored = self._company(name="Capture ir.default")
        self.assertFalse(stored[_STAMP])

    def test_upgrade_initialization_is_idempotent(self):
        enabled = self._company()
        disabled = self._company()
        enabled.with_context(
            marketing_capture_stamp_token=capture_policy.CAPTURE_STAMP_TOKEN
        ).write({_FLAG: True})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_company SET marketing_business_events_changed_at = NULL "
            "WHERE id IN %s",
            [(enabled.id, disabled.id)],
        )
        enabled.invalidate_recordset([_STAMP])
        disabled.invalidate_recordset([_STAMP])
        self.assertGreaterEqual(
            capture_policy.initialize_stamp(self.env, _FLAG, _STAMP), 1
        )
        stamped = enabled[_STAMP]
        self.assertTrue(stamped)
        self.assertFalse(disabled[_STAMP])
        capture_policy.initialize_stamp(self.env, _FLAG, _STAMP)
        self.assertEqual(enabled[_STAMP], stamped)
