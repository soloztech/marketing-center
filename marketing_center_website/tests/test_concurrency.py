import datetime
import threading
import time
import uuid

from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install")
class TestMarketingWebsiteConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _fixture(self, *, active_binding=False, with_action=False):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        with self.registry.cursor() as cr:
            env = api.Environment(
                cr,
                SUPERUSER_ID,
                {"allowed_company_ids": [company_id]},
            )
            website = env["website"].create(
                {
                    "name": "Marketing Website concurrency %s" % suffix[-8:],
                    "company_id": company_id,
                }
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
                    "name": "Website concurrency endpoint %s" % suffix[-8:],
                    "company_id": company_id,
                    "allowed_origins": "https://race-%s.example" % suffix[-8:],
                    "allowed_hosts": "race-%s.example" % suffix[-8:],
                }
            )
            binding = env["marketing.website.ingress.binding"].create(
                {
                    "website_id": website.id,
                    "endpoint_id": endpoint.id,
                    "active": active_binding,
                }
            )
            action_id = 0
            if with_action:
                action_id = (
                    env["marketing.website.action"]
                    .create(
                        {
                            "name": "Concurrent WhatsApp handoff",
                            "binding_id": binding.id,
                            "kind": "whatsapp_handoff",
                            "route_ref": "whatsapp.race.%s" % suffix[-8:],
                            "source_path": "/",
                            "whatsapp_destination": "5519999999999",
                        }
                    )
                    .id
                )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "action_id": action_id,
                "binding_id": binding.id,
                "company_id": company_id,
                "endpoint_id": endpoint.id,
                "website_id": website.id,
            }

    def _run_worker(self, fixture, barrier, operation, outcomes, errors):
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
                        if not attempt:
                            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                        value = operation(env, fixture)
                        cr.commit()  # pylint: disable=invalid-commit
                        outcomes.append(("committed", value))
                        return
                except pg_errors.SerializationFailure:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
                except UserError as error:
                    outcomes.append(("rejected", type(error).__name__))
                    return
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)

    def _race(self, fixture, operations):
        barrier = threading.Barrier(len(operations))
        outcomes = []
        errors = []
        workers = [
            threading.Thread(
                target=self._run_worker,
                args=(fixture, barrier, operation, outcomes, errors),
                name="marketing-website-race-%s" % index,
                daemon=True,
            )
            for index, operation in enumerate(operations)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(self.WORKER_TIMEOUT_SECONDS)
        if any(worker.is_alive() for worker in workers):
            self.fail("A concurrent Website configuration worker did not finish.")
        if errors:
            raise errors[0]
        return outcomes

    def _cleanup(self, fixture):
        with self.registry.cursor() as cr:
            if fixture["action_id"]:
                cr.execute(
                    "DELETE FROM marketing_website_redirect_grant "
                    "WHERE action_id = %s",
                    [fixture["action_id"]],
                )
                cr.execute(
                    "DELETE FROM marketing_website_action WHERE id = %s",
                    [fixture["action_id"]],
                )
            cr.execute(
                "DELETE FROM marketing_website_ingress_binding WHERE id = %s",
                [fixture["binding_id"]],
            )
            env = api.Environment(
                cr,
                SUPERUSER_ID,
                {"allowed_company_ids": [fixture["company_id"]]},
            )
            env["website"].browse(fixture["website_id"]).unlink()
            cr.execute(
                "DELETE FROM marketing_web_ingress_endpoint WHERE id = %s",
                [fixture["endpoint_id"]],
            )
            cr.commit()  # pylint: disable=invalid-commit

    def test_endpoint_deactivation_and_binding_activation_cannot_cross(self):
        fixture = self._fixture(active_binding=False)

        def deactivate_endpoint(env, values):
            return (
                env["marketing.web.ingress.endpoint"]
                .browse(values["endpoint_id"])
                .write({"active": False})
            )

        def activate_binding(env, values):
            return (
                env["marketing.website.ingress.binding"]
                .browse(values["binding_id"])
                .write({"active": True})
            )

        try:
            outcomes = self._race(
                fixture,
                (deactivate_endpoint, activate_binding),
            )
            self.assertEqual(
                sorted(state for state, _value in outcomes),
                ["committed", "rejected"],
            )
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT endpoint.active, binding.active "
                    "FROM marketing_web_ingress_endpoint AS endpoint "
                    "JOIN marketing_website_ingress_binding AS binding "
                    "ON binding.endpoint_id = endpoint.id "
                    "WHERE endpoint.id = %s",
                    [fixture["endpoint_id"]],
                )
                endpoint_active, binding_active = cr.fetchone()
            self.assertFalse(binding_active and not endpoint_active)
        finally:
            self._cleanup(fixture)

    def test_concurrent_redirect_issuers_leave_one_live_capability(self):
        fixture = self._fixture(active_binding=True, with_action=True)
        event_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        now = datetime.datetime.utcnow().replace(microsecond=0)

        def issue(env, values):
            action = env["marketing.website.action"].browse(values["action_id"])
            return env["marketing.website.redirect.grant"]._issue(
                action,
                event_id,
                now=now,
            )

        try:
            outcomes = self._race(fixture, (issue, issue))
            self.assertEqual(
                [state for state, _value in outcomes],
                ["committed", "committed"],
            )
            self.assertEqual(len({value for _state, value in outcomes}), 2)
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT state, count(*) "
                    "FROM marketing_website_redirect_grant "
                    "WHERE action_id = %s GROUP BY state",
                    [fixture["action_id"]],
                )
                counts = dict(cr.fetchall())
            self.assertEqual(counts, {"issued": 1, "revoked": 1})
        finally:
            self._cleanup(fixture)
