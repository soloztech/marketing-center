import datetime
import json
import logging

from psycopg2 import Error as PsycopgError
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from odoo import _, http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.marketing_center_web_ingress.services.contracts import (
    WebIngressContractError,
    normalize_origin,
)
from odoo.addons.website.controllers.form import WebsiteForm

from ..services.contracts import FORM_QUERY_FIELDS, MAX_ACTION_BODY_BYTES

_logger = logging.getLogger(__name__)

_ACTION_OCCURRED_AT_ENV_KEY = "marketing_center_website.action_occurred_at"


def _security_headers():
    return {
        "Cache-Control": "no-store, max-age=0",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _json_response(accepted, status, redirect_path=""):
    payload = {"accepted": bool(accepted)}
    if redirect_path:
        payload["redirect_path"] = redirect_path
    return Response(
        json.dumps(payload, separators=(",", ":")),
        status=status,
        content_type="application/json; charset=utf-8",
        headers=_security_headers(),
    )


def _same_origin():
    candidate = request.httprequest.headers.get("Origin", "")
    host_url = request.httprequest.host_url
    try:
        origin = normalize_origin(candidate)
        current = normalize_origin(host_url)
    except WebIngressContractError:
        return ""
    return origin if origin == current else ""


def _human_post_headers():
    for header_name in ("Purpose", "Sec-Purpose"):
        if "prefetch" in request.httprequest.headers.get(header_name, "").lower():
            return False
    fetch_site = request.httprequest.headers.get("Sec-Fetch-Site", "").lower()
    return fetch_site in ("", "same-origin")


def _request_occurred_at():
    """Return one stable server timestamp for every retry of this HTTP request."""
    environ = request.httprequest.environ
    occurred_at = environ.get(_ACTION_OCCURRED_AT_ENV_KEY)
    if occurred_at is None:
        occurred_at = datetime.datetime.utcnow().replace(microsecond=0)
        environ[_ACTION_OCCURRED_AT_ENV_KEY] = occurred_at
    return occurred_at


def _bounded_strict_json():
    if request.httprequest.mimetype != "application/json":
        raise ValidationError(_("Invalid content type."))
    declared = request.httprequest.content_length
    if declared is None or declared <= 0 or declared > MAX_ACTION_BODY_BYTES:
        raise ValidationError(_("Invalid content length."))
    body = request.httprequest.get_data(cache=True)
    if len(body) != declared or len(body) > MAX_ACTION_BODY_BYTES:
        raise ValidationError(_("Invalid body length."))

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=object_pairs)
    except (ValueError, RecursionError) as error:
        raise ValidationError(_("Invalid JSON body.")) from error
    if not isinstance(payload, dict):
        raise ValidationError(_("An object body is required."))
    return payload


def _form_claim_from_query(kwargs):
    claim = {}
    args = request.httprequest.args
    names = {
        "mc_action": "action_ref",
        "mc_event": "event_id",
        "mc_session": "session_ref",
    }
    valid = True
    for query_name in FORM_QUERY_FIELDS:
        values = args.getlist(query_name)
        if len(values) != 1:
            valid = False
        elif values:
            claim[names[query_name]] = values[0]
        request.params.pop(query_name, None)
        kwargs.pop(query_name, None)
    return claim if valid and len(claim) == len(FORM_QUERY_FIELDS) else {}


def _response_text(result):
    """Expose only the serialized native payload across controller layers."""
    if isinstance(result, str):
        return result
    if isinstance(result, Response):
        try:
            return result.get_data(as_text=True)
        except (TypeError, UnicodeError):
            return ""
    return ""


def _append_form_receipt(result, receipt):
    if not receipt:
        return result
    native_result = _response_text(result)
    if not native_result:
        return result
    try:
        payload = json.loads(native_result)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict) or not payload.get("id"):
        return result
    payload["marketing_center_receipt"] = receipt
    serialized = json.dumps(payload, separators=(",", ":"))
    if isinstance(result, Response):
        result.set_data(serialized)
        return result
    return serialized


class MarketingWebsiteFormController(WebsiteForm):
    @http.route()
    def website_form(self, model_name, **kwargs):
        claim = _form_claim_from_query(kwargs)
        receipt = ""
        origin = _same_origin()
        if claim and origin and request.env.user._is_public():
            try:
                receipt = (
                    request.env["marketing.website.action.service"]
                    .sudo()
                    .with_context(allowed_company_ids=[request.website.company_id.id])
                    .with_company(request.website.company_id)
                    ._prepare_form_receipt(
                        request.website,
                        model_name,
                        claim,
                        origin,
                    )
                )
            except PsycopgError:
                raise
            except (ValidationError, AccessError) as error:
                # The native form remains authoritative and must keep working,
                # but a rejected receipt must be diagnosable without logging the
                # claim, origin, signature or any submitted form value.
                _logger.warning(
                    "Website form receipt rejected error_class=%s",
                    type(error).__name__,
                )
                receipt = ""
            except Exception as error:  # Attribution must not break the native form.
                _logger.warning(
                    "Website form receipt skipped error_class=%s",
                    type(error).__name__,
                )
        result = super().website_form(model_name, **kwargs)
        return _append_form_receipt(result, receipt)


class MarketingWebsiteActionController(http.Controller):
    @http.route(
        "/marketing/website-action/form/exchange",
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
        csrf=False,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def exchange_form(self, **_kwargs):
        return self._handle_action("form")

    @http.route(
        "/marketing/website-action/whatsapp/claim",
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
        csrf=False,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def claim_whatsapp(self, **_kwargs):
        return self._handle_action("whatsapp")

    def _handle_action(self, action_kind):
        if not request.env.user._is_public() or not _human_post_headers():
            return _json_response(False, 404)
        origin = _same_origin()
        if not origin:
            return _json_response(False, 404)
        try:
            payload = _bounded_strict_json()
            service = (
                request.env["marketing.website.action.service"]
                .sudo()
                .with_context(allowed_company_ids=[request.website.company_id.id])
                .with_company(request.website.company_id)
            )
            request_kind = (
                "website_form" if action_kind == "form" else "website_whatsapp"
            )
            if not service._admit_public_website(request.website, request_kind):
                response = _json_response(False, 429)
                response.headers["Retry-After"] = "60"
                return response
            if action_kind == "form":
                result = service._exchange_form_receipt(
                    request.website,
                    payload,
                    origin,
                )
                token = ""
            else:
                result, token = service._claim_whatsapp_handoff(
                    request.website,
                    payload,
                    origin,
                    now=_request_occurred_at(),
                )
        except PsycopgError:
            # A database error leaves the transaction unsafe to reuse. Odoo's
            # request retry loop handles retryable SQLSTATEs (including the
            # ingress serialization signal) from a fresh snapshot.
            raise
        except (ValidationError, AccessError):
            return _json_response(False, 400)
        except Exception as error:  # Public boundary stays body- and token-opaque.
            _logger.error(
                "Website action failed kind=%s error_class=%s",
                action_kind,
                type(error).__name__,
            )
            return _json_response(False, 503)
        if result.disposition == "conflict":
            return _json_response(False, 409)
        if result.disposition not in ("accepted", "duplicate"):
            return _json_response(False, 503)
        if action_kind == "whatsapp":
            if not token:
                return _json_response(False, 503)
            return _json_response(
                True,
                202,
                "/marketing/website-action/go/%s" % token,
            )
        return _json_response(True, 202)

    @http.route(
        "/marketing/website-action/go/<string:raw_token>",
        type="http",
        auth="public",
        methods=["GET"],
        website=True,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def whatsapp_redirect(self, raw_token, **_kwargs):
        action, fallback = (
            request.env["marketing.website.redirect.grant"]
            .sudo()
            .with_context(allowed_company_ids=[request.website.company_id.id])
            .with_company(request.website.company_id)
            ._consume(raw_token, request.website)
        )
        target = fallback
        if action:
            target = "https://wa.me/%s" % action.whatsapp_destination
        response = redirect(target, code=303)
        # Werkzeug otherwise expands a relative fallback to an absolute URL
        # while finalizing the response. Keep the second, already-consumed
        # redirect strictly same-origin and path-only.
        response.autocorrect_location_header = False
        response.headers.update(_security_headers())
        return response
