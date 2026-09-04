import datetime
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN


class TestMarketingWebIngress(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Website laboratory",
                "company_id": cls.env.company.id,
                "allowed_origins": "https://www.soloz.example",
                "allowed_hosts": "www.soloz.example",
            }
        )
        cls.service = cls.env["marketing.web.ingress.service"].sudo()
        cls.observed_at = datetime.datetime(2026, 9, 1, 12, 0)

    def _payload(self, event_id="event-000000000001", **overrides):
        payload = {
            "event_id": event_id,
            "event_type": "entry_point",
            "occurred_at": "2026-09-01T12:00:00Z",
            "landing_url": "https://www.soloz.example/solar?utm_source=google",
            "referrer_url": "https://google.example/search?q=solar",
            "utm_source": "google",
            "utm_medium": "cpc",
            "gclid": "gclid-private-001",
            "session_ref": "session-private-001",
        }
        payload.update(overrides)
        return payload

    def _ingest(self, payload=None, **kwargs):
        return self.service._ingest_payload(
            self.endpoint,
            payload or self._payload(),
            origin=kwargs.pop("origin", "https://www.soloz.example"),
            observed_at=kwargs.pop("observed_at", self.observed_at),
            body_size_bytes=kwargs.pop("body_size_bytes", 512),
            **kwargs,
        )

    def test_atomic_ingestion_projects_dto_and_protected_value_ref(self):
        result = self._ingest()
        self.assertEqual(result.disposition, "accepted")
        event = self.env["marketing.web.ingress.event"].browse(
            self.env["marketing.web.ingress.event"].search([], limit=1).id
        )
        self.assertEqual(event.state, "done")
        self.assertEqual(event.ingress_provenance, "server_internal")
        self.assertEqual(event.touchpoint_id.id, result.touchpoint_id)
        touchpoint = event.touchpoint_id
        self.assertEqual(touchpoint.source_system, "web.ingress")
        self.assertEqual(touchpoint.touchpoint_type, "entry_point")
        self.assertEqual(touchpoint.evidence_level, "first_party")
        self.assertEqual(
            touchpoint.extensions_json["web_ingress.provenance"],
            "server_internal",
        )
        self.assertEqual(touchpoint.source_schema_version, "marketing.web.ingress.v1")
        self.assertEqual(touchpoint.mapping_version, 1)
        self.assertEqual(
            touchpoint.asset_refs_json, {"web.endpoint": self.endpoint.public_ref}
        )
        self.assertEqual(touchpoint.consent_state, "unknown")
        self.assertFalse(touchpoint.privacy_decision_source)
        self.assertFalse(touchpoint.privacy_decided_at)
        self.assertEqual(touchpoint.landing_url, "https://www.soloz.example/solar")
        self.assertEqual(touchpoint.referrer_url, "https://google.example/")
        click_identifier = touchpoint.identifier_ids.filtered(
            lambda item: item.namespace == "google.gclid"
        )
        vault = event.click_value_ids
        self.assertEqual(len(vault), 1)
        self.assertEqual(click_identifier.value_ref, vault.value_ref)
        self.assertEqual(click_identifier.role, "click")
        self.assertEqual(click_identifier.comparison_hash, vault.comparison_hash)
        session_identifier = touchpoint.identifier_ids.filtered(
            lambda item: item.namespace == "web.session"
        )
        self.assertEqual(session_identifier.role, "session")
        self.assertEqual(vault.protected_value, "gclid-private-001")
        safe_records = json.dumps(
            {
                "event": event.read()[0],
                "touchpoint": touchpoint.read()[0],
                "identifiers": touchpoint.identifier_ids.read(),
            },
            default=str,
        )
        self.assertNotIn("gclid-private-001", safe_records)
        self.assertNotIn("session-private-001", safe_records)

    def test_action_events_project_v2_schema_mapping_and_technical_assets(self):
        form_result = self._ingest(
            self._payload(
                event_id="form-action-event-0001",
                event_type="form_submission",
                action_ref="form.contact.request",
                route_ref="website.contactus",
                model_ref="crm.lead",
            )
        )
        form_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            form_result.touchpoint_id
        )
        self.assertEqual(form_touchpoint.touchpoint_type, "form_submission")
        self.assertEqual(
            form_touchpoint.source_schema_version, "marketing.web.ingress.v2"
        )
        self.assertEqual(form_touchpoint.mapping_version, 2)
        self.assertEqual(
            form_touchpoint.asset_refs_json,
            {
                "web.endpoint": self.endpoint.public_ref,
                "web.action": "form.contact.request",
                "web.route": "website.contactus",
                "web.model": "crm.lead",
            },
        )

        organic_result = self._ingest(
            self._payload(
                event_id="organic-action-event-01",
                event_type="organic_link",
                action_ref="whatsapp.sales",
                route_ref="website.hero.cta",
            )
        )
        organic_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            organic_result.touchpoint_id
        )
        self.assertEqual(organic_touchpoint.touchpoint_type, "organic_link")
        self.assertEqual(
            organic_touchpoint.source_schema_version, "marketing.web.ingress.v2"
        )
        self.assertEqual(organic_touchpoint.mapping_version, 2)
        self.assertEqual(
            organic_touchpoint.asset_refs_json,
            {
                "web.endpoint": self.endpoint.public_ref,
                "web.action": "whatsapp.sales",
                "web.route": "website.hero.cta",
            },
        )

    def test_provenance_cannot_upgrade_a_browser_claim_to_a_confirmed_action(self):
        with self.assertRaises(ValidationError):
            self._ingest(
                self._payload(
                    event_id="browser-action-spoof-001",
                    event_type="form_submission",
                    action_ref="form.contact.request",
                    route_ref="website.contactus",
                    model_ref="crm.lead",
                ),
                ingress_provenance="browser_capability",
            )
        with self.assertRaises(ValidationError):
            self._ingest(
                ingress_provenance="website_confirmed_action",
            )

    def test_public_provenance_cannot_assert_a_consent_decision(self):
        for provenance in ("browser_capability", "website_confirmed_action"):
            with self.subTest(provenance=provenance), self.assertRaises(
                ValidationError
            ):
                payload = self._payload(consent_state="granted")
                if provenance == "website_confirmed_action":
                    payload.update(
                        {
                            "event_type": "form_submission",
                            "action_ref": "form.contact.request",
                            "route_ref": "website.contactus",
                            "model_ref": "crm.lead",
                        }
                    )
                self._ingest(payload, ingress_provenance=provenance)

        result = self._ingest(
            self._payload(
                event_id="server-consent-event-001",
                consent_state="granted",
            ),
            ingress_provenance="server_internal",
        )
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        self.assertEqual(touchpoint.consent_state, "granted")
        self.assertEqual(touchpoint.privacy_decision_source, "server_internal")

    def test_duplicate_event_cannot_upgrade_first_wins_provenance(self):
        payload = self._payload(event_id="provenance-first-wins-001")
        first = self._ingest(payload, ingress_provenance="browser_capability")
        duplicate = self._ingest(payload, ingress_provenance="server_internal")
        self.assertEqual(first.disposition, "accepted")
        self.assertEqual(duplicate.disposition, "duplicate")
        event = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("public_ref", "=", first.event_ref)])
        )
        self.assertEqual(event.ingress_provenance, "browser_capability")
        self.assertEqual(
            event.touchpoint_id.extensions_json["web_ingress.provenance"],
            "browser_capability",
        )

    def test_action_reference_change_is_a_conflicting_event_reuse(self):
        payload = self._payload(
            event_id="action-conflict-event-01",
            event_type="organic_link",
            action_ref="whatsapp.sales",
            route_ref="website.hero.cta",
        )
        accepted = self._ingest(payload)
        conflict = self._ingest({**payload, "action_ref": "whatsapp.support"})
        self.assertEqual(accepted.disposition, "accepted")
        self.assertEqual(conflict.disposition, "conflict")
        self.assertEqual(accepted.event_ref, conflict.event_ref)

    def test_exact_replay_and_conflicting_reuse_are_first_wins(self):
        accepted = self._ingest()
        duplicate = self._ingest()
        conflict = self._ingest(self._payload(utm_campaign="changed"))
        self.assertEqual(accepted.disposition, "accepted")
        self.assertEqual(duplicate.disposition, "duplicate")
        self.assertEqual(conflict.disposition, "conflict")
        self.assertEqual(accepted.event_ref, duplicate.event_ref)
        self.assertEqual(accepted.event_ref, conflict.event_ref)
        self.assertEqual(
            self.env["marketing.web.ingress.event"].sudo().search_count([]), 1
        )
        self.assertEqual(
            self.env["marketing.web.ingress.click.value"].sudo().search_count([]), 1
        )
        self.assertEqual(
            self.env["marketing.attribution.touchpoint"]
            .sudo()
            .search_count([("source_system", "=", "web.ingress")]),
            1,
        )

    def test_replay_window_rejects_old_and_future_events_without_evidence(self):
        for occurred_at in (
            "2026-09-01T11:54:59Z",
            "2026-09-01T12:05:01Z",
        ):
            with self.subTest(occurred_at=occurred_at), self.assertRaises(
                ValidationError
            ):
                self._ingest(self._payload(occurred_at=occurred_at))
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )

    def test_origin_landing_host_and_body_limits_are_enforced(self):
        with self.assertRaises(ValidationError):
            self._ingest(origin="https://attacker.example")
        with self.assertRaises(ValidationError):
            self._ingest(self._payload(landing_url="https://attacker.example/collect"))
        with self.assertRaises(ValidationError):
            self._ingest(body_size_bytes=self.endpoint.max_body_bytes + 1)
        for invalid_size in (True, 1.5, "512"):
            with self.subTest(body_size_bytes=invalid_size), self.assertRaises(
                ValidationError
            ):
                self._ingest(body_size_bytes=invalid_size)
        self.endpoint.write(
            {
                "allowed_origins": (
                    "https://www.soloz.example\nhttps://portal.soloz.example"
                ),
                "allowed_hosts": "www.soloz.example\nportal.soloz.example",
            }
        )
        with self.assertRaises(ValidationError):
            self._ingest(
                self._payload(landing_url="https://portal.soloz.example/solar"),
                origin="https://www.soloz.example",
            )

    def test_admission_limit_is_bounded_payload_free_and_company_scoped(self):
        self.endpoint.write({"rate_limit_per_minute": 60})
        admission = self.env["marketing.web.ingress.admission"].sudo()
        for _index in range(60):
            self.assertTrue(admission._admit(self.endpoint, "public_ingress"))
        self.assertFalse(admission._admit(self.endpoint, "public_ingress"))
        self.assertTrue(admission._admit(self.endpoint, "website_form"))
        self.assertNotIn("payload", admission._fields)
        with self.assertRaises(AccessError):
            admission.search([], limit=1).write({"key_revision": 2})

    def test_admission_gc_removes_only_expired_payload_free_rows(self):
        admission = self.env["marketing.web.ingress.admission"].sudo()
        now = datetime.datetime(2026, 9, 1, 12, 0)
        self.assertTrue(
            admission._admit(
                self.endpoint,
                "public_ingress",
                now=now - datetime.timedelta(minutes=6),
            )
        )
        self.assertTrue(admission._admit(self.endpoint, "public_ingress", now=now))
        with patch(
            "odoo.fields.Datetime.now",
            return_value=now,
        ):
            admission._gc_old_admissions()
        remaining = admission.search([("endpoint_id", "=", self.endpoint.id)])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining.admitted_at, now)

    def test_rotation_revokes_old_public_key_and_advances_fence(self):
        old_key = self.endpoint.public_key
        old_revision = self.endpoint.key_revision
        self.assertTrue(
            self.endpoint._locked_for_public_key(
                old_key, str(self.endpoint.config_revision)
            )
        )
        self.endpoint.action_rotate_public_key()
        self.assertEqual(self.endpoint.key_revision, old_revision + 1)
        self.assertFalse(
            self.endpoint._locked_for_public_key(
                old_key, str(self.endpoint.config_revision)
            )
        )
        self.assertTrue(
            self.endpoint._locked_for_public_key(
                self.endpoint.public_key, str(self.endpoint.config_revision)
            )
        )

    def test_event_and_vault_are_service_owned_and_immutable(self):
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            ._fields["ingress_provenance"]
            .default
        )
        self._ingest()
        event = self.env["marketing.web.ingress.event"].sudo().search([], limit=1)
        vault = event.click_value_ids
        with self.assertRaises(AccessError):
            self.env["marketing.web.ingress.event"].create(
                {
                    "endpoint_id": self.endpoint.id,
                    "event_key_hash": "1" * 64,
                    "request_digest": "2" * 64,
                    "origin": "https://www.soloz.example",
                    "landing_host": "www.soloz.example",
                    "occurred_at": self.observed_at,
                    "observed_at": self.observed_at,
                    "body_size_bytes": 1,
                    "key_revision": 1,
                    "config_revision": 1,
                }
            )
        with self.assertRaises(AccessError):
            self.env["marketing.web.ingress.event"].with_context(
                marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
            ).create(
                {
                    "endpoint_id": self.endpoint.id,
                    "event_key_hash": "3" * 64,
                    "request_digest": "4" * 64,
                    "origin": "https://www.soloz.example",
                    "landing_host": "www.soloz.example",
                    "occurred_at": self.observed_at,
                    "observed_at": self.observed_at,
                    "body_size_bytes": 1,
                    "key_revision": 1,
                    "config_revision": 1,
                    "ingress_provenance": "unclassified",
                }
            )
        with self.assertRaises(AccessError):
            self.env["marketing.web.ingress.event"].sudo().with_context(
                marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
            ).create(
                {
                    "endpoint_id": self.endpoint.id,
                    "event_key_hash": "5" * 64,
                    "request_digest": "6" * 64,
                    "origin": "https://www.soloz.example",
                    "landing_host": "www.soloz.example",
                    "occurred_at": self.observed_at,
                    "observed_at": self.observed_at,
                    "body_size_bytes": 1,
                    "key_revision": 1,
                    "config_revision": 1,
                    "ingress_provenance": "server_internal",
                    "state": "done",
                }
            )
        with self.assertRaises(AccessError):
            event.write({"origin": "https://attacker.example"})
        with self.assertRaises(AccessError):
            event.unlink()
        with self.assertRaises(AccessError):
            vault.write({"protected_value": "changed"})
        with self.assertRaises(AccessError):
            self.endpoint.unlink()

    def test_failed_projection_rolls_event_and_vault_back_atomically(self):
        with patch.object(
            type(self.service),
            "_touchpoint_dto",
            side_effect=RuntimeError("synthetic projection failure"),
        ), self.assertRaises(RuntimeError):
            self._ingest(self._payload(event_id="atomic-rollback-0001"))
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )
        self.assertFalse(
            self.env["marketing.web.ingress.click.value"].sudo().search_count([])
        )
        self.assertFalse(
            self.env["marketing.attribution.touchpoint"]
            .sudo()
            .search_count([("source_system", "=", "web.ingress")])
        )

    def test_non_admin_cannot_read_configuration_events_or_vault(self):
        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Ingress viewer",
                    "login": "web-ingress-viewer",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, [self.env.company.id])],
                    "groups_id": [(6, 0, [viewer_group.id])],
                }
            )
        )
        self._ingest()
        for model_name in (
            "marketing.web.ingress.endpoint",
            "marketing.web.ingress.event",
            "marketing.web.ingress.click.value",
            "marketing.web.ingress.admission",
        ):
            with self.subTest(model=model_name), self.assertRaises(AccessError):
                self.env[model_name].with_user(user).search([]).read(["id"])
        with self.assertRaises(AccessError):
            self.endpoint.with_user(user).action_rotate_public_key()

    def test_company_is_fenced_through_endpoint_and_touchpoint(self):
        second_company = self.env["res.company"].create({"name": "Ingress Company B"})
        endpoint = (
            self.env["marketing.web.ingress.endpoint"]
            .sudo()
            .with_company(second_company)
            .create(
                {
                    "name": "Company B endpoint",
                    "company_id": second_company.id,
                    "allowed_origins": "https://b.soloz.example",
                    "allowed_hosts": "b.soloz.example",
                }
            )
        )
        restricted_service = self.service.with_context(
            allowed_company_ids=[self.env.company.id]
        ).with_company(self.env.company)
        with self.assertRaises(AccessError):
            restricted_service._ingest_payload(
                endpoint,
                self._payload(
                    event_id="company-b-rejected-01",
                    landing_url="https://b.soloz.example/solar",
                ),
                origin="https://b.soloz.example",
                observed_at=self.observed_at,
                body_size_bytes=512,
            )
        result = (
            self.service.with_context(allowed_company_ids=[second_company.id])
            .with_company(second_company)
            ._ingest_payload(
                endpoint,
                self._payload(
                    event_id="company-b-event-01",
                    landing_url="https://b.soloz.example/solar",
                ),
                origin="https://b.soloz.example",
                observed_at=self.observed_at,
                body_size_bytes=512,
            )
        )
        event = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("public_ref", "=", result.event_ref)])
        )
        self.assertEqual(event.company_id, second_company)
        self.assertEqual(event.touchpoint_id.company_id, second_company)

    def test_same_event_identifier_is_independent_between_endpoints(self):
        second_endpoint = self.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Second website endpoint",
                "company_id": self.env.company.id,
                "allowed_origins": "https://www.soloz.example",
                "allowed_hosts": "www.soloz.example",
            }
        )
        first = self._ingest()
        second = self.service._ingest_payload(
            second_endpoint,
            self._payload(),
            origin="https://www.soloz.example",
            observed_at=self.observed_at,
            body_size_bytes=512,
        )
        self.assertEqual(first.disposition, "accepted")
        self.assertEqual(second.disposition, "accepted")
        self.assertNotEqual(first.event_ref, second.event_ref)
        self.assertEqual(
            self.env["marketing.web.ingress.event"].sudo().search_count([]), 2
        )
