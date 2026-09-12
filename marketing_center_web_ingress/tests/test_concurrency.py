import datetime
import threading
import time
import uuid

from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install")
class TestMarketingWebIngressConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _fixture(self):
        suffix = uuid.uuid4().hex
        with self.registry.cursor() as cr:
            env = api.Environment(
                cr,
                SUPERUSER_ID,
                {"allowed_company_ids": [self.env.company.id]},
            )
            endpoint = env["marketing.web.ingress.endpoint"].create(
                {
                    "capture_enabled": True,
                    "capture_purpose": "web_attribution",
                    "privacy_policy_version": "test-v1",
                    "privacy_notice_version": "test-v1",
                    "privacy_legal_basis_code": "documented_test_basis",
                    "privacy_policy_justification": "Synthetic test policy.",
                    "identifier_retention_days": 30,
                    "name": "Concurrent web ingress %s" % suffix[-8:],
                    "company_id": self.env.company.id,
                    "allowed_origins": "https://race.soloz.example",
                    "allowed_hosts": "race.soloz.example",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "endpoint_id": endpoint.id,
                "company_id": self.env.company.id,
                "event_id": "race-%s" % suffix,
                "occurred_at": datetime.datetime.utcnow().replace(microsecond=0),
            }

    def _worker(self, fixture, barrier, outcomes, errors):
        try:
            for attempt in range(5):
                try:
                    with self.registry.cursor() as cr:
                        cr.execute("SET LOCAL lock_timeout = '10s'")
                        cr.execute("SET LOCAL statement_timeout = '12s'")
                        env = api.Environment(
                            cr,
                            SUPERUSER_ID,
                            {"allowed_company_ids": [fixture["company_id"]]},
                        )
                        endpoint = env["marketing.web.ingress.endpoint"].browse(
                            fixture["endpoint_id"]
                        )
                        if not attempt:
                            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                        occurred_at = fixture["occurred_at"]
                        result = env["marketing.web.ingress.service"]._ingest_payload(
                            endpoint,
                            {
                                "event_id": fixture["event_id"],
                                "event_type": "entry_point",
                                "occurred_at": occurred_at.isoformat() + "Z",
                                "landing_url": "https://race.soloz.example/landing",
                                "gclid": "race-gclid-%s" % fixture["event_id"],
                            },
                            origin="https://race.soloz.example",
                            observed_at=occurred_at,
                            body_size_bytes=512,
                        )
                        outcomes.append(result.disposition)
                        cr.commit()  # pylint: disable=invalid-commit
                        return
                except pg_errors.SerializationFailure:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)

    def _cleanup(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id, touchpoint_id FROM marketing_web_ingress_event "
                "WHERE endpoint_id = %s",
                [fixture["endpoint_id"]],
            )
            rows = cr.fetchall()
            event_ids = [row[0] for row in rows]
            touchpoint_ids = [row[1] for row in rows if row[1]]
            if event_ids:
                cr.execute(
                    "DELETE FROM marketing_web_ingress_click_value "
                    "WHERE event_id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_web_ingress_event WHERE id = ANY(%s)",
                    [event_ids],
                )
            if touchpoint_ids:
                cr.execute(
                    "DELETE FROM marketing_attribution_identifier "
                    "WHERE touchpoint_id = ANY(%s)",
                    [touchpoint_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_attribution_evidence "
                    "WHERE touchpoint_id = ANY(%s)",
                    [touchpoint_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_attribution_touchpoint WHERE id = ANY(%s)",
                    [touchpoint_ids],
                )
            cr.execute(
                "DELETE FROM marketing_web_ingress_endpoint WHERE id = %s",
                [fixture["endpoint_id"]],
            )
            cr.commit()  # pylint: disable=invalid-commit

    def test_same_event_race_creates_one_event_vault_and_touchpoint(self):
        fixture = self._fixture()
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []
        workers = [
            threading.Thread(
                target=self._worker,
                args=(fixture, barrier, outcomes, errors),
                name="marketing-web-ingress-%s" % index,
                daemon=True,
            )
            for index in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            if any(worker.is_alive() for worker in workers):
                self.fail("A concurrent web ingress worker did not finish.")
            if errors:
                raise errors[0]
            self.assertEqual(sorted(outcomes), ["accepted", "duplicate"])
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT count(*), count(DISTINCT touchpoint_id) "
                    "FROM marketing_web_ingress_event WHERE endpoint_id = %s",
                    [fixture["endpoint_id"]],
                )
                event_count, touchpoint_count = cr.fetchone()
                cr.execute(
                    "SELECT count(*) FROM marketing_web_ingress_click_value value "
                    "JOIN marketing_web_ingress_event event "
                    "ON event.id = value.event_id WHERE event.endpoint_id = %s",
                    [fixture["endpoint_id"]],
                )
                vault_count = cr.fetchone()[0]
            self.assertEqual(event_count, 1)
            self.assertEqual(touchpoint_count, 1)
            self.assertEqual(vault_count, 1)
        finally:
            self._cleanup(fixture)
