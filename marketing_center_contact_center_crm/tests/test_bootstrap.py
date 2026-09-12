from unittest.mock import patch

from psycopg2 import OperationalError, errors as pg_errors

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import FailedJobError, RetryableJobError
from odoo.addons.queue_job.job import Job
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
            return_value={"last_conversation_link_id": 220, "has_more": True},
        ), trap_jobs() as trap:
            result = self.env.company._job_marketing_contact_center_crm_backfill(
                after_conversation_link_id=20,
                limit=200,
            )

            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_backfill,
                args=(220, 200),
            )
        self.assertFalse(result["done"])
        self.assertEqual(result["last_conversation_link_id"], 220)

    def test_job_rejects_non_advancing_cursor(self):
        service = self.env["marketing.contact.center.crm.service"]
        with patch.object(
            type(service),
            "_reconcile_existing",
            return_value={"last_conversation_link_id": 20, "has_more": True},
        ), self.assertRaisesRegex(ValidationError, "cursor did not advance"):
            self.env.company._job_marketing_contact_center_crm_backfill(
                after_conversation_link_id=20,
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

    def test_all_company_jobs_enforce_database_retry_budget(self):
        company = self.env.company
        service_class = type(self.env["marketing.contact.center.crm.service"])
        cases = (
            (
                company._job_marketing_contact_center_crm_backfill,
                (),
                "_reconcile_existing",
            ),
            (
                company._job_marketing_contact_center_crm_conversation_link,
                (1,),
                "_active_conversation_link",
            ),
            (
                company._job_marketing_contact_center_crm_attribution_link,
                (1,),
                "_current_attribution_link",
            ),
        )
        for entrypoint, args, method in cases:
            with self.subTest(entrypoint=entrypoint.__name__):
                job = Job(entrypoint, args=args, max_retries=8)
                job.retry = 6
                with patch.object(
                    service_class, method, side_effect=pg_errors.SerializationFailure()
                ) as failure:
                    with self.assertRaises(RetryableJobError):
                        job.perform()
                    self.assertEqual(job.retry, 7)
                    with self.assertRaises(FailedJobError):
                        job.perform()
                    self.assertEqual(job.retry, 8)
                    self.assertEqual(failure.call_count, 2)

                permanent_job = Job(entrypoint, args=args, max_retries=8)
                with patch.object(
                    service_class, method, side_effect=OperationalError("unclassified")
                ), self.assertRaises(OperationalError):
                    permanent_job.perform()
                self.assertEqual(permanent_job.retry, 1)
