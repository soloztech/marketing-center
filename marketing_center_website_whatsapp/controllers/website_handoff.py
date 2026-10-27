"""Optional capture never owns the underlying public WhatsApp link."""

import json
import logging
from urllib.parse import urlencode, urlsplit

from psycopg2 import Error as PsycopgError
from werkzeug.wrappers import Response

from odoo import _, http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request
from odoo.tools import hmac

from odoo.addons.marketing_center_website.controllers.website_action import (
    _bounded_strict_json,
    _human_post_headers,
    _request_occurred_at,
    _same_origin,
    _security_headers,
)
from odoo.addons.marketing_center_website.services.acquisition import safe_page
from odoo.addons.marketing_center_website.services.contracts import (
    WebsiteActionContractError,
    opaque_uuid,
)
from odoo.addons.marketing_center_website.services.ingress.contracts import (
    normalize_allowed_hosts,
)

from ..services.references import reference_message

_logger = logging.getLogger(__name__)


def _response(status, **values):
    return Response(
        json.dumps({"accepted": status == 202, **values}, separators=(",", ":")),
        status=status,
        content_type="application/json; charset=utf-8",
        headers=_security_headers(),
    )


class WebsiteWhatsAppHandoff(http.Controller):
    @http.route(
        "/marketing/website-whatsapp/claim",
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
        csrf=False,
        sitemap=False,
        multilang=False,
    )
    def native_handoff(self, **_kwargs):
        origin = _same_origin()
        if (
            not origin
            or request.httprequest.scheme != "https"
            or not request.env.user._is_public()
            or not _human_post_headers()
        ):
            return _response(404)
        website = request.website
        company = website.company_id
        env = request.env(
            context=dict(request.env.context, allowed_company_ids=[company.id])
        )
        service = env["marketing.website.action.service"].sudo().with_company(company)
        try:
            if not service._admit_public_website(website, "website_whatsapp"):
                response = _response(429)
                response.headers["Retry-After"] = "60"
                return response
            payload = _bounded_strict_json()
            if set(payload) != {"action_ref", "event_id"}:
                raise ValidationError(_("Envelope WhatsApp inválido."))
            action_ref = opaque_uuid(payload["action_ref"], "action_ref")
            event_id = opaque_uuid(payload["event_id"], "event_id")
            action = website.with_env(env)._whatsapp_handoff_claim_action(
                action_ref,
                request.httprequest.referrer or "",
                origin,
            )
            endpoint = service._lock_effective_configuration(action)
            # Configuration writes share the endpoint fence. Re-resolve under
            # that lock before accepting an archived reference from an old tab.
            if (
                website.with_env(env)._whatsapp_handoff_claim_action(
                    action_ref,
                    request.httprequest.referrer or "",
                    origin,
                )
                != action
            ):
                raise AccessError(_("A configuração WhatsApp desta página mudou."))
            action.invalidate_recordset(
                ["handoff_enabled", "handoff_account_id", "handoff_reference_prefix"]
            )
            if action.handoff_account_id:
                env.cr.execute(
                    "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
                    [action.handoff_account_id.id],
                )
                action.handoff_account_id.invalidate_recordset(
                    [
                        "active",
                        "company_id",
                        "platform",
                        "own_external_identity",
                    ]
                )
            if (
                action.binding_id.capture_mode != "native"
                or not action.handoff_enabled
                or not endpoint.active
                or not action._handoff_account_matches()
            ):
                raise AccessError(_("Ação WhatsApp indisponível."))
            service._validate_runtime_origin(action, origin)
            if urlsplit(origin).hostname not in normalize_allowed_hosts(
                endpoint.allowed_hosts
            ):
                raise AccessError(_("Origem WhatsApp indisponível."))
            with env.cr.savepoint():
                # Hash only the existing native session. Retry admission checks
                # current policy above, but never selects acquisition again.
                session_key = hmac(
                    service.env,
                    "marketing.website.whatsapp.session.v1",
                    "%s:%s" % (website.id, request.session.sid),
                )
                handoffs = env["marketing.website.whatsapp.handoff"].sudo()
                page_url = safe_page(request.httprequest.referrer or "", origin)
                handoff = handoffs._existing_click(
                    action, event_id, session_key, page_url
                )
                if not handoff:
                    visitor = env["website.visitor"]._get_visitor_from_request(
                        force_create=True
                    )
                    snapshot = env["marketing.website.whatsapp.capture"]._snapshot(
                        action,
                        origin,
                        request.httprequest.referrer or "",
                        visitor=visitor,
                        cookies=request.httprequest.cookies,
                        now=_request_occurred_at(),
                    )
                    handoff = handoffs._record_click(
                        action,
                        visitor,
                        event_id,
                        session_key,
                        snapshot["page_url"],
                        snapshot["landing_url"],
                        snapshot["acquisition"],
                        track=snapshot["track"],
                        visit_at=snapshot["visit_at"],
                    )
                message = reference_message(
                    action.whatsapp_message or "", handoff.reference
                )
                target = "https://wa.me/%s?%s" % (
                    action.whatsapp_destination,
                    urlencode({"text": message}),
                )
                return _response(
                    202,
                    url=target,
                    reference=handoff.reference,
                    event_id=handoff.event_id,
                )
        except PsycopgError:
            raise
        except (AccessError, ValidationError, WebsiteActionContractError, ValueError):
            return _response(400)
        except Exception as error:
            _logger.warning(
                "Native WhatsApp capture unavailable error_class=%s",
                type(error).__name__,
            )
            return _response(503)
