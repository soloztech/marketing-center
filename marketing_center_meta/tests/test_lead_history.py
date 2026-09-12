import datetime
import uuid
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.lead_ads import MetaLead, MetaLeadPage
from .common import create_meta_profile


class TestMarketingMetaLeadHistory(SavepointCase):
    NOW = datetime.datetime(2026, 9, 12, 12)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app, cls.profile = create_meta_profile(
            cls.env,
            name="Historical leads",
            external_app_id="710000000000001",
            app_secret_ref="ODOO_HISTORY_APP_SECRET",
            access_token_ref="ODOO_HISTORY_READER_TOKEN",
            reader_kind="lead_reader",
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "History endpoint",
                "app_id": cls.app.id,
                "verify_token_ref": "ODOO_HISTORY_VERIFY_TOKEN",
            }
        )
        cls.page = cls.env["meta.webhook.page"].create(
            {
                "name": "History page",
                "endpoint_id": cls.endpoint.id,
                "external_page_id": "710000000000002",
                "access_token_ref": "ODOO_HISTORY_PAGE_TOKEN",
            }
        )
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "History source",
                "service": "meta.ads",
                "external_account_ref": "act_710000000000003",
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.route = cls.env["marketing.center.meta.lead.route"].create(
            {
                "name": "History form",
                "webhook_page_id": cls.page.id,
                "lead_profile_id": cls.profile.id,
                "source_id": cls.source.id,
                "external_form_id": "710000000000004",
            }
        )
        cls.History = cls.env["marketing.center.meta.lead.history"]

    def _request(self, **values):
        return self.History.create(dict(route_id=self.route.id, **values))

    def _start(self, request):
        with patch(
            "odoo.fields.Datetime.now", return_value=self.NOW
        ), trap_jobs() as jobs:
            request.action_sync()
            jobs.assert_jobs_count(1)

    def _lead(self, offset=10, lead_id="710000000000005"):
        return MetaLead(
            leadgen_id=lead_id,
            form_id=self.route.external_form_id,
            ad_id="",
            campaign_id="",
            created_at=self.NOW - datetime.timedelta(days=offset),
            fields=(),
            payload_sha256="a" * 64,
        )

    def _run_page(self, page):
        route = self.route
        with patch(
            "odoo.addons.marketing_center_meta.models.lead_ads.MetaMarketingReadAdapter"
        ) as adapter, trap_jobs():
            adapter.return_value.fetch_lead_page.return_value = page
            result = route.with_context(
                job_uuid=route.reconcile_job_uuid
            )._job_reconcile_meta_leads_page(
                expected_route_revision=route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
                since=fields.Datetime.to_string(route.reconcile_since),
                after=route.reconcile_after or "",
                page_number=route.reconcile_page_count,
            )
        return result, adapter.return_value.fetch_lead_page.call_args

    def test_explicit_period_ignores_watermark_and_preserves_it_after_completion(self):
        watermark = self.NOW - datetime.timedelta(days=1)
        self.route._runtime_write({"last_reconciled_at": watermark})
        request = self._request(period="30")
        self._start(request)
        cutoff = self.NOW - datetime.timedelta(days=30)
        self.assertEqual(self.route.reconcile_since, cutoff)
        self.assertEqual(request.requested_since, cutoff)
        self.assertEqual(request.requested_at, self.NOW)
        self.assertEqual(request.requested_by, self.env.user)
        result, call = self._run_page(MetaLeadPage((self._lead(),), "", False))
        self.assertTrue(result["done"])
        self.assertEqual(call.kwargs["since"], cutoff)
        self.assertEqual(request.state, "done")
        self.assertFalse(self.route.history_request_id)
        self.assertEqual(self.route.last_reconciled_at, watermark)
        self.assertEqual(
            self.route._new_reconcile_since(),
            watermark - datetime.timedelta(minutes=15),
        )

    def test_history_does_not_advance_an_older_gap_or_missing_watermark(self):
        for previous in (False, self.NOW - datetime.timedelta(days=60)):
            with self.subTest(previous=previous):
                self.route._runtime_write({"last_reconciled_at": previous})
                request = self._request(period="7")
                self._start(request)
                self._run_page(MetaLeadPage((), "", False))
                self.assertEqual(self.route.last_reconciled_at, previous)

    def test_repeated_history_deduplicates_and_excludes_outside_selected_period(self):
        leads = (
            self._lead(),
            self._lead(offset=31, lead_id="710000000000006"),
            self._lead(offset=-1, lead_id="710000000000007"),
        )
        for _attempt in range(2):
            self._start(self._request(period="30"))
            self._run_page(MetaLeadPage(leads, "", False))
        submissions = self.env["marketing.center.meta.lead.submission"].search(
            [
                ("route_id", "=", self.route.id),
            ]
        )
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions.leadgen_id, leads[0].leadgen_id)
        self.assertEqual(submissions.state, "ingested")
        self.assertEqual(
            len(self.route.history_ids.filtered(lambda row: row.state == "done")), 2
        )

    def test_partial_chunk_and_error_resume_keep_explicit_cutoff(self):
        request = self._request(period="90")
        self._start(request)
        cutoff = request.requested_since
        self.route._runtime_write({"reconcile_page_count": 9})
        self._run_page(MetaLeadPage((), "page-11", True))
        self.assertEqual(request.state, "partial")
        self.assertEqual(self.route.reconcile_after, "page-11")
        with trap_jobs() as jobs:
            self.route.action_enqueue_reconciliation()
            jobs.assert_jobs_count(1)
        self.assertEqual(self.route.reconcile_since, cutoff)
        self.assertEqual(self.route.reconcile_after, "page-11")
        self.env["marketing.center.meta.lead.service"]._finish_reconcile_error(
            self.route, "TestRetry"
        )
        self.assertEqual(request.state, "error")
        with trap_jobs():
            self.route.action_enqueue_reconciliation()
        self.assertEqual(self.route.reconcile_since, cutoff)
        self.assertEqual(self.route.reconcile_after, "page-11")
        self._run_page(MetaLeadPage((), "", False))
        self.assertEqual(request.state, "done")

    def test_lifecycle_pause_can_resume_history_without_replacing_cutoff(self):
        request = self._request(period="30")
        self._start(request)
        self.route.write({"reconcile_enabled": False})
        self.assertEqual(request.state, "paused")
        self.assertFalse(self.route.reconcile_since)
        self.route.write({"reconcile_enabled": True})
        with trap_jobs():
            self.route.action_enqueue_reconciliation()
        self.assertEqual(self.route.reconcile_since, request.requested_since)
        self.assertEqual(request.state, "queued")

    def test_active_job_and_partial_sweep_prevent_history_replacement(self):
        job = Job(
            self.route._job_reconcile_meta_leads_page,
            kwargs={
                "expected_route_revision": self.route.route_revision,
                "expected_profile_revision": self.profile.profile_revision,
                "expected_app_revision": self.app.revision,
                "since": fields.Datetime.to_string(self.NOW),
                "after": "",
                "page_number": 0,
            },
        )
        job.store()
        self.route._runtime_write(
            {"reconcile_job_uuid": job.uuid, "reconcile_state": "queued"}
        )
        with self.assertRaises(UserError):
            self._request(period="7").action_sync()
        with trap_jobs() as jobs:
            self.route.action_enqueue_reconciliation()
            jobs.assert_jobs_count(0)
        self.assertEqual(self.route.reconcile_job_uuid, job.uuid)
        self.route._runtime_write(
            {"reconcile_job_uuid": False, "reconcile_state": "partial"}
        )
        with self.assertRaises(UserError):
            self._request(period="7").action_sync()

    def test_second_request_cannot_replace_queued_history(self):
        first = self._request(period="7")
        self._start(first)
        with self.assertRaises(UserError):
            self._request(period="30").action_sync()
        self.assertEqual(self.route.history_request_id, first)
        self.assertEqual(self.route.reconcile_since, first.requested_since)

    def test_custom_start_uses_user_timezone_and_rejects_invalid_dates(self):
        request = self._request(period="custom", start_date="2026-09-01").with_context(
            tz="America/Sao_Paulo"
        )
        self.assertEqual(request._cutoff(self.NOW), datetime.datetime(2026, 9, 1, 3))
        for start in (False, "2026-01-01", "2026-09-13"):
            request.write({"start_date": start})
            with self.subTest(start=start), self.assertRaises(ValidationError):
                request._cutoff(self.NOW)

    def test_initial_period_preserves_legacy_and_new_only_starts_at_creation(self):
        self.route.write({"reconcile_lookback_hours": 1})
        self.assertEqual(self.route.initial_sync_period, "legacy")
        self.assertEqual(self.route.reconcile_lookback_hours, 1)
        self.route.write({"initial_sync_period": "30"})
        self.assertEqual(self.route.reconcile_lookback_hours, 720)
        with patch("odoo.fields.Datetime.now", return_value=self.NOW):
            new_route = self.env["marketing.center.meta.lead.route"].create(
                {
                    "name": "Only new entries",
                    "webhook_page_id": self.page.id,
                    "lead_profile_id": self.profile.id,
                    "source_id": self.source.id,
                    "external_form_id": "710000000000099",
                    "initial_sync_period": "new",
                }
            )
        self.assertEqual(new_route.initial_sync_period, "new")
        self.assertEqual(new_route.initial_sync_started_at, self.NOW)
        self.assertEqual(new_route._new_reconcile_since(), self.NOW)
        self.assertFalse(new_route.last_reconciled_at)
        new_route._runtime_write(
            {"last_reconciled_at": self.NOW + datetime.timedelta(minutes=5)}
        )
        self.assertEqual(new_route._new_reconcile_since(), self.NOW)

    def test_initial_period_internal_fields_and_legacy_cannot_be_forged(self):
        values = {
            "name": "Protected start",
            "webhook_page_id": self.page.id,
            "lead_profile_id": self.profile.id,
            "source_id": self.source.id,
            "external_form_id": "710000000000098",
            "initial_sync_period": "new",
        }
        for field, value in (
            ("initial_sync_new_only", True),
            ("initial_sync_started_at", False),
        ):
            with self.subTest(field=field):
                with self.assertRaises(AccessError):
                    self.route.create(dict(values, **{field: value}))
                with self.assertRaises(AccessError):
                    self.route.with_context(marketing_lead_period_token=True).write(
                        {field: value}
                    )
                with self.assertRaises(AccessError):
                    self.route.with_context(**{"default_%s" % field: value}).create(
                        values
                    )
        with self.assertRaises(ValidationError):
            self.route.write({"initial_sync_period": "legacy"})
        with self.assertRaises(ValidationError):
            self.route.create(dict(values, initial_sync_period="legacy"))
        self.assertFalse(self.route.initial_sync_new_only)
        self.assertFalse(self.route.initial_sync_started_at)

    def test_initial_period_cannot_change_after_collection_started(self):
        self.route._runtime_write({"last_reconciled_at": self.NOW})
        with self.assertRaises(UserError):
            self.route.write({"initial_sync_period": "30"})
        self.route._runtime_write({"last_reconciled_at": False})
        self._start(self._request(period="7"))
        with self.assertRaises(UserError):
            self.route.write({"initial_sync_period": "90"})

    def test_audit_fields_are_not_forgeable_and_sent_request_is_immutable(self):
        for values in (
            {"state": "done"},
            {"requested_at": self.NOW},
            {"requested_by": self.env.uid},
        ):
            with self.subTest(values=values), self.assertRaises(AccessError):
                self._request(period="7", **values)
        with self.assertRaises(AccessError):
            self.History.with_context(default_state="done").create(
                {"route_id": self.route.id}
            )
        request = self._request(period="7")
        self._start(request)
        with self.assertRaises(AccessError):
            request.write({"period": "30"})
        with self.assertRaises(AccessError):
            request.with_context(marketing_lead_history_token=True).write(
                {"state": "done"}
            )
        with self.assertRaises(AccessError):
            request.unlink()
        with self.assertRaises(UserError):
            request.action_sync()

    def test_admin_and_explicit_company_scope_are_required(self):
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "History viewer",
                    "login": "history-viewer-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [
                        Command.set(
                            self.env.ref(
                                "marketing_center_base.group_marketing_center_viewer"
                            ).ids
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.History.with_user(viewer).create({"route_id": self.route.id})
        other = self.env["res.company"].create({"name": "History other company"})
        with self.assertRaises(AccessError):
            self.route.with_context(
                allowed_company_ids=other.ids
            ).action_open_history_sync()
        request = self._request(period="7")
        with self.assertRaises(AccessError):
            request.with_user(viewer).action_sync()
        with self.assertRaises(AccessError):
            request.with_context(allowed_company_ids=other.ids).action_sync()
