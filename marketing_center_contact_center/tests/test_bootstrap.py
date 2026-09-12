from unittest.mock import patch

from psycopg2 import OperationalError, errors as pg_errors

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import FailedJobError, RetryableJobError
from odoo.addons.queue_job.job import Job
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

    def test_bootstrap_and_answered_backfill_enforce_database_retry_budget(self):
        company = self.env.company
        responses = self.env["marketing.contact.center.response"]
        attribution = self.env["marketing.contact.center.attribution.service"]
        cases = (
            (
                company._job_marketing_contact_center_backfill,
                ("attribution",),
                type(attribution),
                "_enqueue_backfill",
            ),
            (responses._job_backfill_answered_events, (), type(responses), "search"),
        )
        for entrypoint, args, target, method in cases:
            with self.subTest(entrypoint=entrypoint.__name__):
                job = Job(entrypoint, args=args, max_retries=8)
                job.retry = 6
                with patch.object(
                    target, method, side_effect=pg_errors.SerializationFailure()
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
                    target, method, side_effect=OperationalError("unclassified")
                ), self.assertRaises(OperationalError):
                    permanent_job.perform()
                self.assertEqual(permanent_job.retry, 1)
