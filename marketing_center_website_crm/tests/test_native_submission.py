import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..controllers import website_form
from ..models.native_submission import acquisition_values


class TestNativeWebsiteSubmission(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.origin = "https://native-form.example.test"
        cls.website = cls.env["website"].create(
            {
                "name": "Native acquisition",
                "domain": cls.origin,
                "company_id": cls.env.company.id,
                "cookies_bar": True,
            }
        )
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Native acquisition",
                "company_id": cls.env.company.id,
                "allowed_origins": cls.origin,
                "allowed_hosts": "native-form.example.test",
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "website_tracking_policy": "informational_notice",
                "privacy_legal_basis_code": False,
                "privacy_policy_version": "native-v1",
                "privacy_notice_version": "native-v1",
                "privacy_policy_justification": "Synthetic test",
                "identifier_retention_days": 30,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].create(
            {
                "website_id": cls.website.id,
                "endpoint_id": cls.endpoint.id,
            }
        )
        model = cls.env["ir.model"]._get("crm.lead")
        model.website_form_access = True
        cls.action = cls.env["marketing.website.action"].create(
            {
                "name": "Native form",
                "binding_id": cls.binding.id,
                "kind": "form_submission",
                "route_ref": "native.form",
                "source_path": "/contactus",
                "form_model_id": model.id,
            }
        )
        cls.visitor = cls.env["website.visitor"].create(
            {"access_token": uuid.uuid4().hex}
        )
        cls.service = cls.env["marketing.website.crm.service"]

    def _track(self, path, when):
        return self.env["website.track"].create(
            {
                "visitor_id": self.visitor.id,
                "url": self.origin + path,
                "visit_datetime": when,
            }
        )

    def _snapshot(self, *, referrer=None, now=None, cookies=None):
        return self.service._native_snapshot(
            self.website,
            self.binding,
            str(uuid.uuid4()),
            self.origin,
            referrer or self.origin + "/contactus",
            now or fields.Datetime.now(),
            visitor=self.visitor,
            cookies=cookies or {},
        )

    def _lead(self, snapshot, request_hash="a" * 64):
        return self.env["crm.lead"].create(
            {
                "name": "Native form test",
                "company_id": self.website.company_id.id,
                "marketing_native_website_id": self.website.id,
                "marketing_native_event_id": snapshot["payload"]["event_id"],
                "marketing_native_request_hash": request_hash,
                "marketing_native_snapshot": snapshot,
                "marketing_native_capture_state": "pending",
                **self.service._native_utm_values(snapshot, {}),
            }
        )

    def test_default_native_freezes_one_acquisition_before_form(self):
        self.assertEqual(self.binding.capture_mode, "native")
        now = fields.Datetime.now()
        entry = self._track(
            "/lp?gad_campaignid=123&gclid=click-original",
            now - datetime.timedelta(minutes=10),
        )
        self._track("/contactus", now - datetime.timedelta(minutes=2))
        self._track(
            "/lp?gad_campaignid=999&gclid=click-future",
            now + datetime.timedelta(seconds=1),
        )
        snapshot = self._snapshot(now=now)
        self.assertEqual(snapshot["track_id"], entry.id)
        self.assertEqual(snapshot["payload"]["gclid"], "click-original")
        self.assertEqual(snapshot["payload"]["gad_campaignid"], "123")
        self.assertNotIn("session_ref", snapshot["payload"])
        self.assertNotIn("?", snapshot["payload"]["landing_url"])
        self.assertNotEqual(
            snapshot["payload"]["occurred_at"], snapshot["payload"]["acquisition_at"]
        )

    def test_same_path_other_click_does_not_invent_acquisition_date(self):
        now = fields.Datetime.now()
        self._track("/contactus?gclid=other-tab", now - datetime.timedelta(minutes=2))
        snapshot = self._snapshot(
            referrer=self.origin + "/contactus?gclid=this-tab", now=now
        )
        self.assertEqual(snapshot["payload"]["gclid"], "this-tab")
        self.assertNotIn("acquisition_at", snapshot["payload"])
        self.assertFalse(snapshot["track_id"])

    def test_informational_capture_and_native_utm_do_not_depend_on_cookie_choice(self):
        url = (
            self.origin
            + "/contactus?utm_source=Newsletter&utm_medium=email&utm_campaign=September"
        )
        expected = None
        for cookie in (
            {},
            {"website_cookies_bar": '{"optional":false}'},
            {"website_cookies_bar": '{"optional":true}'},
        ):
            snapshot = self._snapshot(referrer=url, cookies=cookie)
            values = self.service._native_utm_values(snapshot, {})
            if expected is None:
                expected = values
            self.assertEqual(values, expected)
            lead = self._lead(snapshot)
            self.assertEqual(lead.source_id.name, "Newsletter")
            self.assertEqual(lead.medium_id.name, "email")
            self.assertEqual(lead.campaign_id.name, "September")
            self.assertFalse(lead.marketing_utm_manual)
        self.assertNotIn(
            "campaign_id",
            self.service._native_utm_values(snapshot, {"campaign_id": "42"}),
        )
        snapshot["payload"] = {"gad_campaignid": "123456"}
        self.assertEqual(
            self.service._native_utm_values(snapshot, {}),
            {
                "campaign_id": False,
                "source_id": False,
                "medium_id": self.env.ref("utm.utm_medium_website").id,
            },
        )

    def test_policy_pause_and_individual_refusal_do_not_capture(self):
        self.endpoint.capture_enabled = False
        self.assertEqual(self._snapshot(), {})
        self.endpoint.write(
            {
                "capture_enabled": True,
                "website_tracking_policy": "individual_consent",
                "privacy_legal_basis_code": "consent",
            }
        )
        self.assertEqual(self._snapshot(), {})

    def test_partial_captured_defaults_allow_catalog_completion(self):
        snapshot = self._snapshot(
            referrer=self.origin
            + "/contactus?utm_source=google&utm_medium=cpc&gad_campaignid=123"
        )
        lead = self._lead(snapshot)
        assigned = self.service._native_utm_values(snapshot, {})
        self.service._mark_native_utm_defaults(lead, assigned, {})
        self.assertEqual(lead.marketing_utm_default_json, lead._native_utm_values())
        self.assertFalse(lead.marketing_utm_manual)
        self.assertTrue(self.service._capture_native_submission(lead))
        campaign = self.env["utm.campaign"].create({"name": "Catalog campaign"})
        source = self.env["utm.source"].create({"name": "Catalog Google"})
        resolved = {
            "state": "ready",
            "reason": "confirmed_catalog",
            "entity_id": False,
            "campaign_id": campaign.id,
            "utm_source_id": source.id,
            "medium_id": lead.medium_id.id,
            "source_mode": "apply",
        }
        with patch.object(
            type(self.env["marketing.native.utm.service"]),
            "_resolve_touchpoint",
            return_value=resolved,
        ):
            result = self.env["marketing.crm.native.utm.service"]._classify(
                lead, apply=True
            )
        self.assertEqual(result["state"], "applied")
        self.assertEqual(lead.campaign_id, campaign)
        self.assertEqual(lead.source_id, source)
        self.assertFalse(lead.marketing_utm_manual)

    def test_submitted_fields_and_named_campaign_keep_native_ownership(self):
        snapshot = self._snapshot(
            referrer=self.origin + "/contactus?utm_source=google&utm_medium=cpc"
        )
        lead = self._lead(snapshot)
        assigned = self.service._native_utm_values(snapshot, {})
        self.service._mark_native_utm_defaults(lead, assigned, {"source_id": "42"})
        self.assertFalse(lead.marketing_utm_default_json)
        snapshot = self._snapshot(
            referrer=self.origin + "/contactus?utm_campaign=Explicit&utm_source=google"
        )
        lead = self._lead(snapshot)
        self.service._mark_native_utm_defaults(
            lead, self.service._native_utm_values(snapshot, {}), {}
        )
        self.assertFalse(lead.marketing_utm_default_json)

    def test_one_event_and_touchpoint_reuse_native_lead_without_intent(self):
        now = fields.Datetime.now()
        self._track("/contactus?gclid=captured-click&utm_source=Newsletter", now)
        snapshot = self._snapshot(now=now)
        lead = self._lead(snapshot)
        before = self.env["marketing.website.crm.intent"].search_count([])
        self.assertTrue(self.service._capture_native_submission(lead))
        self.assertTrue(self.service._capture_native_submission(lead))
        self.assertEqual(lead.marketing_native_capture_state, "done")
        self.assertNotIn("gclid", lead.marketing_native_snapshot["payload"])
        self.assertTrue(lead.marketing_native_snapshot["event_ref"])
        links = self.env["marketing.website.crm.correlation"].search(
            [("lead_id", "=", lead.id)]
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links.touchpoint_id.touchpoint_type, "form_submission")
        self.assertEqual(
            self.env["marketing.website.crm.intent"].search_count([]), before
        )
        self.assertEqual(
            self.service._native_existing_submission(
                self.website, lead.marketing_native_event_id, "a" * 64
            ),
            lead,
        )
        with self.assertRaises(ValidationError):
            self.service._native_existing_submission(
                self.website, lead.marketing_native_event_id, "b" * 64
            )
        with self.assertRaises(AccessError):
            lead.write({"marketing_native_snapshot": {}})

    def test_marketing_failure_keeps_lead_and_recovery_uses_frozen_snapshot(self):
        now = fields.Datetime.now()
        entry = self._track("/lp?gclid=original", now - datetime.timedelta(minutes=3))
        self._track("/contactus", now - datetime.timedelta(minutes=1))
        lead = self._lead(self._snapshot(now=now))
        controller = website_form.MarketingWebsiteCrmFormController()
        fake = SimpleNamespace(env=self.env)
        with patch.object(website_form, "request", fake), patch.object(
            type(self.service),
            "_capture_native_submission",
            side_effect=RuntimeError("temporary"),
        ):
            controller._capture_native_lead(lead)
        self.assertTrue(lead.exists())
        self.assertEqual(lead.marketing_native_capture_state, "error")
        self._track("/contactus?gclid=later", now + datetime.timedelta(seconds=1))
        self.service._cron_recover_native_submissions(now=now)
        self.assertEqual(lead.marketing_native_capture_state, "done")
        self.assertEqual(lead.marketing_native_snapshot["track_id"], entry.id)
        links = self.env["marketing.website.crm.correlation"].search(
            [("lead_id", "=", lead.id)]
        )
        values = self.env["marketing.web.ingress.click.value"].search(
            [("event_id", "=", links.event_id.id)]
        )
        self.assertEqual(values.protected_value, "original")

    def test_expired_recovery_stops_retrying_and_keeps_snapshot(self):
        now = fields.Datetime.now()
        lead = self._lead(self._snapshot(now=now))
        after = now + datetime.timedelta(
            seconds=self.endpoint.replay_window_seconds + 1
        )
        self.service._cron_recover_native_submissions(now=after)
        self.assertEqual(lead.marketing_native_capture_state, "skipped")
        self.assertEqual(lead.marketing_native_capture_reason, "capture_window_expired")
        self.assertTrue(lead.marketing_native_snapshot)

    def test_unrelated_query_data_never_enters_snapshot(self):
        self.assertEqual(
            acquisition_values(
                self.origin + "/contactus?gclid=good-click&email=private@example.test"
                "&gad_campaignid=123&gad_source=1&utm_source=A&utm_source=B"
            ),
            {"gclid": "good-click", "gad_campaignid": "123", "gad_source": "1"},
        )
        self.assertEqual(
            self._snapshot(referrer="https://foreign.example/contactus?gclid=other"), {}
        )

    def test_native_mode_rejects_legacy_browser_capture(self):
        snapshot = self._snapshot()
        payload = dict(snapshot["payload"], event_type="entry_point")
        for key in ("action_ref", "route_ref", "model_ref"):
            payload.pop(key)
        with self.assertRaises(AccessError):
            self.env["marketing.web.ingress.service"]._ingest_payload(
                self.endpoint,
                payload,
                origin=self.origin,
                ingress_provenance="browser_capability",
            )
