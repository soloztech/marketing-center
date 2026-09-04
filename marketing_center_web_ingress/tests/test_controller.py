import datetime
import json
from unittest.mock import patch

from psycopg2 import OperationalError, errorcodes

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..services.errors import WebIngressSerializationFailure


@tagged("-at_install", "post_install")
class TestMarketingWebIngressController(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "HTTP ingress",
                "company_id": cls.env.company.id,
                "allowed_origins": "https://www.soloz.example",
                "allowed_hosts": "www.soloz.example",
            }
        )
        cls.path = "/marketing/web-ingress/%s" % cls.endpoint.public_ref

    def _body(self, event_id="http-event-00000001", **overrides):
        occurred_at = (
            datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        )
        values = {
            "event_id": event_id,
            "event_type": "entry_point",
            "occurred_at": occurred_at,
            "landing_url": "https://www.soloz.example/solar?utm_source=google",
            "utm_source": "google",
            "gclid": "http-gclid-private-001",
        }
        values.update(overrides)
        return json.dumps(values, separators=(",", ":")).encode()

    def _headers(self, **overrides):
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://www.soloz.example",
            "X-Marketing-Ingress-Key": self.endpoint.public_key,
            "X-Marketing-Ingress-Revision": str(self.endpoint.config_revision),
        }
        headers.update(overrides)
        return headers

    def test_valid_and_duplicate_post_return_same_opaque_response(self):
        body = self._body()
        first = self.opener.post(
            self.base_url() + self.path, data=body, headers=self._headers()
        )
        second = self.opener.post(
            self.base_url() + self.path, data=body, headers=self._headers()
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json(), {"accepted": True})
        self.assertEqual(second.json(), {"accepted": True})
        self.assertNotIn("Set-Cookie", first.headers)
        self.assertNotIn("event", first.text)
        self.assertNotIn("touchpoint", first.text)
        self.env.invalidate_all()
        events = self.env["marketing.web.ingress.event"].sudo().search([])
        self.assertEqual(len(events), 1)
        self.assertEqual(events.ingress_provenance, "browser_capability")

    def test_serialization_signal_replays_the_same_bounded_request_body(self):
        service_class = type(self.env["marketing.web.ingress.service"])
        original_ingest = service_class._ingest_payload
        endpoint_id = self.endpoint.id
        body = self._body("http-retry-00000001")
        headers = self._headers()
        attempts = []

        def fail_once(service, *args, **kwargs):
            attempts.append(
                {
                    "payload": json.dumps(
                        args[1], sort_keys=True, separators=(",", ":")
                    ),
                    "body_size_bytes": kwargs["body_size_bytes"],
                }
            )
            if len(attempts) == 1:
                raise WebIngressSerializationFailure("synthetic HTTP retry")
            return original_ingest(service, *args, **kwargs)

        with patch.object(service_class, "_ingest_payload", new=fail_once):
            response = self.opener.post(
                self.base_url() + self.path,
                data=body,
                headers=headers,
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0]["body_size_bytes"], len(body))

        duplicate = self.opener.post(
            self.base_url() + self.path,
            data=body,
            headers=headers,
        )
        self.assertEqual(duplicate.status_code, 202, duplicate.text)

        # HttpCase shares its underlying cursor with HTTP requests.  A Docker
        # health probe can overlap this assertion after the deliberately slow
        # retry, so acquire a TestCursor through the registry instead of
        # racing through ``self.env``.
        with self.registry.cursor() as cr:
            cr.execute(
                """
                SELECT count(*)
                  FROM marketing_web_ingress_event
                 WHERE endpoint_id = %s
                """,
                [endpoint_id],
            )
            event_count = cr.fetchone()[0]
        self.assertEqual(event_count, 1)

    def test_serialization_signal_has_odoo_retry_sqlstate(self):
        error = WebIngressSerializationFailure("retry contract")
        self.assertIsInstance(error, OperationalError)
        self.assertEqual(error.pgcode, errorcodes.SERIALIZATION_FAILURE)

    def test_invalid_key_origin_and_unknown_field_are_opaque(self):
        cases = (
            ({"X-Marketing-Ingress-Key": "revoked"}, {}, 404),
            ({"Origin": "https://attacker.example"}, {}, 404),
            ({}, {"email": "must-not-enter@example.invalid"}, 400),
        )
        for index, (header_overrides, body_overrides, status) in enumerate(cases):
            with self.subTest(index=index):
                response = self.opener.post(
                    self.base_url() + self.path,
                    data=self._body("rejected-%s" % index, **body_overrides),
                    headers=self._headers(**header_overrides),
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"accepted": False})
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )

    def test_missing_or_stale_configuration_revision_is_rejected(self):
        missing = self.opener.post(
            self.base_url() + self.path,
            data=self._body("missing-revision-0001"),
            headers=self._headers(**{"X-Marketing-Ingress-Revision": ""}),
        )
        stale_revision = str(self.endpoint.config_revision)
        self.endpoint.write({"replay_window_seconds": 301})
        stale = self.opener.post(
            self.base_url() + self.path,
            data=self._body("stale-revision-00001"),
            headers=self._headers(**{"X-Marketing-Ingress-Revision": stale_revision}),
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(stale.status_code, 404)

    def test_preflight_echoes_only_an_explicit_allowed_origin(self):
        accepted = self.opener.options(
            self.base_url() + self.path,
            headers={"Origin": "https://www.soloz.example"},
        )
        rejected = self.opener.options(
            self.base_url() + self.path,
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(accepted.status_code, 204)
        self.assertEqual(
            accepted.headers["Access-Control-Allow-Origin"],
            "https://www.soloz.example",
        )
        self.assertIn(
            "X-Marketing-Ingress-Revision",
            accepted.headers["Access-Control-Allow-Headers"],
        )
        self.assertEqual(rejected.status_code, 404)
        self.assertNotIn("Access-Control-Allow-Origin", rejected.headers)

    def test_conflicting_event_reuse_is_first_wins_and_not_reported_as_accepted(self):
        first = self.opener.post(
            self.base_url() + self.path,
            data=self._body("http-conflict-000001"),
            headers=self._headers(),
        )
        conflict = self.opener.post(
            self.base_url() + self.path,
            data=self._body("http-conflict-000001", utm_campaign="changed"),
            headers=self._headers(),
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json(), {"accepted": False})
        self.env.invalidate_all()
        self.assertEqual(
            self.env["marketing.web.ingress.event"].sudo().search_count([]), 1
        )

    def test_inactive_endpoint_duplicate_json_and_invalid_utf8_are_rejected(self):
        duplicate_json = self.opener.post(
            self.base_url() + self.path,
            data=(
                b'{"event_id":"duplicate-json-0001",'
                b'"event_id":"duplicate-json-0002"}'
            ),
            headers=self._headers(),
        )
        invalid_utf8 = self.opener.post(
            self.base_url() + self.path,
            data=b'{"event_id":"invalid-utf8-0001","value":"\xff"}',
            headers=self._headers(),
        )
        self.assertEqual(duplicate_json.status_code, 400)
        self.assertEqual(invalid_utf8.status_code, 400)
        self.endpoint.write({"active": False})
        inactive = self.opener.post(
            self.base_url() + self.path,
            data=self._body("inactive-endpoint-001"),
            headers=self._headers(),
        )
        self.assertEqual(inactive.status_code, 404)

    def test_non_json_and_oversized_declared_body_are_rejected_before_parse(self):
        wrong_type = self.opener.post(
            self.base_url() + self.path,
            data=b"event=not-json",
            headers=self._headers(
                **{"Content-Type": "application/x-www-form-urlencoded"}
            ),
        )
        oversized = self.opener.post(
            self.base_url() + self.path,
            data=b"{" + (b"x" * (self.endpoint.max_body_bytes + 1)),
            headers=self._headers(),
        )
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(oversized.status_code, 413)

    def test_public_capability_cannot_assert_confirmed_action_events(self):
        response = self.opener.post(
            self.base_url() + self.path,
            data=self._body(
                "spoofed-action-000001",
                event_type="form_submission",
                action_ref="form.spoofed",
                route_ref="website.spoofed",
                model_ref="crm.lead",
            ),
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )

    def test_rate_limit_returns_retryable_status_without_creating_evidence(self):
        admission_class = type(self.env["marketing.web.ingress.admission"])
        with patch.object(admission_class, "_admit", return_value=False):
            response = self.opener.post(
                self.base_url() + self.path,
                data=self._body("rate-limited-event-001"),
                headers=self._headers(),
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )

    def test_rate_limiter_failure_is_opaque_and_does_not_create_evidence(self):
        admission_class = type(self.env["marketing.web.ingress.admission"])
        with patch.object(
            admission_class,
            "_admit",
            side_effect=RuntimeError("synthetic admission failure"),
        ):
            response = self.opener.post(
                self.base_url() + self.path,
                data=self._body("admission-failure-0001"),
                headers=self._headers(),
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"accepted": False})
        self.assertNotIn("synthetic", response.text)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"].sudo().search_count([])
        )
