import datetime
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.tokens import WEBSITE_ACTION_INTERNAL_TOKEN


class TestMarketingWebsiteAction(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "capture_enabled": True,
                "capture_purpose": "web_attribution",
                "privacy_policy_version": "test-v1",
                "privacy_notice_version": "test-v1",
                "privacy_legal_basis_code": "documented_test_basis",
                "privacy_policy_justification": "Synthetic test policy.",
                "identifier_retention_days": 30,
                "name": "Website action endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": "https://www.example.test",
                "allowed_hosts": "www.example.test",
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
        cls.form_model = cls.env["ir.model"]._get("res.partner")
        cls.form_model.write({"website_form_access": True})

    def _form_action(self, **overrides):
        values = {
            "name": "Contact form",
            "binding_id": self.binding.id,
            "kind": "form_submission",
            "route_ref": "form.contact",
            "source_path": "/contactus",
            "form_model_id": self.form_model.id,
        }
        values.update(overrides)
        return self.env["marketing.website.action"].create(values)

    def _whatsapp_action(self, **overrides):
        values = {
            "name": "Sales WhatsApp",
            "binding_id": self.binding.id,
            "kind": "whatsapp_handoff",
            "route_ref": "whatsapp.sales",
            "source_path": "/contactus",
            "whatsapp_destination": "5519999999999",
            "fallback_path": "/contactus",
        }
        values.update(overrides)
        return self.env["marketing.website.action"].create(values)

    def test_action_identity_is_immutable_and_kind_fields_are_exclusive(self):
        action = self._form_action()
        self.assertIn(action.public_ref, action.marker_attribute)
        with self.assertRaises(AccessError):
            action.write({"route_ref": "form.other"})
        with self.assertRaises(AccessError):
            action.unlink()
        with self.assertRaises(ValidationError):
            self._form_action(
                route_ref="form.invalid",
                whatsapp_destination="5519999999999",
            )
        with self.assertRaises(ValidationError):
            self._whatsapp_action(
                route_ref="whatsapp.invalid",
                form_model_id=self.form_model.id,
            )

    def test_redirect_grant_is_hashed_expiring_and_single_use(self):
        action = self._whatsapp_action()
        grant_model = self.env["marketing.website.redirect.grant"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        raw_token = grant_model._issue(
            action,
            "11111111-1111-4111-8111-111111111111",
            now=now,
        )
        self.assertFalse(grant_model.search([("token_hash", "=", raw_token)]))
        grant = grant_model.search([("action_id", "=", action.id)])
        self.assertEqual(len(grant.token_hash), 64)
        self.assertNotIn(raw_token, grant.token_hash)

        resolved, fallback = grant_model._consume(
            raw_token,
            self.website,
            now=now + datetime.timedelta(seconds=1),
        )
        self.assertEqual(resolved, action)
        self.assertEqual(fallback, "/contactus")
        resolved, fallback = grant_model._consume(
            raw_token,
            self.website,
            now=now + datetime.timedelta(seconds=2),
        )
        self.assertFalse(resolved)
        self.assertEqual(fallback, "/contactus")

    def test_archived_handoff_revokes_redirect_and_refuses_new_grant(self):
        action = self._whatsapp_action()
        grant_model = self.env["marketing.website.redirect.grant"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        raw_token = grant_model._issue(
            action,
            "99999999-9999-4999-8999-999999999999",
            now=now,
        )
        action.write({"active": False})
        resolved, fallback = grant_model._consume(
            raw_token,
            self.website,
            now=now + datetime.timedelta(seconds=1),
        )
        self.assertFalse(resolved)
        self.assertEqual(fallback, "/contactus")
        with self.assertRaises(AccessError):
            grant_model._issue(
                action,
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                now=now,
            )

    def test_replayed_handoff_keeps_exactly_one_issued_redirect_grant(self):
        action = self._whatsapp_action()
        grant_model = self.env["marketing.website.redirect.grant"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        event_id = "abababab-abab-4bab-8bab-abababababab"
        first_token = grant_model._issue(action, event_id, now=now)
        second_token = grant_model._issue(
            action,
            event_id,
            now=now + datetime.timedelta(seconds=1),
        )
        self.assertNotEqual(first_token, second_token)
        grants = grant_model.search([("action_id", "=", action.id)])
        self.assertEqual(len(grants.filtered(lambda grant: grant.state == "issued")), 1)
        self.assertEqual(
            len(grants.filtered(lambda grant: grant.state == "revoked")), 1
        )
        rejected, fallback = grant_model._consume(
            first_token,
            self.website,
            now=now + datetime.timedelta(seconds=2),
        )
        accepted, _fallback = grant_model._consume(
            second_token,
            self.website,
            now=now + datetime.timedelta(seconds=2),
        )
        self.assertFalse(rejected)
        self.assertEqual(fallback, "/contactus")
        self.assertEqual(accepted, action)

    def test_expired_redirect_grants_are_removed_by_bounded_gc(self):
        action = self._whatsapp_action()
        grant_model = self.env["marketing.website.redirect.grant"]
        issued_at = datetime.datetime(2026, 9, 1, 10, 0)
        grant_model._issue(
            action,
            "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc",
            now=issued_at,
        )
        self.assertTrue(grant_model.search([("action_id", "=", action.id)]))
        with patch(
            "odoo.fields.Datetime.now",
            return_value=issued_at + datetime.timedelta(hours=2),
        ):
            grant_model._gc_expired_grants()
        self.assertFalse(grant_model.search([("action_id", "=", action.id)]))

    def test_whatsapp_claim_retries_preserve_first_timestamp_and_reject_changes(self):
        action = self._whatsapp_action()
        service = self.env["marketing.website.action.service"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        claim = {
            "action_ref": action.public_ref,
            "event_id": "abababab-abab-4bab-8bab-abababababab",
            "session_ref": "22222222-2222-4222-8222-222222222222",
        }
        accepted, first_token = service._claim_whatsapp_handoff(
            self.website, claim, "https://www.example.test", now=now
        )
        replay, second_token = service._claim_whatsapp_handoff(
            self.website,
            claim,
            "https://www.example.test",
            now=now + datetime.timedelta(seconds=5),
        )
        self.assertEqual(accepted.disposition, "accepted")
        self.assertEqual(replay.disposition, "duplicate")
        self.assertEqual(replay.touchpoint_id, accepted.touchpoint_id)
        self.assertNotEqual(first_token, second_token)
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            accepted.touchpoint_id
        )
        self.assertEqual(touchpoint.occurred_at, now)
        conflict, token = service._claim_whatsapp_handoff(
            self.website,
            {**claim, "session_ref": "33333333-3333-4333-8333-333333333333"},
            "https://www.example.test",
            now=now + datetime.timedelta(seconds=10),
        )
        self.assertEqual(conflict.disposition, "conflict")
        self.assertFalse(token)

    def test_whatsapp_grant_failure_rolls_back_ingress_and_touchpoint(self):
        action = self._whatsapp_action()
        Event = self.env["marketing.web.ingress.event"]
        Touchpoint = self.env["marketing.attribution.touchpoint"]
        before_events = Event.search_count([])
        before_touchpoints = Touchpoint.search_count([])
        with patch.object(
            type(self.env["marketing.website.redirect.grant"]),
            "_issue",
            side_effect=ValidationError("Injected grant failure"),
        ), self.assertRaises(ValidationError):
            self.env["marketing.website.action.service"]._claim_whatsapp_handoff(
                self.website,
                {
                    "action_ref": action.public_ref,
                    "event_id": "abababab-abab-4bab-8bab-abababababab",
                    "session_ref": "22222222-2222-4222-8222-222222222222",
                },
                "https://www.example.test",
            )
        self.assertEqual(Event.search_count([]), before_events)
        self.assertEqual(Touchpoint.search_count([]), before_touchpoints)

    def test_redirect_grant_lifecycle_rejects_arbitrary_internal_mutation(self):
        action = self._whatsapp_action()
        grant_model = self.env["marketing.website.redirect.grant"]
        raw_token = grant_model._issue(
            action,
            "abababab-abab-4bab-8bab-abababababab",
        )
        grant = grant_model.search([("action_id", "=", action.id)], limit=1)
        internal = grant.with_context(
            marketing_website_action_internal=WEBSITE_ACTION_INTERNAL_TOKEN
        )
        with self.assertRaises(AccessError):
            internal.write({"token_hash": "f" * 64})
        accepted, _fallback = grant_model._consume(raw_token, self.website)
        self.assertEqual(accepted, action)
        with self.assertRaises(AccessError):
            internal.write({"state": "revoked"})

    def test_form_receipt_and_whatsapp_claim_use_same_ingress_ledger(self):
        form_action = self._form_action()
        whatsapp_action = self._whatsapp_action()
        service = self.env["marketing.website.action.service"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        form_claim = {
            "action_ref": form_action.public_ref,
            "event_id": "11111111-1111-4111-8111-111111111111",
            "session_ref": "22222222-2222-4222-8222-222222222222",
        }
        receipt = service._prepare_form_receipt(
            self.website,
            self.form_model.model,
            form_claim,
            "https://www.example.test",
            now=now,
        )
        result = service._exchange_form_receipt(
            self.website,
            {**form_claim, "receipt": receipt},
            "https://www.example.test",
            now=now + datetime.timedelta(seconds=1),
        )
        self.assertEqual(result.disposition, "accepted")
        replay = service._exchange_form_receipt(
            self.website,
            {**form_claim, "receipt": receipt},
            "https://www.example.test",
            now=now + datetime.timedelta(seconds=2),
        )
        self.assertEqual(replay.disposition, "duplicate")

        whatsapp_result, raw_token = service._claim_whatsapp_handoff(
            self.website,
            {
                "action_ref": whatsapp_action.public_ref,
                "event_id": "33333333-3333-4333-8333-333333333333",
                "session_ref": form_claim["session_ref"],
            },
            "https://www.example.test",
            now=now,
        )
        self.assertEqual(whatsapp_result.disposition, "accepted")
        self.assertTrue(raw_token)
        events = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(
            set(events.mapped("ingress_provenance")),
            {"website_confirmed_action"},
        )
        touchpoints = events.mapped("touchpoint_id")
        self.assertEqual(
            set(touchpoints.mapped("touchpoint_type")),
            {"form_submission", "organic_link"},
        )
        session_hashes = {
            identifier.comparison_hash
            for identifier in touchpoints.mapped("identifier_ids")
            if identifier.namespace == "web.session"
        }
        self.assertEqual(len(session_hashes), 1)

    def test_tampered_or_expired_form_receipt_is_rejected_without_event(self):
        action = self._form_action()
        service = self.env["marketing.website.action.service"]
        now = datetime.datetime(2026, 9, 1, 12, 0)
        claim = {
            "action_ref": action.public_ref,
            "event_id": "44444444-4444-4444-8444-444444444444",
            "session_ref": "55555555-5555-4555-8555-555555555555",
        }
        receipt = service._prepare_form_receipt(
            self.website,
            self.form_model.model,
            claim,
            "https://www.example.test",
            now=now,
        )
        tampered = receipt[:-1] + ("a" if receipt[-1] != "a" else "b")
        with self.assertRaises(AccessError):
            service._exchange_form_receipt(
                self.website,
                {**claim, "receipt": tampered},
                "https://www.example.test",
                now=now,
            )
        with self.assertRaises(AccessError):
            service._exchange_form_receipt(
                self.website,
                {**claim, "receipt": receipt},
                "https://www.example.test",
                now=now + datetime.timedelta(minutes=3),
            )
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_action_and_grant_acl_follow_active_companies(self):
        action = self._whatsapp_action()
        self.env["marketing.website.redirect.grant"]._issue(
            action,
            "77777777-7777-4777-8777-777777777777",
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Website action viewer",
                    "login": "website-action-viewer",
                    "company_id": self.website.company_id.id,
                    "company_ids": [(6, 0, [self.website.company_id.id])],
                    "groups_id": [
                        (
                            6,
                            0,
                            [
                                self.env.ref(
                                    "marketing_center_base.group_marketing_center_viewer"
                                ).id
                            ],
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            action.with_user(viewer).read(["route_ref"])
        with self.assertRaises(AccessError):
            self.env["marketing.website.redirect.grant"].with_user(viewer).search([])

        other_company = self.env["res.company"].create({"name": "Website actions B"})
        other_website = (
            self.env["website"]
            .sudo()
            .with_company(other_company)
            .create({"name": "Website B", "company_id": other_company.id})
        )
        other_endpoint = (
            self.endpoint.sudo()
            .with_company(other_company)
            .create(
                {
                    "capture_enabled": True,
                    "capture_purpose": "web_attribution",
                    "privacy_policy_version": "test-v1",
                    "privacy_notice_version": "test-v1",
                    "privacy_legal_basis_code": "documented_test_basis",
                    "privacy_policy_justification": "Synthetic test policy.",
                    "identifier_retention_days": 30,
                    "name": "Website action endpoint B",
                    "company_id": other_company.id,
                    "allowed_origins": "https://b.example.test",
                    "allowed_hosts": "b.example.test",
                }
            )
        )
        other_binding = (
            self.env["marketing.website.ingress.binding"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "website_id": other_website.id,
                    "endpoint_id": other_endpoint.id,
                }
            )
        )
        other_action = (
            self.env["marketing.website.action"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "name": "Website B WhatsApp",
                    "binding_id": other_binding.id,
                    "kind": "whatsapp_handoff",
                    "route_ref": "whatsapp.b",
                    "source_path": "/",
                    "whatsapp_destination": "5519888888888",
                }
            )
        )
        other_grant_model = (
            self.env["marketing.website.redirect.grant"]
            .sudo()
            .with_context(allowed_company_ids=[other_company.id])
            .with_company(other_company)
        )
        other_token = other_grant_model._issue(
            other_action,
            "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
        )
        cross_company_action, fallback = self.env[
            "marketing.website.redirect.grant"
        ]._consume(other_token, self.website)
        self.assertFalse(cross_company_action)
        self.assertEqual(fallback, "/")
        self.assertEqual(
            other_grant_model.search([("action_id", "=", other_action.id)]).state,
            "issued",
        )
        resolved, _fallback = other_grant_model._consume(
            other_token,
            other_website,
        )
        self.assertEqual(resolved, other_action)
        admin = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Restricted Website action admin",
                    "login": "restricted-website-action-admin",
                    "company_id": self.website.company_id.id,
                    "company_ids": [(6, 0, [self.website.company_id.id])],
                    "groups_id": [
                        (
                            6,
                            0,
                            [
                                self.env.ref(
                                    "marketing_center_base.group_marketing_center_admin"
                                ).id
                            ],
                        )
                    ],
                }
            )
        )
        self.assertFalse(
            self.env["marketing.website.action"]
            .with_user(admin)
            .search_count([("id", "=", other_action.id)])
        )
