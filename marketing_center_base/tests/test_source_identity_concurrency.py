import threading
import time
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install")
class TestMarketingSourceIdentityConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _setup_committed_source(self):
        suffix = str(uuid.uuid4().int)[-12:]
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env["res.company"].browse(self.env.company.id)
            source = env["marketing.center.source"].create(
                {
                    "name": "Identity race source %s" % suffix,
                    "company_id": company.id,
                    "service": "test.identity",
                    "external_account_ref": "identity-%s" % suffix,
                    "currency_id": company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {"source_id": source.id, "suffix": suffix}

    def _cleanup_committed_source(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM marketing_center_connection WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source WHERE id = %s",
                [fixture["source_id"]],
            )
            cr.commit()  # pylint: disable=invalid-commit

    def _wait_until_lock_wait(self, application_name):
        deadline = time.monotonic() + self.WORKER_TIMEOUT_SECONDS
        with self.registry.cursor() as cr:
            while time.monotonic() < deadline:
                cr.execute("SELECT pg_stat_clear_snapshot()")
                cr.execute(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE application_name = %s",
                    [application_name],
                )
                row = cr.fetchone()
                if row and row[0] == "Lock":
                    return
                time.sleep(0.02)
        self.fail("The source identity writer did not wait for the evidence lock.")

    def _create_locked_evidence(self, fixture, locked, release, errors):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                source = env["marketing.center.source"].browse(fixture["source_id"])
                source._lock_identity_scope()
                locked.set()
                if not release.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("The evidence transaction was not released.")
                env["marketing.center.connection"].create(
                    {
                        "name": "Identity evidence %s" % fixture["suffix"],
                        "source_id": source.id,
                        "adapter_key": "test.identity",
                        "purpose": "reader",
                        "profile_public_ref": "profile-%s" % fixture["suffix"],
                        "profile_revision": 1,
                        "state": "ready",
                    }
                )
                cr.commit()  # pylint: disable=invalid-commit
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            locked.set()

    def _change_identity(self, fixture, application_name, started, outcomes, errors):
        try:
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    cr.execute(
                        "SELECT set_config('application_name', %s, true)",
                        [application_name],
                    )
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    source = env["marketing.center.source"].browse(fixture["source_id"])
                    started.set()
                    source.write({"external_account_ref": "changed-identity"})
                    cr.commit()  # pylint: disable=invalid-commit
                    outcomes.append("changed")
                    return
            except SerializationFailure:
                outcomes.append("serialization_retry")

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                source = env["marketing.center.source"].browse(fixture["source_id"])
                try:
                    source.write({"external_account_ref": "changed-identity"})
                except AccessError:
                    cr.rollback()
                    outcomes.append("rejected")
                else:
                    cr.commit()  # pylint: disable=invalid-commit
                    outcomes.append("changed_after_retry")
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            started.set()

    def test_committed_evidence_wins_over_waiting_identity_change(self):
        fixture = self._setup_committed_source()
        locked = threading.Event()
        release = threading.Event()
        writer_started = threading.Event()
        errors = []
        outcomes = []
        application_name = "mc-source-writer-%s" % fixture["suffix"]
        evidence = threading.Thread(
            target=self._create_locked_evidence,
            args=(fixture, locked, release, errors),
            name="marketing-source-evidence",
            daemon=True,
        )
        writer = threading.Thread(
            target=self._change_identity,
            args=(fixture, application_name, writer_started, outcomes, errors),
            name="marketing-source-identity-writer",
            daemon=True,
        )
        try:
            evidence.start()
            self.assertTrue(
                locked.wait(timeout=self.WORKER_TIMEOUT_SECONDS),
                "The evidence transaction did not lock the source.",
            )
            writer.start()
            self.assertTrue(
                writer_started.wait(timeout=self.WORKER_TIMEOUT_SECONDS),
                "The identity writer did not start.",
            )
            self._wait_until_lock_wait(application_name)
            release.set()
            evidence.join(self.WORKER_TIMEOUT_SECONDS)
            writer.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertFalse(evidence.is_alive())
            self.assertFalse(writer.is_alive())
            if errors:
                raise errors[0]
            self.assertIn("rejected", outcomes)
            self.assertNotIn("changed", outcomes)
            self.assertNotIn("changed_after_retry", outcomes)
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT external_account_ref FROM marketing_center_source "
                    "WHERE id = %s",
                    [fixture["source_id"]],
                )
                self.assertEqual(cr.fetchone()[0], "identity-%s" % fixture["suffix"])
        finally:
            release.set()
            evidence.join(self.WORKER_TIMEOUT_SECONDS)
            writer.join(self.WORKER_TIMEOUT_SECONDS)
            self._cleanup_committed_source(fixture)
