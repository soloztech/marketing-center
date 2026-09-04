import datetime
import importlib.util
import json
import unittest
from pathlib import Path

try:
    from odoo.tests.common import TransactionCase as ContractTestCase
except ImportError:  # Keep this contract suite executable without Odoo.
    ContractTestCase = unittest.TestCase

try:
    from ..services.contracts import (
        MAX_PAYLOAD_FIELDS,
        WebIngressContractError,
        canonical_request_digest,
        normalize_allowed_hosts,
        normalize_allowed_origins,
        normalize_origin,
        parse_web_ingress_payload,
    )
except ImportError:  # Allow the stdlib-only contract gate without an Odoo runtime.
    _CONTRACT_PATH = Path(__file__).parents[1] / "services" / "contracts.py"
    _SPEC = importlib.util.spec_from_file_location(
        "marketing_web_ingress_contracts", _CONTRACT_PATH
    )
    _CONTRACTS = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(_CONTRACTS)
    WebIngressContractError = _CONTRACTS.WebIngressContractError
    MAX_PAYLOAD_FIELDS = _CONTRACTS.MAX_PAYLOAD_FIELDS
    canonical_request_digest = _CONTRACTS.canonical_request_digest
    normalize_allowed_hosts = _CONTRACTS.normalize_allowed_hosts
    normalize_allowed_origins = _CONTRACTS.normalize_allowed_origins
    normalize_origin = _CONTRACTS.normalize_origin
    parse_web_ingress_payload = _CONTRACTS.parse_web_ingress_payload


class TestWebIngressPureContract(ContractTestCase):
    def _payload(self, **overrides):
        payload = {
            "event_id": "event-20260901-001",
            "event_type": "entry_point",
            "occurred_at": "2026-09-01T12:00:00Z",
            "landing_url": "https://www.soloz.example/solar?utm_source=google#hero",
            "referrer_url": "https://google.example/search/private-token?q=solar",
            "utm_source": "google",
            "utm_medium": "cpc",
            "gclid": "opaque-GCLID_123.abc",
            "session_ref": "session-opaque-001",
            "consent_state": "unknown",
        }
        payload.update(overrides)
        return payload

    def _parse(self, payload=None):
        return parse_web_ingress_payload(
            payload or self._payload(),
            allowed_hosts=("www.soloz.example",),
            expected_origin="https://www.soloz.example",
            max_field_length=512,
        )

    def _form_payload(self, **overrides):
        payload = self._payload(
            event_type="form_submission",
            action_ref="form.contact.request",
            route_ref="website.contactus",
            model_ref="crm.lead",
        )
        payload.update(overrides)
        return payload

    def _organic_link_payload(self, **overrides):
        payload = self._payload(
            event_type="organic_link",
            action_ref="whatsapp.sales",
            route_ref="website.hero.cta",
        )
        payload.update(overrides)
        return payload

    def test_normalizes_first_party_payload_without_url_queries(self):
        parsed = self._parse()
        self.assertEqual(parsed.landing_url, "https://www.soloz.example/solar")
        self.assertEqual(parsed.referrer_url, "https://google.example/")
        self.assertEqual(parsed.utm, {"source": "google", "medium": "cpc"})
        self.assertEqual(parsed.consent_state, "unknown")
        self.assertEqual(parsed.occurred_at, datetime.datetime(2026, 9, 1, 12, 0))

    def test_same_site_referrer_may_keep_its_safe_path(self):
        parsed = self._parse(
            self._payload(referrer_url="https://www.soloz.example/products/solar?q=x")
        )
        self.assertEqual(
            parsed.referrer_url, "https://www.soloz.example/products/solar"
        )

    def test_safe_digest_contains_hashes_not_click_or_session_values(self):
        parsed = self._parse()
        digest = canonical_request_digest(parsed)
        encoded = json.dumps(parsed.safe_canonical_dict(), sort_keys=True)
        self.assertEqual(len(digest), 64)
        self.assertNotIn("opaque-GCLID_123.abc", encoded)
        self.assertNotIn("session-opaque-001", encoded)

    def test_rejects_unknown_and_pii_fields(self):
        for field_name in ("email", "phone", "name", "form_data"):
            with self.subTest(field_name=field_name), self.assertRaises(
                WebIngressContractError
            ):
                self._parse(self._payload(**{field_name: "must-not-enter"}))

    def test_rejects_nested_or_invalid_click_values(self):
        with self.assertRaises(WebIngressContractError):
            self._parse(self._payload(gclid={"raw": "nested"}))
        with self.assertRaises(WebIngressContractError):
            self._parse(self._payload(gclid="contains whitespace"))

    def test_requires_timezone_but_accepts_compact_utc_offset(self):
        with self.assertRaises(WebIngressContractError):
            self._parse(self._payload(occurred_at="2026-09-01T12:00:00"))
        parsed = self._parse(self._payload(occurred_at="2026-09-01T09:00:00-0300"))
        self.assertEqual(parsed.occurred_at, datetime.datetime(2026, 9, 1, 12, 0))

    def test_origins_are_exact_and_canonical(self):
        self.assertEqual(
            normalize_origin("HTTPS://WWW.SOLOZ.EXAMPLE:443"),
            "https://www.soloz.example",
        )
        self.assertEqual(
            normalize_allowed_origins(
                "https://www.soloz.example\nhttps://portal.soloz.example"
            ),
            ("https://portal.soloz.example", "https://www.soloz.example"),
        )
        with self.assertRaises(WebIngressContractError):
            normalize_allowed_origins("*")

    def test_landing_origin_must_match_the_request_origin(self):
        with self.assertRaises(WebIngressContractError):
            parse_web_ingress_payload(
                self._payload(landing_url="https://portal.soloz.example/solar"),
                allowed_hosts=("portal.soloz.example", "www.soloz.example"),
                expected_origin="https://www.soloz.example",
                max_field_length=512,
            )

    def test_default_ports_have_one_canonical_url_representation(self):
        explicit = self._parse(
            self._payload(landing_url="https://www.soloz.example:443/solar")
        )
        implicit = self._parse(
            self._payload(landing_url="https://www.soloz.example/solar")
        )
        self.assertEqual(explicit.landing_url, implicit.landing_url)
        self.assertEqual(
            canonical_request_digest(explicit),
            canonical_request_digest(implicit),
        )

    def test_hosts_reject_wildcards_urls_and_empty_values(self):
        self.assertEqual(
            normalize_allowed_hosts("WWW.SOLOZ.EXAMPLE\nportal.soloz.example"),
            ("portal.soloz.example", "www.soloz.example"),
        )
        for value in (
            "",
            "*.soloz.example",
            "https://www.soloz.example",
            "bad host.example",
            "bad_.example",
            "-bad.example",
        ):
            with self.subTest(value=value), self.assertRaises(WebIngressContractError):
                normalize_allowed_hosts(value)

    def test_urls_reject_percent_encoded_control_characters(self):
        with self.assertRaises(WebIngressContractError):
            self._parse(
                self._payload(landing_url="https://www.soloz.example/%0d%0aheader")
            )

    def test_event_identifier_is_hashed_before_contract_leaves_memory(self):
        parsed = self._parse()
        self.assertEqual(len(parsed.event_key_hash), 64)
        self.assertNotEqual(parsed.event_key_hash, "event-20260901-001")

    def test_entry_point_forbids_action_reference_fields(self):
        for field_name in ("action_ref", "route_ref", "model_ref"):
            with self.subTest(field_name=field_name), self.assertRaises(
                WebIngressContractError
            ):
                self._parse(self._payload(**{field_name: ""}))

    def test_form_submission_requires_all_technical_references(self):
        parsed = self._parse(self._form_payload())
        self.assertEqual(parsed.action_ref, "form.contact.request")
        self.assertEqual(parsed.route_ref, "website.contactus")
        self.assertEqual(parsed.model_ref, "crm.lead")
        for field_name in ("action_ref", "route_ref", "model_ref"):
            payload = self._form_payload()
            payload.pop(field_name)
            with self.subTest(field_name=field_name), self.assertRaises(
                WebIngressContractError
            ):
                self._parse(payload)

    def test_organic_link_requires_action_and_route_but_forbids_model(self):
        parsed = self._parse(self._organic_link_payload())
        self.assertEqual(parsed.action_ref, "whatsapp.sales")
        self.assertEqual(parsed.route_ref, "website.hero.cta")
        self.assertFalse(parsed.model_ref)
        for field_name in ("action_ref", "route_ref"):
            payload = self._organic_link_payload()
            payload.pop(field_name)
            with self.subTest(field_name=field_name), self.assertRaises(
                WebIngressContractError
            ):
                self._parse(payload)
        with self.assertRaises(WebIngressContractError):
            self._parse(self._organic_link_payload(model_ref=""))

    def test_action_references_are_opaque_and_part_of_v2_digest(self):
        parsed = self._parse(self._form_payload())
        canonical = parsed.safe_canonical_dict()
        self.assertEqual(canonical["action_ref"], "form.contact.request")
        self.assertEqual(canonical["route_ref"], "website.contactus")
        self.assertEqual(canonical["model_ref"], "crm.lead")
        changed = self._parse(self._form_payload(action_ref="form.quote.request"))
        self.assertNotEqual(
            canonical_request_digest(parsed), canonical_request_digest(changed)
        )
        for field_name in ("action_ref", "route_ref", "model_ref"):
            with self.subTest(field_name=field_name), self.assertRaises(
                WebIngressContractError
            ):
                self._parse(self._form_payload(**{field_name: "contains whitespace"}))

    def test_entry_point_canonical_remains_v1_and_field_cap_remains_twenty(self):
        parsed_entry = self._parse()
        canonical = parsed_entry.safe_canonical_dict()
        for field_name in ("action_ref", "route_ref", "model_ref"):
            self.assertNotIn(field_name, canonical)
        self.assertEqual(
            canonical_request_digest(parsed_entry, "https://www.soloz.example"),
            "c20738c346045763a90c71a73150025f3cbf4b397582e60262667994ea47b38d",
        )
        payload = self._form_payload(
            utm_campaign="solar",
            utm_content="hero",
            utm_term="industrial",
            gbraid="opaque-gbraid",
            wbraid="opaque-wbraid",
            fbclid="opaque-fbclid",
            visitor_ref="visitor-opaque-001",
        )
        self.assertEqual(len(payload), MAX_PAYLOAD_FIELDS)
        self._parse(payload)


if __name__ == "__main__":
    unittest.main()
