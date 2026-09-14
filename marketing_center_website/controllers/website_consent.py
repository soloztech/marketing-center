import json
from urllib.parse import unquote

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from ..models.consent import CONSENT_COOKIE, CONSENT_CONTEXT_TOKEN
from .website_action import _bounded_strict_json, _human_post_headers, _same_origin
from .website_ingress import _config_response, _endpoint_origin_allowed


def _binding():
    if not request.env.user._is_public() or request.httprequest.scheme != "https":
        return request.env["marketing.website.ingress.binding"]
    binding = (
        request.env["marketing.website.ingress.binding"]
        .sudo()
        .search(
            [
                ("website_id", "=", request.website.id),
                ("active", "=", True),
                ("endpoint_id.active", "=", True),
                ("company_id", "=", request.website.company_id.id),
            ],
            limit=1,
        )
    )
    if binding and _endpoint_origin_allowed(binding.endpoint_id, request.httprequest.host_url):
        return binding
    return request.env["marketing.website.ingress.binding"]


def _configuration(binding):
    endpoint = binding.endpoint_id
    if (
        not binding
        or not endpoint.active
        or not _endpoint_origin_allowed(endpoint, request.httprequest.host_url)
        or not endpoint.capture_enabled
        or not endpoint._privacy_policy_configured()
    ):
        return {
            "available": False,
            "granted": False,
            "informational_notice": False,
            "capture_allowed": False,
        }
    decision = (request.env["marketing.website.consent"] if endpoint._informational_notice()
                else request.env["marketing.website.consent"]._current(endpoint))
    return {
        "available": bool(
            binding.website_id.cookies_bar
            and endpoint._requires_individual_consent()
        ),
        "granted": bool(decision),
        "informational_notice": endpoint._informational_notice(),
        "capture_allowed": bool(endpoint._capture_policy_allows()),
        "config_revision": endpoint.config_revision,
        "policy_version": endpoint.privacy_policy_version,
        "notice_version": endpoint.privacy_notice_version,
    }


class MarketingWebsiteConsentController(http.Controller):
    @http.route(
        "/marketing/website-consent/config",
        type="http",
        auth="public",
        methods=["GET"],
        website=True,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def configuration(self, **_kwargs):
        return _config_response(_configuration(_binding()))

    @http.route(
        "/marketing/website-consent/decision",
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
        csrf=False,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def decide(self, **_kwargs):
        # Same-origin JSON plus a non-simple custom header is a CSRF boundary:
        # cross-origin browsers need a preflight, and this route exposes no CORS.
        if (
            not _same_origin()
            or not _human_post_headers()
            or request.httprequest.headers.get("X-Marketing-Consent") != "1"
        ):
            return _config_response({"accepted": False})
        binding = _binding()
        if not binding:
            return _config_response({"accepted": False})
        try:
            payload = _bounded_strict_json()
            if (
                set(payload)
                != {"granted", "config_revision", "policy_version", "notice_version"}
                or type(payload["granted"]) is not bool
                or type(payload["config_revision"]) is not int
                or not isinstance(payload["policy_version"], str)
                or not isinstance(payload["notice_version"], str)
                or len(payload["policy_version"]) > 128
                or len(payload["notice_version"]) > 128
            ):
                raise ValidationError("Invalid decision envelope")
            endpoint = binding.endpoint_id
            model = request.env["marketing.website.consent"].sudo()
            previous_cookie = request.httprequest.cookies.get(CONSENT_COOKIE, "")
            if payload["granted"] is False:
                # Withdrawal remains possible after a configuration/policy change.
                # No fresh tracking identity is needed to remember a refusal.
                previous = model._from_cookie(endpoint, previous_cookie)
                if previous:
                    request.env.cr.execute(
                        "SELECT id FROM marketing_web_ingress_endpoint "
                        "WHERE id = %s FOR SHARE",
                        [endpoint.id],
                    )
                    request.env.cr.execute(
                        "SELECT id FROM marketing_website_consent "
                        "WHERE id = %s FOR UPDATE",
                        [previous.id],
                    )
                    previous.invalidate_recordset(["revoked_at"])
                    if not previous.revoked_at:
                        from odoo import fields

                        previous.with_context(
                            website_consent_internal=CONSENT_CONTEXT_TOKEN
                        ).write({"revoked_at": fields.Datetime.now()})
                response = _config_response(
                    {
                        "accepted": True,
                        "granted": False,
                        "notice_version": endpoint.privacy_notice_version,
                        "informational_notice": endpoint._informational_notice(),
                        "capture_allowed": bool(
                            endpoint._informational_notice()
                            and endpoint.capture_enabled
                            and endpoint._privacy_policy_configured()
                        ),
                    }
                )
                response.delete_cookie(
                    CONSENT_COOKIE,
                    path="/",
                    secure=True,
                    httponly=True,
                    samesite="Strict",
                )
                return response
            # The native preference alone never creates a grant. It must agree
            # with this explicit versioned decision submitted by its UI handler.
            native_cookie = json.loads(
                unquote(request.httprequest.cookies.get("website_cookies_bar", "{}"))
            )
            if (
                not isinstance(native_cookie, dict)
                or native_cookie.get("optional") is not True
            ):
                raise AccessError("Native optional preference is absent")
            for key in ("config_revision", "policy_version", "notice_version"):
                expected = (
                    endpoint[key]
                    if key == "config_revision"
                    else endpoint["privacy_" + key]
                )
                if payload[key] != expected:
                    raise AccessError("The displayed privacy notice has changed")
            if (
                not request.env["marketing.web.ingress.admission"]
                .sudo()
                ._admit(endpoint, "website_form")
            ):
                response = _config_response({"accepted": False})
                response.status_code = 429
                return response
            decision = model._decide(binding, True, previous_cookie)
            response = _config_response(
                {
                    "accepted": True,
                    "granted": True,
                    "notice_version": decision.notice_version,
                    "informational_notice": endpoint._informational_notice(),
                    "capture_allowed": True,
                }
            )
            response.set_cookie(
                CONSENT_COOKIE,
                decision._cookie(),
                max_age=endpoint.consent_ttl_days * 86400,
                path="/",
                secure=True,
                httponly=True,
                samesite="Strict",
            )
            return response
        except (ValueError, TypeError, AccessError, ValidationError):
            response = _config_response({"accepted": False, "granted": False})
            response.status_code = 400
            return response
