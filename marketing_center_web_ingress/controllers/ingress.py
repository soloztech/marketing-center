import json
import logging

from psycopg2 import Error as PsycopgError
from werkzeug.wrappers import Response

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from ..services.contracts import (
    PUBLIC_INGRESS_EVENT_TYPES,
    WebIngressContractError,
    normalize_allowed_origins,
    normalize_origin,
)

_logger = logging.getLogger(__name__)
_PUBLIC_KEY_HEADER = "X-Marketing-Ingress-Key"
_CONFIG_REVISION_HEADER = "X-Marketing-Ingress-Revision"


def _response(accepted, status, origin=""):
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if origin:
        headers.update(
            {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Content-Type, X-Marketing-Ingress-Key, "
                    "X-Marketing-Ingress-Revision"
                ),
                "Access-Control-Max-Age": "300",
                "Vary": "Origin",
            }
        )
    body = (
        b""
        if status == 204
        else json.dumps({"accepted": bool(accepted)}, separators=(",", ":")).encode(
            "ascii"
        )
    )
    return Response(
        body,
        status=status,
        content_type="application/json; charset=utf-8",
        headers=headers,
    )


def _endpoint(public_ref):
    if not isinstance(public_ref, str) or len(public_ref) != 36:
        return request.env["marketing.web.ingress.endpoint"]
    return (
        request.env["marketing.web.ingress.endpoint"]
        .sudo()
        .with_context(active_test=False)
        .search([("public_ref", "=", public_ref)], limit=1)
    )


def _allowed_origin(endpoint):
    candidate = request.httprequest.headers.get("Origin", "")
    try:
        origin = normalize_origin(candidate)
        if origin not in normalize_allowed_origins(endpoint.allowed_origins):
            return ""
        return origin
    except WebIngressContractError:
        return ""


def _bounded_body(endpoint):
    if request.httprequest.mimetype != "application/json":
        return None, 415
    declared = request.httprequest.content_length
    if declared is None:
        return None, 411
    if declared <= 0:
        return None, 400
    if declared > endpoint.max_body_bytes:
        return None, 413
    # The body is already strictly bounded. Keep these bytes on the Werkzeug
    # request so Odoo can replay the complete route after a serialization
    # rollback; an uncached input stream would be empty on the next attempt.
    body = request.httprequest.get_data(cache=True)
    if len(body) != declared or len(body) > endpoint.max_body_bytes:
        return None, 400
    return body, 0


def _strict_json(body):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    decoded = json.loads(body.decode("utf-8"), object_pairs_hook=object_pairs)
    if not isinstance(decoded, dict):
        raise ValueError("object required")
    return decoded


class MarketingWebIngressController(http.Controller):
    @http.route(
        "/marketing/web-ingress/<string:public_ref>",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        save_session=False,
    )
    def ingest(self, public_ref, **_kwargs):
        endpoint = _endpoint(public_ref)
        if not endpoint or not endpoint.active:
            return _response(False, 404)
        origin = _allowed_origin(endpoint)
        if not origin:
            return _response(False, 404)
        if request.httprequest.method == "OPTIONS":
            return _response(False, 204, origin)
        candidate_key = request.httprequest.headers.get(_PUBLIC_KEY_HEADER, "")
        candidate_revision = request.httprequest.headers.get(
            _CONFIG_REVISION_HEADER, ""
        )
        if not endpoint._locked_for_public_key(candidate_key, candidate_revision):
            return _response(False, 404, origin)
        body, rejected_status = _bounded_body(endpoint)
        if rejected_status:
            return _response(False, rejected_status, origin)
        try:
            payload = _strict_json(body)
        except (ValueError, RecursionError):
            return _response(False, 400, origin)
        if payload.get("event_type") not in PUBLIC_INGRESS_EVENT_TYPES:
            return _response(False, 400, origin)
        try:
            admitted = (
                request.env["marketing.web.ingress.admission"]
                .sudo()
                .with_context(allowed_company_ids=[endpoint.company_id.id])
                .with_company(endpoint.company_id)
                ._admit(endpoint, "public_ingress")
            )
            if not admitted:
                response = _response(False, 429, origin)
                response.headers["Retry-After"] = "60"
                return response
            result = (
                request.env["marketing.web.ingress.service"]
                .sudo()
                .with_context(allowed_company_ids=[endpoint.company_id.id])
                .with_company(endpoint.company_id)
                ._ingest_payload(
                    endpoint,
                    payload,
                    origin=origin,
                    body_size_bytes=len(body),
                    ingress_provenance="browser_capability",
                )
            )
        except PsycopgError:
            # A PostgreSQL error leaves the transaction unsafe to reuse.  Let
            # Odoo's request retry/rollback boundary handle retryable SQLSTATEs
            # (including our synthetic serialization signal) from a fresh
            # REPEATABLE READ snapshot.
            raise
        except (ValidationError, AccessError):
            return _response(False, 400, origin)
        except Exception as error:  # Public boundary: body and values stay opaque.
            _logger.error(
                "Web ingress failed endpoint=%s error_class=%s",
                endpoint.public_ref,
                type(error).__name__,
            )
            return _response(False, 503, origin)
        if result.disposition == "conflict":
            return _response(False, 409, origin)
        if result.disposition not in ("accepted", "duplicate"):
            return _response(False, 503, origin)
        return _response(True, 202, origin)
