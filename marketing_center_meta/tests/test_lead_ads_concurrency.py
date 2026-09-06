import threading
import uuid
from datetime import datetime

from psycopg2 import errorcodes

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
)

from ..services.lead_ads import MetaLead, MetaLeadField
from ..services.tokens import MARKETING_META_LEAD_INTERNAL_TOKEN
from .common import create_meta_profile


@tagged("-at_install", "post_install")
class TestMarketingCenterMetaLeadAdsConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _setup_committed_fixture(self):
        suffix = str(uuid.uuid4().int)[-12:]
        refs = {
            "app": "91%s" % suffix,
            "page": "92%s" % suffix,
            "form": "93%s" % suffix,
            "lead": "94%s" % suffix,
            "ad": "95%s" % suffix,
            "campaign": "96%s" % suffix,
        }
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            app, profile = create_meta_profile(
                env,
                name="Concurrent Lead Ads %s" % suffix,
                external_app_id=refs["app"],
                app_secret_ref="ODOO_META_LEAD_RACE_SECRET_%s" % suffix,
                access_token_ref="ODOO_META_LEAD_RACE_TOKEN_%s" % suffix,
                reader_kind="lead_reader",
            )
            endpoint = env["meta.webhook.endpoint"].create(
                {
                    "name": "Concurrent Lead Ads endpoint %s" % suffix,
                    "app_id": app.id,
                    "credential_backend": "environment",
                    "verify_token_ref": "ODOO_META_LEAD_RACE_VERIFY_%s" % suffix,
                }
            )
            page = env["meta.webhook.page"].create(
                {
                    "name": "Concurrent Lead Ads Page %s" % suffix,
                    "endpoint_id": endpoint.id,
                    "external_page_id": refs["page"],
                    "credential_backend": "environment",
                    "access_token_ref": "ODOO_META_LEAD_RACE_PAGE_%s" % suffix,
                }
            )
            source = env["marketing.center.source"].create(
                {
                    "name": "Concurrent Meta source %s" % suffix,
                    "company_id": env.company.id,
                    "service": "meta.ads",
                    "external_account_ref": "act_97%s" % suffix,
                    "currency_id": env.company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
            route = env["marketing.center.meta.lead.route"].create(
                {
                    "name": "Concurrent Lead Ads route %s" % suffix,
                    "webhook_page_id": page.id,
                    "lead_profile_id": profile.id,
                    "source_id": source.id,
                    "external_form_id": refs["form"],
                }
            )
            submission = env[
                "marketing.center.meta.lead.service"
            ]._find_or_create_submission(
                route,
                leadgen_id=refs["lead"],
                origin="webhook",
                page_id=refs["page"],
                form_id=refs["form"],
                ad_id=refs["ad"],
                legacy_adgroup_id="",
                hint_created_at=datetime(2026, 9, 1, 10, 30),
                hint_sha256="a" * 64,
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                **refs,
                "app_id": app.id,
                "endpoint_id": endpoint.id,
                "page_record_id": page.id,
                "profile_id": profile.id,
                "source_id": source.id,
                "route_id": route.id,
                "route_revision": route.route_revision,
                "profile_revision": profile.profile_revision,
                "app_revision": app.revision,
                "submission_id": submission.id,
            }

    def _cleanup_committed_fixture(self, fixture):
        submission_ids = [fixture["submission_id"]] + fixture.get(
            "additional_submission_ids", []
        )
        with self.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM marketing_center_meta_lead_field "
                "WHERE submission_id = ANY(%s)",
                [submission_ids],
            )
            cr.execute(
                "DELETE FROM marketing_center_meta_lead_submission WHERE id = ANY(%s)",
                [submission_ids],
            )
            cr.execute(
                "DELETE FROM marketing_center_meta_lead_route WHERE id = %s",
                [fixture["route_id"]],
            )
            cr.execute(
                "DELETE FROM meta_webhook_asset WHERE page_id = %s",
                [fixture["page_record_id"]],
            )
            cr.execute(
                "DELETE FROM meta_webhook_page WHERE id = %s",
                [fixture["page_record_id"]],
            )
            cr.execute(
                "DELETE FROM meta_webhook_endpoint WHERE id = %s",
                [fixture["endpoint_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_meta_profile WHERE id = %s",
                [fixture["profile_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source_access_user_rel "
                "WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source WHERE id = %s",
                [fixture["source_id"]],
            )
            cr.execute("DELETE FROM meta_api_app WHERE id = %s", [fixture["app_id"]])
            cr.commit()  # pylint: disable=invalid-commit

    def _find_submission_for_fixture(self, env, fixture, leadgen_id):
        route = env["marketing.center.meta.lead.route"].browse(fixture["route_id"])
        return env["marketing.center.meta.lead.service"]._find_or_create_submission(
            route,
            leadgen_id=leadgen_id,
            origin="webhook",
            page_id=fixture["page"],
            form_id=fixture["form"],
            ad_id=fixture["ad"],
            legacy_adgroup_id="",
            hint_created_at=datetime(2026, 9, 1, 10, 30),
            hint_sha256="a" * 64,
        )

    def test_concurrent_submission_admission_retries_then_reuses_winner(self):
        fixture = self._setup_committed_fixture()
        leadgen_id = fixture["lead"] + "1"
        try:
            with self.registry.cursor() as writer_cr:
                writer = api.Environment(writer_cr, SUPERUSER_ID, {})
                submission = self._find_submission_for_fixture(
                    writer, fixture, leadgen_id
                )
                fixture["additional_submission_ids"] = submission.ids
                winner_id = submission.id
                with self.registry.cursor() as contender_cr:
                    contender_cr.execute("SET LOCAL lock_timeout = '1s'")
                    contender_cr.execute("SET LOCAL statement_timeout = '5s'")
                    contender = api.Environment(contender_cr, SUPERUSER_ID, {})
                    with self.assertRaises(MarketingSerializationFailure) as caught:
                        self._find_submission_for_fixture(
                            contender, fixture, leadgen_id
                        )
                    self.assertEqual(
                        caught.exception.pgcode, errorcodes.SERIALIZATION_FAILURE
                    )
                    contender_cr.rollback()
                writer_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retried = self._find_submission_for_fixture(
                    api.Environment(retry_cr, SUPERUSER_ID, {}), fixture, leadgen_id
                )
                self.assertEqual(retried.id, winner_id)
                self.assertEqual(
                    retried.search_count(
                        [
                            ("meta_app_id", "=", fixture["app_id"]),
                            ("leadgen_id", "=", leadgen_id),
                        ]
                    ),
                    1,
                )
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_committed_submission_outside_snapshot_requests_full_retry(self):
        fixture = self._setup_committed_fixture()
        leadgen_id = fixture["lead"] + "2"
        try:
            with self.registry.cursor() as stale_cr:
                stale = api.Environment(stale_cr, SUPERUSER_ID, {})
                domain = [
                    ("meta_app_id", "=", fixture["app_id"]),
                    ("leadgen_id", "=", leadgen_id),
                ]
                # Fix a REPEATABLE READ snapshot before the independent winner
                # commits. The advisory key is free by the time we attempt the
                # insert, but another search still cannot see that winner.
                self.assertFalse(
                    stale["marketing.center.meta.lead.submission"].search(domain)
                )
                with self.registry.cursor() as winner_cr:
                    winner = self._find_submission_for_fixture(
                        api.Environment(winner_cr, SUPERUSER_ID, {}),
                        fixture,
                        leadgen_id,
                    )
                    fixture["additional_submission_ids"] = winner.ids
                    winner_id = winner.id
                    winner_cr.commit()  # pylint: disable=invalid-commit
                with mute_logger("odoo.sql_db"), self.assertRaises(
                    MarketingSerializationFailure
                ) as caught:
                    self._find_submission_for_fixture(stale, fixture, leadgen_id)
                self.assertEqual(
                    caught.exception.pgcode, errorcodes.SERIALIZATION_FAILURE
                )
                stale_cr.rollback()

            with self.registry.cursor() as retry_cr:
                retried = self._find_submission_for_fixture(
                    api.Environment(retry_cr, SUPERUSER_ID, {}), fixture, leadgen_id
                )
                self.assertEqual(retried.id, winner_id)
                self.assertEqual(retried.search_count(domain), 1)
        finally:
            self._cleanup_committed_fixture(fixture)

    def _hold_webhook_claim(self, fixture, locked, release, errors):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                submission = env["marketing.center.meta.lead.submission"].browse(
                    fixture["submission_id"]
                )
                cr.execute(
                    "SELECT id FROM marketing_center_meta_lead_submission "
                    "WHERE id = %s FOR UPDATE",
                    [submission.id],
                )
                submission.with_context(
                    marketing_meta_lead_internal_token=(
                        MARKETING_META_LEAD_INTERNAL_TOKEN
                    )
                ).write({"state": "processing"})
                locked.set()
                if not release.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("The reconciliation worker did not finish.")
                cr.rollback()
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            locked.set()

    def test_pull_skips_submission_claimed_by_webhook_worker(self):
        fixture = self._setup_committed_fixture()
        locked = threading.Event()
        release = threading.Event()
        errors = []
        worker = threading.Thread(
            target=self._hold_webhook_claim,
            args=(fixture, locked, release, errors),
            name="marketing-meta-webhook-lead-claim",
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(
                locked.wait(timeout=self.WORKER_TIMEOUT_SECONDS),
                "The webhook worker did not acquire its submission claim.",
            )
            if errors:
                raise errors[0]
            lead = MetaLead(
                leadgen_id=fixture["lead"],
                form_id=fixture["form"],
                ad_id=fixture["ad"],
                campaign_id=fixture["campaign"],
                created_at=datetime(2026, 9, 1, 10, 30),
                fields=(MetaLeadField("email", ("private@example.com",)),),
                payload_sha256="b" * 64,
            )
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '1s'")
                cr.execute("SET LOCAL statement_timeout = '5s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                route = env["marketing.center.meta.lead.route"].browse(
                    fixture["route_id"]
                )
                result = env[
                    "marketing.center.meta.lead.service"
                ]._project_reconcile_leads(
                    route,
                    (lead,),
                    expected_route_revision=fixture["route_revision"],
                    expected_profile_revision=fixture["profile_revision"],
                    expected_app_revision=fixture["app_revision"],
                )
                self.assertTrue(result)
                cr.commit()  # pylint: disable=invalid-commit
        finally:
            release.set()
            worker.join(self.WORKER_TIMEOUT_SECONDS)
            try:
                if worker.is_alive():
                    self.fail("The webhook claim worker did not finish.")
                if errors:
                    raise errors[0]
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    submission = env["marketing.center.meta.lead.submission"].browse(
                        fixture["submission_id"]
                    )
                    self.assertEqual(submission.state, "pending")
                    self.assertFalse(submission.field_ids)
                    self.assertFalse(submission.touchpoint_id)
            finally:
                self._cleanup_committed_fixture(fixture)
