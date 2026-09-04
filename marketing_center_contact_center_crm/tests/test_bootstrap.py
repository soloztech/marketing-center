from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs


class TestMarketingContactCenterCrmBootstrap(SavepointCase):
    def test_enqueue_is_one_durable_company_job(self):
        with trap_jobs() as trap:
            self.env.company._enqueue_marketing_contact_center_crm_backfill()

            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_backfill,
                args=(0, 50),
                properties={
                    "identity_key": (
                        "marketing_contact_center_crm:bootstrap:company:%s:after:0"
                        % self.env.company.id
                    ),
                    "priority": 50,
                },
            )

    def test_job_chains_only_after_a_monotonic_page(self):
        service = self.env["marketing.contact.center.crm.service"]
        with patch.object(
            type(service),
            "_reconcile_existing",
            return_value={"last_case_link_id": 220, "has_more": True},
        ), trap_jobs() as trap:
            result = self.env.company._job_marketing_contact_center_crm_backfill(
                after_case_link_id=20,
                limit=200,
            )

            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_backfill,
                args=(220, 200),
            )
        self.assertFalse(result["done"])
        self.assertEqual(result["last_case_link_id"], 220)

    def test_job_rejects_non_advancing_cursor(self):
        service = self.env["marketing.contact.center.crm.service"]
        with patch.object(
            type(service),
            "_reconcile_existing",
            return_value={"last_case_link_id": 20, "has_more": True},
        ), self.assertRaisesRegex(ValidationError, "cursor did not advance"):
            self.env.company._job_marketing_contact_center_crm_backfill(
                after_case_link_id=20,
                limit=200,
            )

    def test_reconciliation_rejects_unavailable_company(self):
        with self.assertRaises(AccessError):
            self.env["marketing.contact.center.crm.service"]._reconcile_existing(
                company=object()
            )
        with self.assertRaises(AccessError):
            self.env["marketing.contact.center.crm.service"]._reconcile_existing(
                company=self.env["res.company"]
            )
