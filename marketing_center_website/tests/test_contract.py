from pathlib import Path

from odoo.modules.module import get_manifest, get_resource_path
from odoo.tests.common import SavepointCase


class TestMarketingWebsiteContract(SavepointCase):
    def _resource_text(self, *parts):
        return Path(get_resource_path("marketing_center_website", *parts)).read_text(
            encoding="utf-8"
        )

    def test_manifest_keeps_provider_neutral_boundary(self):
        manifest = get_manifest("marketing_center_website")
        self.assertEqual(
            manifest["depends"], ["marketing_center_web_ingress", "website"]
        )
        self.assertNotIn("crm", manifest["depends"])
        self.assertNotIn("link_tracker", manifest["depends"])
        self.assertNotIn("utm", manifest["depends"])

    def test_native_click_owner_is_optional_and_not_duplicated_by_adapter(self):
        manifest = get_manifest("marketing_center_website")
        readme = self._resource_text("README.rst")
        action_capture = self._resource_text(
            "static", "src", "js", "action_capture.esm.js"
        )
        action_kinds = dict(
            self.env["marketing.website.action"]._fields["kind"].selection
        )

        self.assertNotIn("link_tracker", manifest["depends"])
        self.assertIn("link.tracker.click`` owns that click fact", readme)
        self.assertIn("must not create a\nsecond canonical click", readme)
        self.assertNotIn("physical click is persisted", readme)
        self.assertEqual(set(action_kinds), {"form_submission", "whatsapp_handoff"})
        self.assertIn('"data-marketing-whatsapp-action"', action_capture)
        self.assertIn("nativeTrackedLinkActivation", action_capture)
        self.assertIn("NATIVE_TRACKED_LINK_PATH_PATTERN", action_capture)
        self.assertNotIn('querySelectorAll("a', action_capture)

    def test_frontend_never_reads_forms_cookies_or_persistent_storage(self):
        sources = "\n".join(
            [
                self._resource_text("static", "src", "js", "landing_capture.esm.js"),
                self._resource_text("static", "src", "js", "landing_bootstrap.esm.js"),
                self._resource_text("static", "src", "js", "action_capture.esm.js"),
                self._resource_text("static", "src", "js", "action_bootstrap.esm.js"),
            ]
        )
        for forbidden in (
            "FormData",
            "querySelector",
            "getElementById",
            "document.cookie",
            "localStorage",
            ".elements",
            ".value",
            "textContent",
            "innerHTML",
            "serializeArray",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sources)
        self.assertIn("document.referrer", sources)
        self.assertIn("window.sessionStorage", sources)

    def test_native_form_bridge_strips_only_technical_query_before_super(self):
        controller = self._resource_text("controllers", "website_action.py")
        strip_position = controller.index("_form_claim_from_query(kwargs)")
        super_position = controller.index("super().website_form(model_name, **kwargs)")
        self.assertLess(strip_position, super_position)
        self.assertIn("request.params.pop(query_name, None)", controller)
        self.assertIn("kwargs.pop(query_name, None)", controller)
        for forbidden in (
            "request.httprequest.form",
            "request.httprequest.files",
            "extract_data(",
            "insert_record(",
            "kwargs.get(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, controller)

    def test_whatsapp_redirect_has_no_browser_selected_destination(self):
        controller = self._resource_text("controllers", "website_action.py")
        grant = self._resource_text("models", "redirect_grant.py")
        self.assertIn(
            'target = "https://wa.me/%s" % action.whatsapp_destination', controller
        )
        self.assertNotIn('get("next")', controller)
        self.assertNotIn("redirect_url", grant)
        self.assertNotIn("raw_token = fields", grant)
        self.assertIn("token_hash = fields.Char", grant)

    def test_public_route_is_read_only_and_does_not_call_ingress_service(self):
        controller = self._resource_text("controllers", "website_ingress.py")
        route_body = controller.split("def public_config", 1)[1]
        for forbidden in (".create(", ".write(", "._ingest_payload("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_body)
        self.assertIn('methods=["GET"]', controller)
        self.assertIn('"Cache-Control": "no-store, max-age=0"', controller)
        self.assertIn("request.env.user._is_public()", controller)

    def test_configuration_mutations_share_database_row_locks(self):
        binding = self._resource_text("models", "website_ingress_binding.py")
        action = self._resource_text("models", "website_action.py")
        endpoint = self._resource_text("models", "endpoint.py")
        website = self._resource_text("models", "website.py")
        self.assertIn("UPDATE website SET write_date = write_date", binding)
        self.assertIn(
            "UPDATE marketing_web_ingress_endpoint SET write_date = write_date",
            binding,
        )
        self.assertIn("_fence_effective_configuration()", action)
        self.assertIn("ORDER BY id FOR UPDATE", endpoint)
        self.assertIn("FROM website WHERE id IN %s ORDER BY id FOR UPDATE", website)

    def test_action_replay_and_redirect_consumption_use_database_locks(self):
        grant = self._resource_text("models", "redirect_grant.py")
        controller = self._resource_text("controllers", "website_action.py")
        consume = grant.split("def _consume", 1)[1]
        self.assertIn(
            "UPDATE marketing_website_action SET write_date = write_date", grant
        )
        self.assertIn("FROM marketing_website_action WHERE id = %s FOR UPDATE", grant)
        self.assertIn("marketing_website_redirect_grant_issued_uniq", grant)
        self.assertIn(
            "WHERE token_hash = %s AND action_id = %s FOR UPDATE",
            grant,
        )
        self.assertLess(
            consume.index("FROM marketing_web_ingress_endpoint"),
            consume.index(
                "SELECT id FROM marketing_website_action WHERE id = %s FOR UPDATE"
            ),
        )
        self.assertLess(
            consume.index(
                "SELECT id FROM marketing_website_action WHERE id = %s FOR UPDATE"
            ),
            consume.index("WHERE token_hash = %s AND action_id = %s FOR UPDATE"),
        )
        self.assertIn("except PsycopgError:", controller)
        self.assertIn("get_data(cache=True)", controller)

    def test_endpoint_domain_company_field_is_available_to_every_admin(self):
        view = self._resource_text("views", "website_ingress_binding_views.xml")
        self.assertIn('<field name="company_id" />', view)
        self.assertNotIn(
            '<field name="company_id" groups="base.group_multi_company"', view
        )
