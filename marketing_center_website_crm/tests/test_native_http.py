"""Exercise the public Website CRM route through its complete HTTP/MRO stack."""

import uuid
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import config


@tagged("-at_install", "post_install")
class TestNativeWebsiteSubmissionHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.website.company_id.marketing_business_events_enabled = False
        local = urlsplit(cls.base_url())
        cls.proxy_host = local.netloc
        cls.origin = urlunsplit(("https", local.netloc, "", "", ""))
        cls.website.domain = cls.origin
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Native public CRM HTTP",
                "company_id": cls.website.company_id.id,
                "allowed_origins": cls.origin,
                "allowed_hosts": local.hostname,
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "website_tracking_policy": "informational_notice",
                "privacy_legal_basis_code": False,
                "privacy_policy_version": "native-http-v1",
                "privacy_notice_version": "native-http-v1",
                "privacy_policy_justification": "Synthetic HTTP regression fixture",
                "identifier_retention_days": 30,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write(
                {
                    "endpoint_id": cls.endpoint.id,
                    "active": True,
                    "capture_mode": "native",
                }
            )
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
                    "capture_mode": "native",
                }
            )
        cls.form_model = cls.env["ir.model"]._get("crm.lead")
        cls.form_model.website_form_access = True
        cls.env["marketing.website.action"].search(
            [
                ("binding_id", "=", cls.binding.id),
                ("source_path", "=", "/contactus"),
                ("kind", "=", "form_submission"),
                ("form_model_id", "=", cls.form_model.id),
            ]
        ).write({"active": False})
        cls.action = cls.env["marketing.website.action"].create(
            {
                "name": "Native public CRM HTTP form",
                "binding_id": cls.binding.id,
                "kind": "form_submission",
                "route_ref": "native.http.contact",
                "source_path": "/contactus",
                "form_model_id": cls.form_model.id,
            }
        )

    def setUp(self):
        super().setUp()
        # The real HTTP server terminates no TLS in HttpCase. Exercise Odoo's
        # actual trusted-proxy handling, as in production, rather than replacing
        # request objects or skipping the HTTPS guard in the controller.
        proxy = patch.dict(config.options, {"proxy_mode": True})
        proxy.start()
        self.addCleanup(proxy.stop)
        self.submission_id = str(uuid.uuid4())
        self.form_values = {
            "name": "Native HTTP submission %s" % self.submission_id,
            "contact_name": "Synthetic website contact",
            "email_from": "native-http@example.invalid",
            "description": "Synthetic acquisition regression",
        }

    def _post_form(self, *, query=None, values=None):
        referrer_query = query or (
            "utm_source=HTTPSource&utm_medium=email&utm_campaign=HTTPSeptember"
            "&gad_campaignid=23172115632&gclid=http-original-click"
        )
        response = self.opener.post(
            self.base_url() + "/website/form/crm.lead?mc_event=" + self.submission_id,
            data=self.form_values if values is None else values,
            headers={
                "Origin": self.origin,
                "Referer": self.origin + "/contactus?" + referrer_query,
                "X-Forwarded-Host": self.proxy_host,
                "X-Forwarded-Proto": "https",
                "Sec-Fetch-Site": "same-origin",
            },
            allow_redirects=False,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsInstance(payload, dict, response.text)
        self.assertNotIn("error", payload, response.text)
        self.assertNotIn("error_fields", payload, response.text)
        self.assertEqual(payload.get("marketing_center_event_id"), self.submission_id)
        self.assertIs(type(payload.get("id")), int, response.text)
        self.env.invalidate_all()
        return payload, self.env["crm.lead"].browse(payload["id"]).exists()

    def _assert_one_capture(self, lead, initial_intents):
        self.assertEqual(lead.marketing_native_capture_state, "done")
        self.assertEqual(
            self.env["crm.lead"]
            .with_context(active_test=False)
            .search_count(
                [
                    ("marketing_native_website_id", "=", self.website.id),
                    ("marketing_native_event_id", "=", self.submission_id),
                ]
            ),
            1,
        )
        events = self.env["marketing.web.ingress.event"].search(
            [("endpoint_id", "=", self.endpoint.id)]
        )
        self.assertEqual(len(events), 1)
        correlations = self.env["marketing.website.crm.correlation"].search(
            [("lead_id", "=", lead.id)]
        )
        self.assertEqual(len(correlations), 1)
        self.assertEqual(correlations.event_id, events)
        self.assertEqual(correlations.touchpoint_id, events.touchpoint_id)
        self.assertEqual(events.touchpoint_id.touchpoint_type, "form_submission")
        self.assertEqual(
            events.touchpoint_id.asset_refs_json["campaign_id"], "23172115632"
        )
        self.assertEqual(
            self.env["marketing.website.crm.intent"].search_count([]), initial_intents
        )
        self.assertFalse(lead.marketing_business_event_link_ids)
        return events

    def test_real_post_and_uuid_retry_create_one_native_lead_and_touchpoint(self):
        initial_intents = self.env["marketing.website.crm.intent"].search_count([])
        first, lead = self._post_form()
        self.assertTrue(lead)
        self.assertEqual(lead.name, self.form_values["name"])
        self.assertEqual(lead.company_id, self.website.company_id)
        self.assertEqual(lead.source_id.name, "HTTPSource")
        self.assertEqual(lead.medium_id.name, "email")
        self.assertEqual(lead.campaign_id.name, "HTTPSeptember")
        events = self._assert_one_capture(lead, initial_intents)

        # A later page/campaign must not replace the acquisition of the original
        # successful submission when its opaque id is replayed.
        second, replay_lead = self._post_form(
            query="utm_source=LaterSource&gad_campaignid=999&gclid=later-click"
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(replay_lead, lead)
        self.assertEqual(self._assert_one_capture(lead, initial_intents), events)
        click = self.env["marketing.web.ingress.click.value"].search(
            [("event_id", "=", events.id), ("namespace", "=", "google.gclid")]
        )
        self.assertEqual(click.protected_value, "http-original-click")

        # Pausing measurement after CRM accepted the form must not turn a retry
        # of a lost HTTP response into a second lead without an acquisition key.
        self.binding.active = False
        third, paused_replay_lead = self._post_form()
        self.assertEqual(third["id"], first["id"])
        self.assertEqual(paused_replay_lead, lead)
        self.assertEqual(self._assert_one_capture(lead, initial_intents), events)
        self.assertEqual(
            self.env["crm.lead"].search_count(
                [("name", "=", self.form_values["name"])]
            ),
            1,
        )

    def test_real_post_survives_capture_failure_and_recovers_without_another_lead(self):
        initial_intents = self.env["marketing.website.crm.intent"].search_count([])
        service = self.env["marketing.website.crm.service"]
        with patch.object(
            type(service),
            "_capture_native_submission",
            side_effect=RuntimeError("synthetic temporary acquisition failure"),
        ):
            first, lead = self._post_form()
        self.assertTrue(lead)
        self.assertEqual(lead.marketing_native_capture_state, "error")
        self.assertEqual(
            lead.marketing_native_snapshot["payload"]["gclid"], "http-original-click"
        )
        self.assertFalse(
            self.env["marketing.web.ingress.event"].search_count(
                [("endpoint_id", "=", self.endpoint.id)]
            )
        )
        service._cron_recover_native_submissions()
        self.env.invalidate_all()
        events = self._assert_one_capture(lead, initial_intents)
        second, replay_lead = self._post_form()
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(replay_lead, lead)
        self.assertEqual(self._assert_one_capture(lead, initial_intents), events)

    def test_real_post_new_google_click_does_not_inherit_old_utm_cookies(self):
        initial_intents = self.env["marketing.website.crm.intent"].search_count([])
        self.opener.cookies.update(
            {
                "odoo_utm_campaign": "OldCookieCampaign",
                "odoo_utm_source": "OldCookieSource",
                "odoo_utm_medium": "OldCookieMedium",
            }
        )

        _, lead = self._post_form(query="gad_campaignid=23172115632&gclid=new-click")

        self.assertTrue(lead)
        self.assertFalse(lead.campaign_id)
        self.assertFalse(lead.source_id)
        self.assertEqual(lead.medium_id, self.env.ref("utm.utm_medium_website"))
        events = self._assert_one_capture(lead, initial_intents)
        payload = lead.marketing_native_snapshot["payload"]
        self.assertEqual(payload["gad_campaignid"], "23172115632")
        for field in ("utm_campaign", "utm_source", "utm_medium"):
            self.assertNotIn(field, payload)
        click = self.env["marketing.web.ingress.click.value"].search(
            [("event_id", "=", events.id), ("namespace", "=", "google.gclid")]
        )
        self.assertEqual(click.protected_value, "new-click")
