import hashlib
import json
import logging
import secrets

from psycopg2 import IntegrityError
from werkzeug.wrappers import Response

from odoo import http
from odoo.http import request

from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.meta_api_base.services.signature import (
    MAX_WEBHOOK_BODY_BYTES,
    verify_signature,
)

from ..services.contracts import META_WEBHOOK_ROUTING_KEY_RE
from ..services.sanitizer import MetaWebhookSanitizationError, sanitized_webhook
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN

_logger = logging.getLogger(__name__)


def _json_response(payload, status=200):
    return Response(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _plain_response(value, status=200):
    return Response(
        value,
        status=status,
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _constant_time_text_equal(candidate, expected):
    if not isinstance(candidate, str) or not isinstance(expected, str):
        return False
    try:
        candidate_bytes = candidate.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return secrets.compare_digest(candidate_bytes, expected_bytes)


def _endpoint(routing_key):
    model = request.env["meta.webhook.endpoint"]
    if not isinstance(routing_key, str) or not META_WEBHOOK_ROUTING_KEY_RE.fullmatch(
        routing_key
    ):
        return model
    endpoint = (
        model.sudo()
        .with_context(active_test=False)
        .search([("routing_key", "=", routing_key)], limit=1)
    )
    if not endpoint:
        return endpoint
    company = endpoint.company_id
    return endpoint.with_company(company).with_context(allowed_company_ids=[company.id])


def _rejected(reason, status, *, endpoint=None, body=None):
    digest = hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else "none"
    _logger.warning(
        "Shared Meta webhook rejected: status=%s reason=%s endpoint=%s "
        "content_sha256=%s body_size=%s",
        status,
        reason,
        endpoint.public_ref if endpoint else "none",
        digest,
        len(body) if isinstance(body, bytes) else -1,
    )
    return _json_response({"error": reason}, status)


def _find_delivery(endpoint, digest):
    return (
        endpoint.env["meta.webhook.delivery"]
        .sudo()
        .search(
            [
                ("endpoint_id", "=", endpoint.id),
                ("content_sha256", "=", digest),
            ],
            limit=1,
        )
    )


def _bounded_request_body(endpoint):
    if request.httprequest.mimetype != "application/json":
        return None, _rejected("unsupported_media_type", 415, endpoint=endpoint)
    declared = request.httprequest.content_length
    if declared is None:
        return None, _rejected("length_required", 411, endpoint=endpoint)
    if declared <= 0:
        return None, _rejected("empty_payload", 400, endpoint=endpoint)
    if declared > MAX_WEBHOOK_BODY_BYTES:
        return None, _rejected("payload_too_large", 413, endpoint=endpoint)
    body = request.httprequest.get_data(cache=False)
    if len(body) != declared or len(body) > MAX_WEBHOOK_BODY_BYTES:
        return None, _rejected(
            "invalid_content_length", 400, endpoint=endpoint, body=body
        )
    return body, None


def _persist_delivery(
    endpoint,
    runtime,
    body,
    digest,
    decoded,
    sanitized,
    endpoint_revision,
):
    existing = _find_delivery(endpoint, digest)
    if existing:
        existing._enqueue()
        return existing, True
    delivery_model = (
        endpoint.env["meta.webhook.delivery"]
        .sudo()
        .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
    )
    values = {
        "endpoint_id": endpoint.id,
        "endpoint_revision": endpoint_revision,
        "app_revision": runtime.revision,
        "content_sha256": digest,
        "body_size_bytes": len(body),
        "object_type": sanitized.object_type,
        "graph_version": runtime.graph_version,
        "sanitized_envelope_json": sanitized.envelope,
    }
    try:
        with endpoint.env.cr.savepoint():
            delivery = delivery_model.create(values)
            endpoint.env["meta.webhook.dispatcher"]._ingest_delivery(
                endpoint, delivery, decoded, sanitized
            )
            delivery._enqueue()
            return delivery, False
    except IntegrityError:
        delivery = _find_delivery(endpoint, digest)
        if not delivery:
            raise
        delivery._enqueue()
        return delivery, True


class MetaWebhookController(http.Controller):
    @http.route(
        "/meta/webhook/<string:routing_key>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
    )
    def verify(self, routing_key, **_kwargs):
        endpoint = _endpoint(routing_key)
        if not endpoint:
            return _plain_response("Not Found", 404)
        mode = request.httprequest.args.get("hub.mode", "")
        challenge = request.httprequest.args.get("hub.challenge", "")
        candidate = request.httprequest.args.get("hub.verify_token", "")
        if (
            mode != "subscribe"
            or not challenge
            or len(challenge) > 2048
            or len(candidate) > 512
        ):
            return _plain_response("Forbidden", 403)
        try:
            _runtime, verify_token, _revision = endpoint._locked_runtime()
        except MetaCredentialResolutionError:
            return _plain_response("Not Found", 404)
        if not _constant_time_text_equal(candidate, verify_token):
            return _plain_response("Forbidden", 403)
        return _plain_response(challenge)

    @http.route(
        "/meta/webhook/<string:routing_key>",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook(self, routing_key, **_kwargs):
        endpoint = _endpoint(routing_key)
        if not endpoint:
            return _rejected("not_found", 404)
        body, rejection = _bounded_request_body(endpoint)
        if rejection:
            return rejection
        expected_endpoint_revision = endpoint.revision
        expected_app_revision = endpoint.app_id.revision
        try:
            runtime, _token, _revision = endpoint._locked_runtime(
                expected_revision=expected_endpoint_revision,
                expected_app_revision=expected_app_revision,
                require_verify_token=False,
            )
        except MetaCredentialResolutionError:
            return _rejected("not_found", 404, endpoint=endpoint, body=body)
        if not verify_signature(runtime.app_secret, request.httprequest.headers, body):
            return _rejected("invalid_signature", 401, endpoint=endpoint, body=body)
        try:
            decoded = json.loads(body.decode("utf-8"))
            sanitized = sanitized_webhook(decoded)
        except (ValueError, RecursionError):
            return _rejected("invalid_envelope", 400, endpoint=endpoint, body=body)
        try:
            # Linearize admission with a pause or credential rotation. Recheck
            # HMAC with the fenced current App secret before durable evidence.
            runtime, _token, endpoint_revision = endpoint._locked_runtime(
                expected_revision=expected_endpoint_revision,
                expected_app_revision=expected_app_revision,
                require_verify_token=False,
            )
        except MetaCredentialResolutionError:
            return _rejected("configuration_changed", 409, endpoint=endpoint, body=body)
        if not verify_signature(runtime.app_secret, request.httprequest.headers, body):
            return _rejected("invalid_signature", 401, endpoint=endpoint, body=body)
        try:
            digest = hashlib.sha256(body).hexdigest()
            delivery, duplicate = _persist_delivery(
                endpoint,
                runtime,
                body,
                digest,
                decoded,
                sanitized,
                endpoint_revision,
            )
        except (MetaWebhookSanitizationError, ValueError):
            return _rejected("invalid_envelope", 400, endpoint=endpoint, body=body)
        except Exception as error:
            # Consumer extensions inspect the raw bounded envelope and may carry
            # provider-private values in their exceptions. Never render the
            # exception or traceback on this public ingress boundary.
            _logger.error(
                "Shared Meta webhook persistence failed for endpoint %s: %s",
                endpoint.public_ref,
                type(error).__name__,
            )
            return _rejected(
                "temporarily_unavailable", 503, endpoint=endpoint, body=body
            )
        return _json_response(
            {
                "accepted": True,
                "duplicate": duplicate,
                "delivery_ref": delivery.public_ref,
            }
        )
