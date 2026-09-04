import hashlib
import json
import uuid
from datetime import datetime
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.meta_webhook_base.services.sanitizer import sanitized_webhook
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.lead_ads import MetaLead, MetaLeadField, MetaLeadPage
from ..services.tokens import (
    MARKETING_META_LEAD_INTERNAL_TOKEN,
    MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN,
)
from .common import create_meta_profile


class TestMarketingCenterMetaLeadAds(SavepointCase):
    PAGE_ID = "100000000000101"
    FORM_ID = "300000000000001"
    LEAD_ID = "200000000000001"
    AD_ID = "400000000000001"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app, cls.profile = create_meta_profile(
            cls.env,
            name="Meta Lead Ads laboratory",
            external_app_id="100000000000001",
            app_secret_ref="ODOO_META_LEADS_APP_SECRET",
            access_token_ref="ODOO_META_LEADS_READER_TOKEN",
            reader_kind="lead_reader",
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "Meta Lead Ads endpoint",
                "app_id": cls.app.id,
                "credential_backend": "environment",
                "verify_token_ref": "ODOO_META_LEADS_VERIFY_TOKEN",
            }
        )
        cls.page = cls.env["meta.webhook.page"].create(
            {
                "name": "Meta Lead Ads Page",
                "endpoint_id": cls.endpoint.id,
                "external_page_id": cls.PAGE_ID,
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_LEADS_PAGE_TOKEN",
            }
        )
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Meta Lead Ads source",
                "company_id": cls.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_%s" % uuid.uuid4().int,
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.route = cls.env["marketing.center.meta.lead.route"].create(
            {
                "name": "Soloz Lead Ads",
                "webhook_page_id": cls.page.id,
                "lead_profile_id": cls.profile.id,
                "source_id": cls.source.id,
                "external_form_id": cls.FORM_ID,
            }
        )
        cls.subscription = cls.env["meta.webhook.subscription"].create(
            {
                "page_id": cls.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )

    def _envelope(self, **overrides):
        value = {
            "leadgen_id": self.LEAD_ID,
            "page_id": self.PAGE_ID,
            "form_id": self.FORM_ID,
            "created_time": 1_788_259_800,
            "ad_id": self.AD_ID,
            "adgroup_id": "500000000000001",
        }
        value.update(overrides)
        return {
            "object": "page",
            "entry": [
                {
                    "id": self.PAGE_ID,
                    "time": 1_788_259_800,
                    "changes": [{"field": "leadgen", "value": value}],
                }
            ],
        }

    def _lead(self, **overrides):
        values = {
            "leadgen_id": self.LEAD_ID,
            "form_id": self.FORM_ID,
            "ad_id": self.AD_ID,
            "campaign_id": "600000000000001",
            "created_at": datetime(2026, 9, 1, 10, 30),
            "fields": (
                MetaLeadField("email", ("person@example.com",)),
                MetaLeadField("phone_number", ("+55 19 99999-0000",)),
                MetaLeadField("full_name", ("Private Person",)),
            ),
            "payload_sha256": "a" * 64,
        }
        values.update(overrides)
        return MetaLead(**values)

    def _delivery(self, envelope, salt=""):
        sanitized = sanitized_webhook(envelope)
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
        digest = hashlib.sha256(body + salt.encode()).hexdigest()
        delivery = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
            .create(
                {
                    "endpoint_id": self.endpoint.id,
                    "endpoint_revision": self.endpoint.revision,
                    "app_revision": self.app.revision,
                    "content_sha256": digest,
                    "body_size_bytes": len(body),
                    "object_type": sanitized.object_type,
                    "graph_version": self.app.graph_version,
                    "sanitized_envelope_json": sanitized.envelope,
                }
            )
        )
        self.env["meta.webhook.dispatcher"]._ingest_delivery(
            self.endpoint, delivery, envelope, sanitized
        )
        return delivery

    def _dispatch_hint(self, envelope=None, salt=""):
        delivery = self._delivery(envelope or self._envelope(), salt=salt)
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        with trap_jobs() as jobs:
            self._run_dispatch_job(dispatch)
            jobs.assert_jobs_count(1)
        return dispatch, self.env["marketing.center.meta.lead.submission"].search(
            [("leadgen_id", "=", self.LEAD_ID)]
        )

    def _run_dispatch_job(self, dispatch):
        dispatch.invalidate_recordset(["queue_job_uuid"])
        job_uuid = dispatch.queue_job_uuid
        self.assertTrue(job_uuid)
        return (
            dispatch.sudo()
            .with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
                job_uuid=job_uuid,
            )
            ._job_process()
        )

    def _run_submission_job(self, submission):
        return submission.with_context(
            job_uuid=submission.queue_job_uuid
        )._job_fetch_meta_lead(
            expected_route_revision=self.route.route_revision,
            expected_profile_revision=self.profile.profile_revision,
            expected_app_revision=self.app.revision,
        )

    def test_callback_is_only_a_durable_hint_then_enqueues_authenticated_get(self):
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_ads.graph_request"
        ) as graph_request:
            dispatch, submission = self._dispatch_hint()
        graph_request.assert_not_called()
        self.assertEqual(dispatch.state, "done")
        self.assertEqual(submission.state, "pending")
        self.assertEqual(submission.form_id_hint, self.FORM_ID)
        self.assertEqual(submission.first_dispatch_id, dispatch)
        self.assertFalse(submission.field_ids)
        self.assertFalse(submission.touchpoint_id)
        serialized = str(submission.read()[0])
        self.assertNotIn("Private Person", serialized)

    def test_authenticated_get_projects_private_fields_and_touchpoint_once(self):
        _dispatch, submission = self._dispatch_hint()
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.fetch_lead.return_value = self._lead()
            result = self._run_submission_job(submission)
        self.assertEqual(result.touchpoint_id, submission.touchpoint_id.id)
        self.assertEqual(submission.state, "ingested")
        self.assertEqual(submission.field_count, 3)
        self.assertEqual(len(submission.field_ids), 3)
        touchpoint = submission.touchpoint_id
        self.assertEqual(touchpoint.source_system, "meta.lead_ads")
        self.assertEqual(touchpoint.touchpoint_type, "lead_ad")
        self.assertEqual(touchpoint.asset_refs_json["meta.form_id"], self.FORM_ID)
        self.assertEqual(
            touchpoint.asset_refs_json["meta.campaign_id"], "600000000000001"
        )
        self.assertNotIn("meta.adset_id", touchpoint.asset_refs_json)
        self.assertEqual(len(touchpoint.identifier_ids), 2)
        self.assertNotIn("person@example.com", str(touchpoint.read()[0]))

        duplicate = self.env["marketing.attribution.service"]._ingest_touchpoint(
            submission.company_id,
            self.env["marketing.center.meta.lead.service"]._lead_touchpoint(
                submission, self._lead()
            ),
        )
        self.assertEqual(duplicate.disposition, "duplicate")

    def test_replayed_hint_converges_on_one_submission(self):
        self._dispatch_hint()
        second_delivery = self._delivery(
            self._envelope(created_time=1_788_259_801), "2"
        )
        with trap_jobs():
            second_delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        second = second_delivery.dispatch_ids.ensure_one()
        with trap_jobs():
            self._run_dispatch_job(second)
        submissions = self.env["marketing.center.meta.lead.submission"].search(
            [("leadgen_id", "=", self.LEAD_ID)]
        )
        self.assertEqual(len(submissions), 1)

    def test_webhook_and_pull_converge_by_leadgen_id(self):
        _dispatch, webhook_submission = self._dispatch_hint()
        pull_submission = self.env[
            "marketing.center.meta.lead.service"
        ]._find_or_create_submission_from_lead(self.route, self._lead())
        self.assertEqual(webhook_submission, pull_submission)
        self.assertEqual(
            self.env["marketing.center.meta.lead.submission"].search_count(
                [("leadgen_id", "=", self.LEAD_ID)]
            ),
            1,
        )

    def test_conflicting_replay_is_quarantined(self):
        _dispatch, submission = self._dispatch_hint()
        service = self.env["marketing.center.meta.lead.service"]
        replay = service._find_or_create_submission(
            self.route,
            leadgen_id=self.LEAD_ID,
            origin="webhook",
            page_id=self.PAGE_ID,
            form_id=self.FORM_ID,
            ad_id="400000000000099",
            legacy_adgroup_id="",
            hint_created_at=datetime(2026, 9, 1, 10, 31),
            hint_sha256="b" * 64,
        )
        self.assertEqual(replay, submission)
        self.assertEqual(submission.state, "review")
        self.assertEqual(submission.last_error_class, "WebhookHintConflict")

        service._project_reconcile_leads(
            self.route,
            (self._lead(),),
            expected_route_revision=self.route.route_revision,
            expected_profile_revision=self.profile.profile_revision,
            expected_app_revision=self.app.revision,
        )
        self.assertEqual(submission.state, "review")
        self.assertEqual(submission.last_error_class, "WebhookHintConflict")
        self.assertFalse(submission.field_ids)
        self.assertFalse(submission.touchpoint_id)

    def test_wrong_form_is_strictly_unrouted(self):
        delivery = self._delivery(self._envelope(form_id="300000000000099"), "wrong")
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        self._run_dispatch_job(dispatch)
        self.assertEqual(dispatch.state, "unrouted")
        self.assertFalse(
            self.env["marketing.center.meta.lead.submission"].search(
                [("leadgen_id", "=", self.LEAD_ID)]
            )
        )

    def test_new_route_recovers_an_unrouted_durable_dispatch(self):
        delayed_form = "300000000000099"
        delivery = self._delivery(self._envelope(form_id=delayed_form), "delayed")
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        self._run_dispatch_job(dispatch)
        self.assertEqual(dispatch.state, "unrouted")
        self.env["marketing.center.meta.lead.route"].create(
            {
                "name": "Delayed Lead Ads route",
                "webhook_page_id": self.page.id,
                "lead_profile_id": self.profile.id,
                "source_id": self.source.id,
                "external_form_id": delayed_form,
            }
        )
        with trap_jobs() as jobs:
            self.env["meta.webhook.dispatcher"]._after_subscription_reconcile(
                self.endpoint
            )
            jobs.assert_jobs_count(1)
        self.assertEqual(dispatch.state, "pending")
        self.assertTrue(dispatch.queue_job_uuid)

    def test_authenticated_route_conflict_goes_to_review_without_touchpoint(self):
        _dispatch, submission = self._dispatch_hint()
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.fetch_lead.return_value = self._lead(
                form_id="300000000000099"
            )
            result = self._run_submission_job(submission)
        self.assertFalse(result)
        self.assertEqual(submission.state, "review")
        self.assertEqual(submission.last_error_class, "RouteMismatch")
        self.assertFalse(submission.touchpoint_id)

    def test_managerial_view_cannot_read_private_answer_rows(self):
        _dispatch, submission = self._dispatch_hint()
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.fetch_lead.return_value = self._lead()
            self._run_submission_job(submission)
        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        admin_group = self.env.ref("marketing_center_base.group_marketing_center_admin")
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Lead Ads viewer",
                    "login": "lead-ads-viewer-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(viewer_group.ids)],
                }
            )
        )
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Lead Ads outsider",
                    "login": "lead-ads-outsider-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(viewer_group.ids)],
                }
            )
        )
        marketing_admin = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Lead Ads company administrator",
                    "login": "lead-ads-admin-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(admin_group.ids)],
                }
            )
        )
        team = self.env["marketing.center.team"].create(
            {"name": "Lead Ads viewer roster", "company_id": self.env.company.id}
        )
        membership = self.env["marketing.center.team.member"].create(
            {
                "team_id": team.id,
                "user_id": viewer.id,
                "role": "viewer",
            }
        )
        source_link = self.env["marketing.center.team.source"].create(
            {
                "team_id": team.id,
                "source_id": self.source.id,
                "access_mode": "read",
            }
        )
        safe = submission.with_user(viewer).read()[0]
        self.assertNotIn("field_ids", safe)
        self.assertNotIn("first_dispatch_id", safe)
        self.assertNotIn("person@example.com", str(safe))
        with self.assertRaises(AccessError):
            self.env["marketing.center.meta.lead.field"].with_user(
                viewer
            ).check_access_rights("read")
        self.assertFalse(
            self.env["marketing.center.meta.lead.submission"]
            .with_user(outsider)
            .search([("id", "=", submission.id)])
        )
        self.assertEqual(
            self.env["marketing.center.meta.lead.submission"]
            .with_user(marketing_admin)
            .search([("id", "=", submission.id)])
            .ids,
            submission.ids,
        )

        source_link.write({"active": False})
        self.assertFalse(
            self.env["marketing.center.meta.lead.route"]
            .with_user(viewer)
            .search([("id", "=", self.route.id)])
        )
        self.assertFalse(
            self.env["marketing.center.meta.lead.submission"]
            .with_user(viewer)
            .search([("id", "=", submission.id)])
        )
        source_link.write({"active": True})
        membership.write({"active": False})
        self.assertFalse(
            self.env["marketing.center.meta.lead.submission"]
            .with_user(viewer)
            .search([("id", "=", submission.id)])
        )

    def test_route_requires_explicit_lead_reader_and_is_immutable(self):
        ads_profile = self.env["marketing.center.meta.profile"].create(
            {
                "name": "Ads-only profile",
                "meta_app_id": self.app.id,
                "reader_kind": "ads_reader",
                "access_token_ref": "ODOO_META_ADS_ONLY_READER_TOKEN",
            }
        )
        with self.assertRaises(ValidationError):
            self.env["marketing.center.meta.lead.route"].create(
                {
                    "name": "Invalid profile",
                    "webhook_page_id": self.page.id,
                    "lead_profile_id": ads_profile.id,
                    "source_id": self.source.id,
                    "external_form_id": "300000000000099",
                }
            )
        with self.assertRaises(AccessError):
            self.route.write({"external_form_id": "300000000000099"})
        with self.assertRaises(AccessError):
            self.route.unlink()

    def test_route_freezes_source_and_its_external_identity(self):
        source = self.env["marketing.center.source"].create(
            {
                "name": "Meta Lead Ads route source",
                "company_id": self.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_%s" % uuid.uuid4().int,
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        self.assertIn(
            ("marketing.center.meta.lead.route", "source_id", ()),
            self.source._identity_evidence_registry(),
        )
        with self.assertRaises(AccessError):
            self.route.write({"source_id": source.id})
        with self.assertRaises(AccessError):
            self.source.write({"external_account_ref": "act_reassigned"})

    def test_new_route_requires_an_explicit_source(self):
        with self.assertRaises(ValidationError):
            self.env["marketing.center.meta.lead.route"].create(
                {
                    "name": "Source-less route",
                    "webhook_page_id": self.page.id,
                    "lead_profile_id": self.profile.id,
                    "external_form_id": "300000000000098",
                }
            )

    def test_reconciliation_cron_scopes_each_route_to_its_company(self):
        other_company = self.env["res.company"].create(
            {"name": "Lead Ads scheduler caller %s" % uuid.uuid4()}
        )
        observed = []

        def inspect_scope(route):
            route.ensure_one()
            observed.append(
                (
                    route.company_id.id,
                    route.env.company.id,
                    route.env.companies.ids,
                )
            )
            return False

        route_class = type(self.route)
        caller = (
            self.env["marketing.center.meta.lead.route"]
            .with_company(other_company)
            .with_context(allowed_company_ids=[other_company.id])
        )
        with patch.object(
            route_class,
            "_enqueue_reconciliation",
            autospec=True,
            side_effect=inspect_scope,
        ):
            caller._cron_enqueue_reconciliation(limit=100)

        own_scope = next(
            values for values in observed if values[0] == self.route.company_id.id
        )
        self.assertEqual(own_scope[0], own_scope[1])
        self.assertEqual(own_scope[2], [own_scope[0]])

    def test_reconciliation_job_reanchors_the_route_company(self):
        caller_company = self.env["res.company"].create(
            {"name": "Unrelated Lead Ads job company %s" % uuid.uuid4()}
        )
        observed = []

        def inspect_scope(service, route, **_kwargs):
            observed.append(
                (
                    service.env.company.id,
                    service.env.companies.ids,
                    route.env.company.id,
                    route.env.companies.ids,
                    route.company_id.id,
                )
            )
            return {"continued": False}

        service_class = type(self.env["marketing.center.meta.lead.service"])
        wrong_scope = (
            self.route.sudo()
            .with_company(caller_company)
            .with_context(allowed_company_ids=[caller_company.id])
        )
        with patch.object(
            service_class,
            "_reconcile_page",
            autospec=True,
            side_effect=inspect_scope,
        ):
            wrong_scope._job_reconcile_meta_leads_page(
                expected_route_revision=self.route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
                since="2026-08-31 10:30:00",
                after="",
                page_number=0,
            )

        owning_company = self.route.company_id.id
        self.assertEqual(
            observed,
            [
                (
                    owning_company,
                    [owning_company],
                    owning_company,
                    [owning_company],
                    owning_company,
                )
            ],
        )

    def test_submission_job_reanchors_company_before_attribution(self):
        _dispatch, submission = self._dispatch_hint()
        caller_company = self.env["res.company"].create(
            {"name": "Unrelated Meta lead worker company %s" % uuid.uuid4()}
        )
        wrong_scope = (
            submission.sudo()
            .with_company(caller_company)
            .with_context(
                allowed_company_ids=[caller_company.id],
                job_uuid=submission.queue_job_uuid,
            )
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.fetch_lead.return_value = self._lead()
            result = wrong_scope._job_fetch_meta_lead(
                expected_route_revision=self.route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
            )

        submission.invalidate_recordset()
        self.assertTrue(result)
        self.assertEqual(submission.state, "ingested")
        self.assertEqual(submission.touchpoint_id.company_id, self.route.company_id)

    def test_reconciliation_cron_rotates_past_repeatedly_failing_routes(self):
        routes = self.route
        for position in range(2):
            routes |= self.env["marketing.center.meta.lead.route"].create(
                {
                    "name": "Fair Lead Ads route %s" % position,
                    "webhook_page_id": self.page.id,
                    "lead_profile_id": self.profile.id,
                    "source_id": self.source.id,
                    "external_form_id": "3000000000001%s" % position,
                }
            )
        selected = []

        def reject_route(route):
            selected.append(route.id)
            return False

        route_class = type(self.route)
        with patch.object(
            route_class,
            "_enqueue_reconciliation",
            autospec=True,
            side_effect=reject_route,
        ):
            for _iteration in range(4):
                self.env[
                    "marketing.center.meta.lead.route"
                ]._cron_enqueue_reconciliation(limit=1)

        ordered = routes.sorted("id").ids
        self.assertEqual(selected, ordered + ordered[:1])

    def test_source_cannot_be_archived_while_route_is_active(self):
        with self.assertRaises(ValidationError):
            self.source.write({"active": False})
        self.route.write({"active": False})
        self.source.write({"active": False})
        self.assertFalse(self.source.active)

    def test_reconciliation_enqueue_initializes_cursor_cycle_guard(self):
        with trap_jobs() as jobs:
            delayed = self.route._enqueue_reconciliation()
            jobs.assert_jobs_count(1)

        self.assertTrue(delayed)
        self.assertEqual(
            self.route.reconcile_cursor_hashes_json,
            [self.route._reconcile_cursor_digest("")],
        )

    def test_reconciler_persists_cursor_and_chains_only_one_bounded_page(self):
        current_job_uuid = str(uuid.uuid4())
        self.route.with_context(
            marketing_meta_lead_route_runtime_token=(
                MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN
            )
        ).write(
            {
                "reconcile_state": "running",
                "reconcile_since": datetime(2026, 8, 31, 10, 30),
                "reconcile_cursor_hashes_json": [
                    self.route._reconcile_cursor_digest("")
                ],
                "reconcile_job_uuid": current_job_uuid,
            }
        )
        page = MetaLeadPage(
            leads=(self._lead(),), next_after="next-bounded-cursor", has_more=True
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class, trap_jobs() as jobs:
            adapter_class.return_value.fetch_lead_page.return_value = page
            result = self.route.with_context(
                job_uuid=current_job_uuid
            )._job_reconcile_meta_leads_page(
                expected_route_revision=self.route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
                since="2026-08-31 10:30:00",
                after="",
                page_number=0,
            )
            jobs.assert_jobs_count(1)
        self.assertTrue(result["continued"])
        self.assertEqual(self.route.reconcile_after, "next-bounded-cursor")
        self.assertEqual(self.route.reconcile_page_count, 1)
        self.assertEqual(
            self.env["marketing.center.meta.lead.submission"].search_count(
                [("leadgen_id", "=", self.LEAD_ID), ("state", "=", "ingested")]
            ),
            1,
        )

    def test_reconciler_stops_a_non_immediate_provider_cursor_cycle(self):
        current_job_uuid = str(uuid.uuid4())
        first_cursor = "cursor-a"
        current_cursor = "cursor-b"
        self.route.with_context(
            marketing_meta_lead_route_runtime_token=(
                MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN
            )
        ).write(
            {
                "reconcile_state": "running",
                "reconcile_since": datetime(2026, 8, 31, 10, 30),
                "reconcile_after": current_cursor,
                "reconcile_cursor_hashes_json": [
                    self.route._reconcile_cursor_digest(first_cursor),
                    self.route._reconcile_cursor_digest(current_cursor),
                ],
                "reconcile_job_uuid": current_job_uuid,
            }
        )
        page = MetaLeadPage(leads=(), next_after=first_cursor, has_more=True)
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class, trap_jobs() as jobs:
            adapter_class.return_value.fetch_lead_page.return_value = page
            result = self.route.with_context(
                job_uuid=current_job_uuid
            )._job_reconcile_meta_leads_page(
                expected_route_revision=self.route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
                since="2026-08-31 10:30:00",
                after=current_cursor,
                page_number=1,
            )
            jobs.assert_jobs_count(0)

        self.assertFalse(result)
        self.assertEqual(self.route.reconcile_state, "error")
        self.assertEqual(self.route.last_reconcile_error_class, "ProviderCursorCycle")

    def test_orphan_submission_job_never_crosses_provider_boundary(self):
        _dispatch, submission = self._dispatch_hint()
        owned_uuid = submission.queue_job_uuid
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            self.assertFalse(
                submission.with_context(
                    job_uuid=str(uuid.uuid4())
                )._job_fetch_meta_lead(
                    expected_route_revision=self.route.route_revision,
                    expected_profile_revision=self.profile.profile_revision,
                    expected_app_revision=self.app.revision,
                )
            )
            submission.with_context(
                marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
            ).write({"queue_job_uuid": False})
            self.assertFalse(
                submission.with_context(job_uuid=owned_uuid)._job_fetch_meta_lead(
                    expected_route_revision=self.route.route_revision,
                    expected_profile_revision=self.profile.profile_revision,
                    expected_app_revision=self.app.revision,
                )
            )
        adapter_class.assert_not_called()
        self.assertEqual(submission.state, "pending")

    def test_orphan_reconcile_job_never_crosses_provider_boundary(self):
        owned_uuid = str(uuid.uuid4())
        self.route.with_context(
            marketing_meta_lead_route_runtime_token=(
                MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN
            )
        ).write(
            {
                "reconcile_state": "queued",
                "reconcile_since": datetime(2026, 8, 31, 10, 30),
                "reconcile_job_uuid": owned_uuid,
            }
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.lead_ads."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            result = self.route.with_context(
                job_uuid=str(uuid.uuid4())
            )._job_reconcile_meta_leads_page(
                expected_route_revision=self.route.route_revision,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
                since="2026-08-31 10:30:00",
                after="",
                page_number=0,
            )
        self.assertFalse(result)
        adapter_class.assert_not_called()
        self.assertEqual(self.route.reconcile_state, "queued")

    def test_cross_company_route_is_rejected(self):
        other_company = self.env["res.company"].create(
            {"name": "Lead Ads isolated company %s" % uuid.uuid4()}
        )
        with self.assertRaises(UserError):
            self.env["marketing.center.meta.lead.route"].sudo().create(
                {
                    "name": "Cross-company route",
                    "company_id": other_company.id,
                    "webhook_page_id": self.page.id,
                    "lead_profile_id": self.profile.id,
                    "source_id": self.source.id,
                    "external_form_id": "300000000000099",
                }
            )
