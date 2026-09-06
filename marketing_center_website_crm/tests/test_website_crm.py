import datetime
import json
import uuid
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.modules.module import get_manifest
from odoo.tests import tagged
from odoo.tests.common import HttpCase, SavepointCase

from odoo.addons.marketing_center_web_ingress.services.errors import (
    WebIngressSerializationFailure,
)
from odoo.addons.marketing_center_website.controllers.website_action import (
    MarketingWebsiteFormController,
)
from odoo.addons.marketing_center_website.services.contracts import sha256_text
from odoo.addons.website.controllers.form import WebsiteForm as NativeWebsiteForm
from odoo.addons.website_crm.controllers.website_form import (
    WebsiteForm as NativeWebsiteCrmForm,
)

from ..controllers.website_form import MarketingWebsiteCrmFormController


class TestMarketingWebsiteCrm(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.company = cls.website.company_id
        cls.origin = "https://website-crm.example.test"
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Website CRM endpoint",
                "company_id": cls.company.id,
                "allowed_origins": cls.origin,
                "allowed_hosts": "website-crm.example.test",
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
                }
            )
        cls.crm_model = cls.env["ir.model"]._get("crm.lead")
        cls.crm_model.write({"website_form_access": True})
        cls.action = cls.env["marketing.website.action"].create(
            {
                "name": "CRM lead form",
                "binding_id": cls.binding.id,
                "kind": "form_submission",
                "route_ref": "form.crm.lead",
                "source_path": "/contactus",
                "form_model_id": cls.crm_model.id,
            }
        )
        cls.service = cls.env["marketing.website.crm.service"].with_company(cls.company)

    def _claim(self, session_ref=None):
        return {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": session_ref or str(uuid.uuid4()),
        }

    def _ingest_entry(
        self, session_ref, occurred_at=None, origin=None, observed_at=None
    ):
        occurred_at = occurred_at or fields.Datetime.now()
        origin = origin or self.origin
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": "entry_point",
            "occurred_at": occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "landing_url": "%s/landing?utm_source=google" % origin,
            "referrer_url": "https://google.example/search",
            "utm_source": "google",
            "utm_medium": "cpc",
            "session_ref": session_ref,
        }
        return (
            self.env["marketing.web.ingress.service"]
            .sudo()
            ._ingest_payload(
                self.endpoint,
                payload,
                origin=origin,
                observed_at=observed_at or fields.Datetime.now(),
                body_size_bytes=512,
            )
        )

    def _lead(self, name="Website CRM lead"):
        return self.env["crm.lead"].create(
            {"name": "%s %s" % (name, uuid.uuid4()), "company_id": self.company.id}
        )

    def _native_result(self, claim, lead, receipt=None):
        receipt = receipt or self.env[
            "marketing.website.action.service"
        ]._prepare_form_receipt(
            self.website,
            "crm.lead",
            claim,
            self.origin,
        )
        return json.dumps(
            {"id": lead.id, "marketing_center_receipt": receipt},
            separators=(",", ":"),
        )

    def _pending_intent(self, label):
        claim = self._claim()
        lead = self._lead(label)
        receipt = self.env["marketing.website.action.service"]._prepare_form_receipt(
            self.website,
            "crm.lead",
            claim,
            self.origin,
        )
        action_service = (
            self.env["marketing.website.action.service"]
            .sudo()
            .with_context(allowed_company_ids=[self.company.id])
            .with_company(self.company)
        )
        payload = dict(claim, receipt=receipt)
        action, values, occurred_at = action_service._validate_form_receipt(
            self.website,
            payload,
            self.origin,
        )
        return self.service._get_or_create_intent(
            self.website,
            action,
            lead,
            values["event_id"],
            values["session_ref"],
            self.origin,
            occurred_at,
        )

    def _marketing_admin_restricted_to(self, company):
        return (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Restricted Website CRM admin %s" % uuid.uuid4(),
                    "login": "restricted-website-crm-%s@example.invalid" % uuid.uuid4(),
                    "company_id": company.id,
                    "company_ids": [(6, 0, [company.id])],
                    "groups_id": [
                        (
                            6,
                            0,
                            [
                                self.env.ref("base.group_user").id,
                                self.env.ref(
                                    "marketing_center_base."
                                    "group_marketing_center_admin"
                                ).id,
                            ],
                        )
                    ],
                }
            )
        )

    def _correlate_native_form_result(
        self, website, model_name, claim, origin, native_result
    ):
        """Exercise the canonical durable-intent path and return its projection."""
        return self.service._capture_native_form_intent(
            website, model_name, claim, origin, native_result
        ).correlation_id

    def test_controller_mro_keeps_native_website_crm_hooks(self):
        controller_mro = MarketingWebsiteCrmFormController.__mro__
        marketing_position = controller_mro.index(MarketingWebsiteFormController)
        native_crm_position = controller_mro.index(NativeWebsiteCrmForm)
        native_form_position = controller_mro.index(NativeWebsiteForm)

        self.assertLess(marketing_position, native_crm_position)
        self.assertLess(native_crm_position, native_form_position)
        # Odoo consumes the empty ``@http.route()`` metadata while building the
        # controller routing map, so a runtime ``routing`` attribute is not a
        # stable contract.  The HttpCase below proves that the inherited native
        # route is actually published and dispatches through this composite MRO.
        self.assertIn("website_form", MarketingWebsiteCrmFormController.__dict__)

    def test_bridge_manifest_requires_native_website_crm_but_not_link_tracker(self):
        dependencies = get_manifest("marketing_center_website_crm")["depends"]
        self.assertIn("website_crm", dependencies)
        self.assertNotIn("link_tracker", dependencies)

    def test_success_links_only_opaque_evidence_and_replay_is_idempotent(self):
        claim = self._claim()
        lead = self._lead("Private Person private@example.invalid")
        native_result = self._native_result(claim, lead)
        correlation = self._correlate_native_form_result(
            self.website, "crm.lead", claim, self.origin, native_result
        )
        replay = self._correlate_native_form_result(
            self.website, "crm.lead", claim, self.origin, native_result
        )
        self.assertEqual(replay, correlation)
        self.assertEqual(correlation.lead_id, lead)
        self.assertEqual(correlation.event_id.touchpoint_id, correlation.touchpoint_id)
        self.assertNotEqual(correlation.event_id.public_ref, claim["event_id"])
        self.assertEqual(
            correlation.event_id.event_key_hash,
            sha256_text(claim["event_id"]),
        )
        self.assertEqual(correlation.assertion_id.authority_key, "website.form")
        self.assertEqual(
            self.env["marketing.website.crm.correlation"].sudo().search_count([]), 1
        )
        self.assertEqual(
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search_count([("authority_key", "=", "website.form")]),
            1,
        )
        serialized = "|".join(
            (
                correlation.event_id.public_ref,
                correlation.assertion_id.source_ref,
                correlation.touchpoint_id.source_evidence_ref,
            )
        )
        self.assertNotIn("private@example.invalid", serialized)
        self.assertNotIn("Private Person", serialized)
        intent = self.env["marketing.website.crm.intent"].sudo().search([])
        self.assertEqual(len(intent), 1)
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.correlation_id, correlation)
        self.assertNotIn("receipt", self.env["marketing.website.crm.intent"]._fields)

    def test_prior_session_entry_is_append_only_noncausal_crm_evidence(self):
        causal_before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "marketing.attribution.candidate",
                "marketing.attribution.result",
                "marketing.attribution.contribution",
            )
        }
        session_ref = str(uuid.uuid4())
        entry = self._ingest_entry(
            session_ref, fields.Datetime.now() - datetime.timedelta(minutes=1)
        )
        claim = self._claim(session_ref)
        lead = self._lead("Session evidence")
        self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        intent = (
            self.env["marketing.website.crm.intent"]
            .sudo()
            .search([("lead_id", "=", lead.id)])
        )
        links = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links.touchpoint_id.id, entry.touchpoint_id)
        self.assertEqual(
            links.authority_ref, "website.form.intent:%s" % intent.public_ref
        )
        self.assertTrue(links.assertion_ref.startswith("website.session:"))
        self.assertNotIn(session_ref, links.source_ref)
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.session_reconcile_state, "watching")
        self.assertEqual(links.touchpoint_id.utm_source, "google")
        self.assertEqual(
            causal_before,
            {model: self.env[model].sudo().search_count([]) for model in causal_before},
        )
        self.env["marketing.website.crm.intent"]._cron_reconcile_sessions(
            now=intent.session_reconcile_until + datetime.timedelta(seconds=1)
        )
        intent.invalidate_recordset()
        self.assertEqual(intent.session_reconcile_state, "complete")

    def test_session_authority_revocation_is_append_only_and_not_revived(self):
        session_ref = str(uuid.uuid4())
        self._ingest_entry(
            session_ref, fields.Datetime.now() - datetime.timedelta(minutes=1)
        )
        claim = self._claim(session_ref)
        lead = self._lead("Revoked session evidence")
        intent = self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        links = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.service._revoke_session_assertions(
            intent, "website-session-revocation:%s" % intent.public_ref, "operator"
        )
        self.assertTrue(links.revocation_ids)
        self.service._recover_session_intent(intent)
        replay = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.assertEqual(replay, links)
        self.assertTrue(replay.revocation_ids)

    def test_session_authority_can_revoke_after_native_lead_merge(self):
        session_ref = str(uuid.uuid4())
        entry = self._ingest_entry(
            session_ref, fields.Datetime.now() - datetime.timedelta(minutes=1)
        )
        entry_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            entry.touchpoint_id
        )
        source = self._lead("Merged Website session source")
        target = self._lead("Merged Website session survivor")
        source.write({"probability": 10})
        target.write({"probability": 90})
        claim = self._claim(session_ref)
        intent = self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, source),
        )
        root_assertion = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", source.id),
                    ("canonical_key", "=", entry_touchpoint.canonical_key),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.assertEqual(len(root_assertion), 1)

        merged = (source | target).merge_opportunity()
        self.assertEqual(merged, target)
        intent.invalidate_recordset(["lead_id"])
        self.assertFalse(intent.lead_id)
        derived = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", target.id),
                    ("derived_from_assertion_id", "=", root_assertion.id),
                ]
            )
        )
        self.assertEqual(len(derived), 1)
        effective_domain = [
            ("canonical_key", "=", entry_touchpoint.canonical_key),
            ("lead_id", "=", target.id),
        ]
        self.assertTrue(
            self.env["marketing.attribution.crm.effective.link"].search(
                effective_domain
            )
        )

        revocations = self.service._revoke_session_assertions(
            intent,
            "website-session-after-merge:%s" % intent.public_ref,
            "operator",
        )
        self.assertEqual(revocations.assertion_id, root_assertion)
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                effective_domain
            )
        )

    def test_same_session_can_link_independently_to_two_leads(self):
        session_ref = str(uuid.uuid4())
        self._ingest_entry(
            session_ref, fields.Datetime.now() - datetime.timedelta(minutes=1)
        )
        leads = self._lead("First session lead") | self._lead("Second session lead")
        for lead in leads:
            claim = self._claim(session_ref)
            self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        links = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search([("authority_key", "=", "website.session.correlation.v1")])
        )
        self.assertEqual(set(links.mapped("lead_id").ids), set(leads.ids))
        self.assertEqual(len(links), 2)

    def test_session_scope_excludes_old_and_other_origin_touchpoints(self):
        session_ref = str(uuid.uuid4())
        old = fields.Datetime.now() - datetime.timedelta(hours=25)
        self._ingest_entry(session_ref, old, observed_at=old)
        other_origin = "https://other-crm.example.test"
        self.endpoint.write(
            {
                "allowed_origins": "%s\n%s" % (self.origin, other_origin),
                "allowed_hosts": ("website-crm.example.test\nother-crm.example.test"),
            }
        )
        self._ingest_entry(
            session_ref,
            fields.Datetime.now() - datetime.timedelta(minutes=1),
            origin=other_origin,
        )
        claim = self._claim(session_ref)
        lead = self._lead("Strict session scope")
        self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        self.assertFalse(
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )

    def test_late_entry_is_reconciled_without_reopening_done_intent(self):
        session_ref = str(uuid.uuid4())
        claim = self._claim(session_ref)
        lead = self._lead("Late session evidence")
        intent = self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        self.assertEqual(intent.state, "done")
        self.assertFalse(
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search([("authority_key", "=", "website.session.correlation.v1")])
        )
        entry = self._ingest_entry(
            session_ref, intent.occurred_at - datetime.timedelta(seconds=30)
        )
        self.env["marketing.website.crm.intent"]._cron_reconcile_sessions(
            now=intent.next_session_reconcile_at + datetime.timedelta(seconds=1)
        )
        intent.invalidate_recordset()
        links = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search([("authority_key", "=", "website.session.correlation.v1")])
        )
        self.assertEqual(links.touchpoint_id.id, entry.touchpoint_id)
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.session_reconcile_state, "watching")

    def test_session_failure_does_not_undo_direct_form_correlation(self):
        claim = self._claim()
        lead = self._lead("Bounded session")
        service_class = type(self.service)
        with patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=ValidationError("over limit"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.assertEqual(intent.state, "done")
        self.assertTrue(intent.correlation_id)
        self.assertEqual(intent.session_reconcile_state, "failed")
        self.assertEqual(
            intent.correlation_id.assertion_id.authority_key, "website.form"
        )

    def test_expired_session_retry_runs_final_reconciliation(self):
        session_ref = str(uuid.uuid4())
        claim = self._claim(session_ref)
        lead = self._lead("Expired retry")
        service_class = type(self.service)
        with patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=ValidationError("initial session failure"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        entry = self._ingest_entry(
            session_ref, intent.occurred_at - datetime.timedelta(seconds=30)
        )
        expired_at = intent.session_reconcile_until + datetime.timedelta(seconds=1)
        with patch.object(
            fields.Datetime, "now", return_value=expired_at
        ), patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=ValidationError("final session failure"),
        ):
            intent.action_retry_session()
        intent.invalidate_recordset()
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.session_reconcile_state, "failed")
        self.assertEqual(intent.session_reconcile_error_class, "ValidationError")

        with patch.object(fields.Datetime, "now", return_value=expired_at):
            intent.action_retry_session()
        intent.invalidate_recordset()
        link = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.assertEqual(link.touchpoint_id.id, entry.touchpoint_id)
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.session_reconcile_state, "complete")
        self.assertFalse(intent.next_session_reconcile_at)
        self.assertFalse(intent.session_reconcile_error_class)

    def test_final_runtime_failure_is_terminal_for_automatic_recovery(self):
        claim = self._claim()
        lead = self._lead("Final runtime failure")
        service_class = type(self.service)
        with patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=ValidationError("initial session failure"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        expired_at = intent.session_reconcile_until + datetime.timedelta(seconds=1)
        with patch.object(
            fields.Datetime, "now", return_value=expired_at
        ), patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=RuntimeError("final runtime failure"),
        ):
            intent.action_retry_session()
        intent.invalidate_recordset()
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.session_reconcile_state, "failed")
        self.assertEqual(intent.session_reconcile_error_class, "RuntimeError")
        self.assertFalse(intent.next_session_reconcile_at)
        processed = self.env["marketing.website.crm.intent"]._cron_reconcile_sessions(
            now=expired_at + datetime.timedelta(minutes=1)
        )
        self.assertEqual(processed, 0)

    def test_session_retry_inside_window_reconciles_immediately(self):
        session_ref = str(uuid.uuid4())
        claim = self._claim(session_ref)
        lead = self._lead("Immediate retry")
        service_class = type(self.service)
        with patch.object(
            service_class,
            "_session_touchpoints",
            side_effect=ValidationError("initial session failure"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        entry = self._ingest_entry(
            session_ref, intent.occurred_at - datetime.timedelta(seconds=30)
        )
        intent.action_retry_session()
        intent.invalidate_recordset()
        link = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", "website.session.correlation.v1"),
                ]
            )
        )
        self.assertEqual(link.touchpoint_id.id, entry.touchpoint_id)
        self.assertEqual(intent.session_reconcile_state, "watching")
        self.assertTrue(intent.next_session_reconcile_at)

    def test_terminal_queue_job_is_not_reenqueued_forever(self):
        claim = self._claim()
        lead = self._lead("Terminal queue")
        service_class = type(self.service)
        with patch.object(
            service_class, "_process_intent", side_effect=RuntimeError("temporary")
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", intent.queue_job_uuid)], limit=1)
        )
        self.assertTrue(job)
        job.write({"state": "failed"})
        intent._internal_write({"next_retry_at": fields.Datetime.now()})
        enqueued = self.env["marketing.website.crm.intent"]._cron_enqueue_due()
        intent.invalidate_recordset()
        self.assertEqual(enqueued, 0)
        self.assertEqual(intent.state, "failed")
        self.assertEqual(intent.last_error_class, "QueueJobTerminal")

    def test_retry_rejects_marketing_admin_from_another_company(self):
        intent = self._pending_intent("Cross-company retry")
        intent._internal_write({"state": "failed"})
        other_company = self.env["res.company"].create(
            {"name": "Restricted retry company %s" % uuid.uuid4()}
        )
        restricted_admin = self._marketing_admin_restricted_to(other_company)
        foreign_intent = intent.with_user(restricted_admin).with_context(
            allowed_company_ids=[other_company.id],
            force_company=other_company.id,
        )

        with self.assertRaises(AccessError):
            foreign_intent.action_retry()

        intent.invalidate_recordset(["state", "queue_job_uuid"])
        self.assertEqual(intent.state, "failed")
        self.assertFalse(intent.queue_job_uuid)

    def test_session_retry_rejects_marketing_admin_from_another_company(self):
        claim = self._claim()
        lead = self._lead("Cross-company session retry")
        intent = self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        self.assertEqual(intent.state, "done")
        self.assertTrue(intent.correlation_id)
        self.assertTrue(intent.processed_at)
        intent._internal_write(
            {
                "session_reconcile_state": "failed",
                "next_session_reconcile_at": False,
            }
        )
        other_company = self.env["res.company"].create(
            {"name": "Restricted session company %s" % uuid.uuid4()}
        )
        restricted_admin = self._marketing_admin_restricted_to(other_company)
        foreign_intent = intent.with_user(restricted_admin).with_context(
            allowed_company_ids=[other_company.id]
        )

        with self.assertRaises(AccessError):
            foreign_intent.action_retry_session()

        intent.invalidate_recordset(
            ["session_reconcile_state", "next_session_reconcile_at"]
        )
        self.assertEqual(intent.session_reconcile_state, "failed")
        self.assertFalse(intent.next_session_reconcile_at)

    def test_due_cron_rotates_past_active_low_id_jobs(self):
        intents = self.env["marketing.website.crm.intent"]
        for position in range(4):
            intents |= self._pending_intent("Fair due intent %s" % position)
        active = intents.sorted("id")[:3]
        target = intents.sorted("id")[-1]
        active._enqueue()
        self.assertTrue(all(active.mapped("queue_job_uuid")))
        self.assertFalse(target.queue_job_uuid)

        processed = []
        for _iteration in range(4):
            processed.append(
                self.env["marketing.website.crm.intent"]._cron_enqueue_due(limit=1)
            )

        target.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(processed, [0, 0, 0, 1])
        self.assertTrue(target.queue_job_uuid)

    def test_session_cron_rotates_between_expired_and_live_due_work(self):
        self.endpoint.write({"replay_window_seconds": 30})
        expired = self.env["marketing.website.crm.intent"]
        for position in range(3):
            claim = self._claim()
            lead = self._lead("Expired fair session %s" % position)
            expired |= self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.endpoint.write({"replay_window_seconds": 3600})
        live_claim = self._claim()
        live = self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            live_claim,
            self.origin,
            self._native_result(live_claim, self._lead("Live fair session")),
        )
        run_at = max(
            max(expired.mapped("session_reconcile_until"))
            + datetime.timedelta(seconds=1),
            live.next_session_reconcile_at,
        )
        self.assertLess(run_at, live.session_reconcile_until)
        observed = []

        def inspect_recovery(service, intent, _now=None, final=False):
            observed.append((intent.id, final))
            return True

        service_class = type(self.service)
        with patch.object(
            service_class,
            "_recover_session_intent",
            autospec=True,
            side_effect=inspect_recovery,
        ):
            for _iteration in range(4):
                self.env["marketing.website.crm.intent"]._cron_reconcile_sessions(
                    now=run_at, limit=1
                )

        ordered = (expired | live).sorted("id")
        self.assertEqual([item[0] for item in observed], ordered.ids)
        self.assertEqual([item[1] for item in observed], [True, True, True, False])

    def test_active_canonical_job_repairs_a_stale_persisted_pointer(self):
        claim = self._claim()
        lead = self._lead("Canonical queue ownership")
        with patch.object(
            type(self.service),
            "_process_intent",
            side_effect=RuntimeError("temporary"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        canonical_uuid = intent.queue_job_uuid
        jobs_before = (
            self.env["queue.job"]
            .sudo()
            .search_count([("identity_key", "=", intent._identity_key())])
        )
        intent._internal_write({"queue_job_uuid": str(uuid.uuid4())})

        intent._enqueue()

        intent.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(intent.queue_job_uuid, canonical_uuid)
        self.assertEqual(
            self.env["queue.job"]
            .sudo()
            .search_count([("identity_key", "=", intent._identity_key())]),
            jobs_before,
        )

    def test_worker_requires_the_current_persisted_job_uuid(self):
        claim = self._claim()
        lead = self._lead("Strict queue ownership")
        with patch.object(
            type(self.service),
            "_process_intent",
            side_effect=RuntimeError("temporary"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        attempts = intent.attempts
        persisted_uuid = intent.queue_job_uuid

        self.assertFalse(intent._job_process())
        self.assertFalse(intent.with_context(job_uuid=str(uuid.uuid4()))._job_process())
        intent.invalidate_recordset(["attempts", "state", "queue_job_uuid"])
        self.assertEqual(intent.attempts, attempts)
        self.assertEqual(intent.state, "retry")
        self.assertEqual(intent.queue_job_uuid, persisted_uuid)

        self.assertTrue(intent.with_context(job_uuid=persisted_uuid)._job_process())
        intent.invalidate_recordset(["state", "queue_job_uuid"])
        self.assertEqual(intent.state, "done")
        self.assertFalse(intent.queue_job_uuid)

    def test_intent_rejects_an_incomplete_terminal_projection(self):
        claim = self._claim()
        lead = self._lead("Invalid terminal projection")
        with patch.object(type(self.service), "_attempt_intent", return_value=True):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            intent._internal_write(
                {"state": "done", "processed_at": fields.Datetime.now()}
            )

    def test_unexpected_processing_failure_persists_and_recovers(self):
        claim = self._claim()
        lead = self._lead("Durable private@example.invalid")
        native_result = self._native_result(claim, lead)
        service_class = type(self.service)
        with patch.object(
            service_class,
            "_process_intent",
            side_effect=RuntimeError("private@example.invalid must not persist"),
        ):
            intent = self.service._capture_native_form_intent(
                self.website, "crm.lead", claim, self.origin, native_result
            )
        self.assertEqual(intent.state, "retry")
        self.assertEqual(intent.attempts, 1)
        self.assertEqual(intent.last_error_class, "RuntimeError")
        self.assertNotIn("private@example.invalid", intent.last_error_message)
        self.assertFalse(intent.correlation_id)
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )
        self.service._attempt_intent(intent)
        intent.invalidate_recordset()
        self.assertEqual(intent.state, "done")
        self.assertEqual(intent.attempts, 2)
        self.assertTrue(intent.correlation_id)

    def test_retry_uses_validated_snapshot_after_receipt_ttl(self):
        claim = self._claim()
        lead = self._lead("Delayed durable lead")
        service_class = type(self.service)
        with patch.object(
            service_class, "_process_intent", side_effect=RuntimeError("temporary")
        ):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        validated_at = intent.occurred_at
        action_service_class = type(self.env["marketing.website.action.service"])
        with patch.object(
            action_service_class,
            "_validate_form_receipt",
            side_effect=AssertionError("retry must not validate the receipt"),
        ):
            self.service._attempt_intent(intent)
        intent.invalidate_recordset()
        self.assertEqual(intent.state, "done")
        self.assertTrue(intent.correlation_id)
        self.assertEqual(intent.correlation_id.touchpoint_id.occurred_at, validated_at)

    def test_same_endpoint_event_cannot_be_claimed_by_another_action(self):
        claim = self._claim()
        lead = self._lead("One endpoint event")
        self.service._capture_native_form_intent(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        other_action = self.env["marketing.website.action"].create(
            {
                "name": "Other CRM lead form",
                "binding_id": self.binding.id,
                "kind": "form_submission",
                "route_ref": "form.crm.lead.alternate",
                "source_path": "/alternate-contactus",
                "form_model_id": self.crm_model.id,
            }
        )
        with self.assertRaises(ValidationError):
            self.service._get_or_create_intent(
                self.website,
                other_action,
                lead,
                claim["event_id"],
                claim["session_ref"],
                self.origin,
                fields.Datetime.now(),
            )

    def test_global_lead_anchored_to_other_company_is_rejected(self):
        other = self.env["res.company"].create({"name": "Other Website CRM company"})
        lead = (
            self.env["crm.lead"]
            .with_company(other)
            .create(
                {
                    "name": "Anchored elsewhere",
                    "company_id": False,
                }
            )
        )
        claim = self._claim()
        with self.assertRaises(ValidationError):
            self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.assertFalse(self.env["marketing.website.crm.intent"].sudo().search([]))

    def test_failed_native_result_and_invalid_receipt_create_no_evidence(self):
        claim = self._claim()
        empty = self._correlate_native_form_result(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            json.dumps({"error": "native create failed"}),
        )
        self.assertFalse(empty)
        lead = self._lead()
        valid_result = json.loads(self._native_result(claim, lead))
        valid_receipt = valid_result["marketing_center_receipt"]
        invalid_receipt = valid_receipt[:-1] + (
            "0" if valid_receipt[-1] != "0" else "1"
        )
        with self.assertRaises(AccessError):
            self._correlate_native_form_result(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead, receipt=invalid_receipt),
            )
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_event_cannot_be_replayed_for_a_different_native_lead(self):
        claim = self._claim()
        first_lead = self._lead("First")
        second_lead = self._lead("Second")
        first_result = self._native_result(claim, first_lead)
        receipt = json.loads(first_result)["marketing_center_receipt"]
        correlation = self._correlate_native_form_result(
            self.website, "crm.lead", claim, self.origin, first_result
        )
        with self.assertRaises(ValidationError):
            self._correlate_native_form_result(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, second_lead, receipt=receipt),
            )
        self.assertEqual(correlation.lead_id, first_lead)
        self.assertEqual(
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search_count([("authority_key", "=", "website.form")]),
            1,
        )

    def test_arbitrary_form_model_is_never_correlated(self):
        claim = self._claim()
        lead = self._lead()
        with self.assertRaises(ValidationError):
            self._correlate_native_form_result(
                self.website,
                "res.partner",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_cross_company_lead_is_rejected(self):
        other_company = self.env["res.company"].create(
            {"name": "Website CRM other %s" % uuid.uuid4()}
        )
        lead = (
            self.env["crm.lead"]
            .sudo()
            .with_context(allowed_company_ids=[other_company.id])
            .with_company(other_company)
            .create({"name": "Other-company lead", "company_id": other_company.id})
        )
        claim = self._claim()
        with self.assertRaises(ValidationError):
            self._correlate_native_form_result(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )

    def test_correlation_model_is_append_only(self):
        claim = self._claim()
        lead = self._lead()
        correlation = self._correlate_native_form_result(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        with self.assertRaises(AccessError):
            correlation.sudo().write({"lead_id": lead.id})
        with self.assertRaises(AccessError):
            correlation.sudo().unlink()
        with self.assertRaises(AccessError):
            self.env["marketing.website.crm.correlation"].sudo().create(
                {
                    "company_id": self.company.id,
                    "event_id": correlation.event_id.id,
                    "action_id": self.action.id,
                    "touchpoint_id": correlation.touchpoint_id.id,
                    "lead_id": lead.id,
                    "assertion_id": correlation.assertion_id.id,
                }
            )

    def test_lead_unlink_preserves_website_evidence_snapshots(self):
        claim = self._claim()
        lead = self._lead("Website tombstone")
        lead_id = lead.id
        lead_name = lead.name
        correlation = self._correlate_native_form_result(
            self.website,
            "crm.lead",
            claim,
            self.origin,
            self._native_result(claim, lead),
        )
        intent = (
            self.env["marketing.website.crm.intent"]
            .sudo()
            .search([("event_ref", "=", claim["event_id"])], limit=1)
        )

        self.assertTrue(lead.unlink())
        correlation.invalidate_recordset(["lead_id"])
        correlation.assertion_id.invalidate_recordset(["lead_id"])
        intent.invalidate_recordset(["lead_id"])
        self.assertFalse(correlation.lead_id)
        self.assertFalse(correlation.assertion_id.lead_id)
        self.assertFalse(intent.lead_id)
        for evidence in (correlation, correlation.assertion_id, intent):
            self.assertEqual(evidence.lead_model, "crm.lead")
            self.assertEqual(evidence.lead_res_id, lead_id)
            self.assertEqual(evidence.lead_display_ref, lead_name)

    def test_pending_intent_becomes_terminal_when_lead_is_removed(self):
        claim = self._claim()
        lead = self._lead("Website pending tombstone")
        with patch.object(type(self.service), "_attempt_intent", return_value=True):
            intent = self.service._capture_native_form_intent(
                self.website,
                "crm.lead",
                claim,
                self.origin,
                self._native_result(claim, lead),
            )
        self.assertEqual(intent.state, "pending")
        lead.unlink()
        intent.invalidate_recordset(["lead_id"])
        self.assertFalse(intent.lead_id)
        self.assertTrue(self.service._attempt_intent(intent))
        self.assertEqual(intent.state, "tombstoned")
        self.assertEqual(intent.session_reconcile_state, "complete")
        self.assertFalse(intent.correlation_id)


@tagged("-at_install", "post_install")
class TestMarketingWebsiteCrmHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.origin = cls.base_url().rstrip("/")
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Website CRM HTTP endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": cls.origin,
                "allowed_hosts": urlsplit(cls.origin).hostname,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
                }
            )
        cls.crm_model = cls.env["ir.model"]._get("crm.lead")
        cls.crm_model.write({"website_form_access": True})
        cls.action = cls.env["marketing.website.action"].create(
            {
                "name": "CRM HTTP form",
                "binding_id": cls.binding.id,
                "kind": "form_submission",
                "route_ref": "form.crm.http",
                "source_path": "/contactus",
                "form_model_id": cls.crm_model.id,
            }
        )

    def _post_form(self, claim, name, extra=None):
        query = urlencode(
            {
                "mc_action": claim["action_ref"],
                "mc_event": claim["event_id"],
                "mc_session": claim["session_ref"],
            }
        )
        data = {"name": name} if name is not None else {}
        data.update(extra or {})
        return self.opener.post(
            "%s/website/form/crm.lead?%s" % (self.base_url(), query),
            data=data,
            headers={"Origin": self.origin},
        )

    def test_native_visitor_is_linked_to_marketing_correlated_lead(self):
        self.opener.cookies.clear()
        self.opener.headers.update(
            {"User-Agent": "marketing-center-mro/%s" % uuid.uuid4()}
        )
        self.assertTrue(self.env.ref("website.contactus_page").track)
        visitor_ids_before = self.env["website.visitor"].sudo().search([]).ids

        page = self.url_open("/contactus")
        self.assertEqual(page.status_code, 200, page.text)
        self.env.invalidate_all()
        visitor = (
            self.env["website.visitor"]
            .sudo()
            .search([("id", "not in", visitor_ids_before)], order="id desc", limit=1)
        )
        self.assertTrue(visitor, "The native Website request must create a visitor")

        claim = {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": str(uuid.uuid4()),
        }
        marker = "Website CRM native visitor %s" % uuid.uuid4()
        response = self._post_form(claim, marker)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("marketing_center_receipt"))

        self.env.invalidate_all()
        lead = self.env["crm.lead"].sudo().search([("name", "=", marker)])
        self.assertEqual(len(lead), 1)
        visitor.invalidate_recordset(["lead_ids"])
        self.assertIn(lead, visitor.lead_ids)
        correlation = (
            self.env["marketing.website.crm.correlation"]
            .sudo()
            .search([("lead_id", "=", lead.id)])
        )
        self.assertEqual(len(correlation), 1)
        self.assertEqual(correlation.touchpoint_id, correlation.event_id.touchpoint_id)

    def test_native_field_validation_failure_creates_no_marketing_evidence(self):
        claim = {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": str(uuid.uuid4()),
        }
        marker = "Website CRM invalid field %s" % uuid.uuid4()
        lead_count = self.env["crm.lead"].sudo().search_count([])
        response = self._post_form(claim, marker, {"team_id": "not-an-id"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("team_id", response.json().get("error_fields", []))
        self.env.invalidate_all()
        self.assertEqual(self.env["crm.lead"].sudo().search_count([]), lead_count)
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )

    def test_native_create_and_serialization_retry_produce_one_correlation(self):
        claim = {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": str(uuid.uuid4()),
        }
        service_class = type(self.env["marketing.website.crm.service"])
        original = service_class._capture_native_form_intent
        attempts = []

        def fail_once(service, *args, **kwargs):
            attempts.append(args[4])
            if len(attempts) == 1:
                raise WebIngressSerializationFailure("synthetic Website CRM retry")
            return original(service, *args, **kwargs)

        marker = "Website CRM retry %s" % uuid.uuid4()
        with patch.object(service_class, "_capture_native_form_intent", new=fail_once):
            response = self._post_form(claim, marker)
        self.assertEqual(response.status_code, 200, response.text)
        response_payload = response.json()
        self.assertIn("marketing_center_receipt", response_payload, response_payload)
        self.assertEqual(len(attempts), 2)
        attempt_payloads = [json.loads(value) for value in attempts]
        self.assertTrue(
            all(
                payload.get("marketing_center_receipt") for payload in attempt_payloads
            ),
            attempt_payloads,
        )
        self.env.invalidate_all()
        leads = self.env["crm.lead"].sudo().search([("name", "=", marker)])
        self.assertEqual(len(leads), 1)
        correlation = (
            self.env["marketing.website.crm.correlation"]
            .sudo()
            .search([("lead_id", "=", leads.id)])
        )
        self.assertEqual(len(correlation), 1)
        self.assertEqual(correlation.event_id.touchpoint_id, correlation.touchpoint_id)
        self.assertEqual(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)]),
            1,
        )
        self.assertEqual(
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search_count([("authority_key", "=", "website.form")]),
            1,
        )

    def test_unexpected_bridge_failure_keeps_intent_and_recovers(self):
        claim = {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": str(uuid.uuid4()),
        }
        service = self.env["marketing.website.crm.service"]
        service_class = type(service)
        marker = "Website CRM durable failure %s" % uuid.uuid4()

        with patch.object(
            service_class,
            "_process_intent",
            side_effect=RuntimeError("synthetic Website CRM bridge failure"),
        ):
            response = self._post_form(claim, marker)

        self.assertEqual(response.status_code, 200, response.text)
        response_payload = response.json()
        self.assertIn("marketing_center_receipt", response_payload, response_payload)
        self.env.invalidate_all()
        lead = self.env["crm.lead"].sudo().search([("name", "=", marker)])
        self.assertEqual(len(lead), 1)
        intent = (
            self.env["marketing.website.crm.intent"]
            .sudo()
            .search([("lead_id", "=", lead.id)])
        )
        self.assertEqual(len(intent), 1)
        self.assertEqual(intent.state, "retry")
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )
        service.with_company(intent.company_id)._attempt_intent(intent)
        intent.invalidate_recordset()
        self.assertEqual(intent.state, "done")
        self.assertTrue(intent.correlation_id)

    def test_pre_intent_failure_never_rolls_back_the_native_lead(self):
        claim = {
            "action_ref": self.action.public_ref,
            "event_id": str(uuid.uuid4()),
            "session_ref": str(uuid.uuid4()),
        }
        service_class = type(self.env["marketing.website.crm.service"])
        marker = "Website CRM isolated failure %s" % uuid.uuid4()

        with patch.object(
            service_class,
            "_capture_native_form_intent",
            side_effect=RuntimeError("synthetic pre-intent failure"),
        ):
            response = self._post_form(claim, marker)

        self.assertEqual(response.status_code, 200, response.text)
        self.env.invalidate_all()
        self.assertEqual(
            len(self.env["crm.lead"].sudo().search([("name", "=", marker)])), 1
        )
        self.assertFalse(self.env["marketing.website.crm.intent"].sudo().search([]))
        self.assertFalse(
            self.env["marketing.website.crm.correlation"].sudo().search([])
        )
