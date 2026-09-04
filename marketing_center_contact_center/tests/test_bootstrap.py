from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs


class TestMarketingContactCenterBootstrap(SavepointCase):
    def test_enqueue_is_one_durable_company_job(self):
        with trap_jobs() as trap:
            self.env.company._enqueue_marketing_contact_center_backfill("attribution")

            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_backfill,
                args=("attribution", 0, 200),
                properties={
                    "identity_key": (
                        "marketing_contact_center:bootstrap:attribution:company:%s:"
                        "after:0" % self.env.company.id
                    ),
                    "priority": 50,
                },
            )

    def test_job_chains_only_after_a_monotonic_page(self):
        service = self.env["marketing.contact.center.attribution.service"]
        with patch.object(
            type(service),
            "_enqueue_backfill",
            return_value={"last_id": 240, "has_more": True},
        ), trap_jobs() as trap:
            result = self.env.company._job_marketing_contact_center_backfill(
                "attribution", after_id=40, limit=200
            )

            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_backfill,
                args=("attribution", 240, 200),
            )
        self.assertFalse(result["done"])
        self.assertEqual(result["last_id"], 240)

    def test_job_rejects_non_advancing_cursor(self):
        service = self.env["marketing.contact.center.lifecycle.service"]
        with patch.object(
            type(service),
            "_enqueue_backfill",
            return_value={"last_id": 40, "has_more": True},
        ), self.assertRaisesRegex(ValidationError, "cursor did not advance"):
            self.env.company._job_marketing_contact_center_backfill(
                "lifecycle", after_id=40, limit=200
            )

    def test_unknown_phase_is_rejected_before_queueing(self):
        with trap_jobs() as trap, self.assertRaises(ValidationError):
            self.env.company._enqueue_marketing_contact_center_backfill("unknown")
        trap.assert_jobs_count(0)
