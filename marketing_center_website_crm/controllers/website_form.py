import datetime
import hashlib
import json
import logging
import re
import uuid
from urllib.parse import urlsplit

from psycopg2 import Error as PsycopgError
from psycopg2.errors import DeadlockDetected, SerializationFailure

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.marketing_center_website.controllers.website_action import (
    MarketingWebsiteFormController,
    _response_text,
    _same_origin,
)
from odoo.addons.marketing_center_website.models.website_visitor import GEO_FIELDS
from odoo.addons.marketing_center_website.services.contracts import FORM_QUERY_FIELDS
from odoo.addons.website_crm.controllers.website_form import (
    WebsiteForm as NativeWebsiteCrmFormController,
)

_logger = logging.getLogger(__name__)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I
)


def _submission_hash():
    """Hash business input, excluding transient tokens and technical query."""
    digest = hashlib.sha256()
    pairs = sorted(
        (key, values)
        for key, values in request.httprequest.form.lists()
        if key not in {
            "csrf_token",
            "recaptcha_token_response",
            "mc_event",
            "mc_action",
            "mc_session",
        }
    )
    digest.update(json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode())
    for name, uploads in sorted(request.httprequest.files.lists()):
        for upload in uploads:
            digest.update(
                json.dumps([name, upload.filename, upload.content_type]).encode()
            )
            stream = upload.stream
            position = stream.tell()
            try:
                stream.seek(0)
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            finally:
                stream.seek(position)
    return digest.hexdigest()


def _append_native_event(result, event_id):
    try:
        payload = json.loads(_response_text(result))
    except (TypeError, ValueError):
        return result
    if (
        not isinstance(payload, dict)
        or type(payload.get("id")) is not int
        or payload["id"] <= 0
    ):
        return result
    payload["marketing_center_event_id"] = event_id
    serialized = json.dumps(payload, separators=(",", ":"))
    if hasattr(result, "set_data"):
        result.set_data(serialized)
        return result
    return serialized


def _form_claim():
    """Peek at the opaque claim without consuming the parent controller args."""
    names = {
        "mc_action": "action_ref",
        "mc_event": "event_id",
        "mc_session": "session_ref",
    }
    claim = {}
    for query_name in FORM_QUERY_FIELDS:
        values = request.httprequest.args.getlist(query_name)
        if len(values) != 1:
            return {}
        claim[names[query_name]] = values[0]
    return claim


class MarketingWebsiteCrmFormController(
    MarketingWebsiteFormController,
    NativeWebsiteCrmFormController,
):
    """Compose Marketing receipts with the complete native Website CRM flow.

    The base order is intentional. ``MarketingWebsiteFormController`` owns the
    receipt wrapper around the public route while ``NativeWebsiteCrmFormController``
    remains in the cooperative MRO so its phone, geo, visitor and lead hooks are
    dispatched by Odoo's native ``WebsiteForm`` implementation.
    """

    def extract_data(self, model, values):
        if model.sudo().model == "crm.lead":
            # Never accept an observation from the form, including as a custom
            # description field if the native form whitelist rejects it.
            values = {
                key: value for key, value in values.items() if key not in GEO_FIELDS
            }
        return super().extract_data(model, values)

    def insert_record(self, request, model, values, custom, meta=None):
        native_default = False
        utm_values = {}
        if model.sudo().model == "crm.lead" and getattr(request, "website", None):
            website = request.website
            binding = (
                request.env["marketing.website.ingress.binding"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    [
                        ("website_id", "=", website.id),
                        ("company_id", "=", website.company_id.id),
                    ],
                    limit=1,
                )
            )
            if binding and not binding.endpoint_id._capture_policy_allows():
                # Native utm.mixin.default_get reads residual cookies without a
                # consent check. Discard attribution while retaining the native
                # Website channel, which describes this form rather than cookies.
                # Do not reuse medium_id: the native input filter may already
                # have resolved a residual cookie into that value.
                values = dict(
                    values,
                    campaign_id=False,
                    source_id=False,
                    medium_id=request.env.ref("utm.utm_medium_website").id,
                )
            elif binding:
                # Prove this is the native fallback at insertion. Equality with
                # Website on an old CRM record cannot establish provenance.
                incoming = getattr(request, "params", {})
                cookies = request.httprequest.cookies
                native_default = (
                    values.get("medium_id")
                    == request.env.ref("utm.utm_medium_website").id
                    and not values.get("campaign_id")
                    and not values.get("source_id")
                    and not any(
                        incoming.get(key)
                        for key in ("campaign_id", "source_id", "medium_id")
                    )
                    and not any(
                        cookies.get(key)
                        for key in (
                            "odoo_utm_campaign",
                            "odoo_utm_source",
                            "odoo_utm_medium",
                        )
                    )
                )
        native = getattr(request, "_marketing_native_submission", None)
        if native and model.sudo().model == "crm.lead":
            values = dict(values, **native["values"])
            values.update(native.get("geo_values", {}))
            try:
                with request.env.cr.savepoint():
                    utm_values = (
                        request.env["marketing.website.crm.service"]
                        .sudo()
                        ._native_utm_values(
                            native["values"]["marketing_native_snapshot"],
                            request.params,
                        )
                    )
                # Assigned before create: native defaults, never a later manual
                # edit. A submitted field retains its explicitly chosen value.
                values.update(utm_values)
            except (SerializationFailure, DeadlockDetected):
                raise
            except Exception as error:
                _logger.warning(
                    "Native UTM defaults unavailable (%s)", type(error).__name__
                )
        result = super().insert_record(request, model, values, custom, meta=meta)
        if native_default and result:
            request.env["crm.lead"].sudo().browse(
                result
            )._mark_native_utm_website_default()
        if native and result:
            self._mark_native_acquisition_defaults(result, utm_values)
        return result

    def _mark_native_acquisition_defaults(self, lead_id, utm_values):
        try:
            with request.env.cr.savepoint():
                request.env[
                    "marketing.website.crm.service"
                ].sudo()._mark_native_utm_defaults(
                    request.env["crm.lead"].sudo().browse(lead_id),
                    utm_values,
                    request.params,
                )
        except (SerializationFailure, DeadlockDetected):
            raise
        except Exception as error:
            _logger.warning(
                "Native UTM provenance unavailable (%s)", type(error).__name__
            )

    @http.route()
    def website_form(self, model_name, **kwargs):
        try:
            with request.env.cr.savepoint():
                native = self._prepare_native_submission(model_name)
        except (SerializationFailure, DeadlockDetected):
            raise
        except ValidationError:
            return json.dumps(
                {
                    "error": (
                        "This submission reference was already used "
                        "for different form data."
                    ),
                    "marketing_center_submission_conflict": True,
                }
            )
        except Exception as error:
            _logger.warning(
                "Native acquisition preparation unavailable (%s)", type(error).__name__
            )
            native = None
        if native:
            request._marketing_native_submission = native
            try:
                if native["existing"]:
                    result = json.dumps({"id": native["existing"].id})
                else:
                    result = super().website_form(model_name, **kwargs)
                return self._confirm_native_result(result, native["event_id"])
            finally:
                del request._marketing_native_submission
        claim = _form_claim()
        origin = _same_origin()
        result = super().website_form(model_name, **kwargs)
        if (
            model_name != "crm.lead"
            or not claim
            or not origin
            or not request.env.user._is_public()
        ):
            return result
        try:
            # Validation and durable intent creation happen before processing.
            # Once the intent exists, unexpected projection failures are stored
            # for bounded recovery without losing the native lead.
            with request.env.cr.savepoint():
                (
                    request.env["marketing.website.crm.service"]
                    .sudo()
                    .with_context(allowed_company_ids=[request.website.company_id.id])
                    .with_company(request.website.company_id)
                    ._capture_native_form_intent(
                        request.website,
                        model_name,
                        claim,
                        origin,
                        _response_text(result),
                    )
                )
        except PsycopgError:
            # A retryable database failure must restart the complete HTTP
            # transaction; the native lead created by the failed attempt rolls
            # back with it.
            raise
        except (ValidationError, AccessError) as error:
            _logger.warning(
                "Website CRM correlation was rejected error_class=%s",
                type(error).__name__,
            )
        except Exception as error:  # Marketing evidence must not break native CRM.
            _logger.error(
                "Website CRM correlation was skipped error_class=%s",
                type(error).__name__,
            )
        return result

    def _confirm_native_result(self, result, event_id):
        try:
            payload = json.loads(_response_text(result))
        except (TypeError, ValueError):
            return result
        if isinstance(payload, dict) and type(payload.get("id")) is int:
            lead = request.env["crm.lead"].sudo().browse(payload["id"]).exists()
            if (
                lead
                and lead.marketing_native_website_id == request.website
                and lead.marketing_native_event_id == event_id
            ):
                self._capture_native_lead(lead)
                return _append_native_event(result, event_id)
        return result

    def _prepare_native_submission(self, model_name):
        if (
            model_name != "crm.lead"
            or not request.env.user._is_public()
            or request.httprequest.scheme != "https"
        ):
            return None
        website = request.website
        binding = website._marketing_measurement_binding()
        origin = _same_origin()
        if not origin:
            # Same-origin forms may omit Origin; a same-origin Referer is still
            # required. An explicit foreign Origin never gets this fallback.
            if request.httprequest.headers.get("Origin"):
                return None
            referrer = urlsplit(request.httprequest.referrer or "")
            host = urlsplit(request.httprequest.host_url)
            if (referrer.scheme, referrer.netloc) != (host.scheme, host.netloc):
                return None
            origin = "%s://%s" % (host.scheme, host.netloc)
        query = request.httprequest.args.getlist("mc_event")
        supplied = (
            query[0].lower() if len(query) == 1 and _UUID.fullmatch(query[0]) else ""
        )
        native_binding = binding and binding.capture_mode == "native"
        if not native_binding and not supplied:
            return None
        environ = request.httprequest.environ
        event_id = supplied or environ.setdefault(
            "marketing.native.event", str(uuid.uuid4())
        )
        submitted_at = environ.setdefault(
            "marketing.native.occurred_at",
            datetime.datetime.utcnow().replace(microsecond=0),
        )
        request_hash = _submission_hash()
        service = (
            request.env["marketing.website.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[website.company_id.id])
            .with_company(website.company_id)
        )
        existing = service._native_existing_submission(website, event_id, request_hash)
        # Replaying a successful submission still returns its original lead after
        # capture is paused or the binding changes mode.
        if not existing and not native_binding:
            return None
        snapshot = {}
        geo_values = {}
        visitor = None
        if not existing:
            try:
                with request.env.cr.savepoint():
                    visitor = request.env["website.visitor"]._get_visitor_from_request()
                    snapshot = service._native_snapshot(
                        website,
                        binding,
                        event_id,
                        origin,
                        request.httprequest.referrer or "",
                        submitted_at,
                        visitor=visitor,
                        cookies=request.httprequest.cookies,
                    )
            except (SerializationFailure, DeadlockDetected):
                raise
            except Exception as error:
                _logger.warning(
                    "Native acquisition snapshot unavailable (%s)", type(error).__name__
                )
            if snapshot:
                geo_values = self._prepare_ip_observation(visitor)
        return {
            "existing": existing,
            "event_id": event_id,
            "geo_values": geo_values,
            "values": {
                "marketing_native_website_id": website.id,
                "marketing_native_event_id": event_id,
                "marketing_native_request_hash": request_hash,
                "marketing_native_snapshot": snapshot,
                "marketing_native_capture_state": "pending" if snapshot else "skipped",
                "marketing_native_capture_reason": False
                if snapshot
                else "acquisition_unavailable",
            },
        }

    def _prepare_ip_observation(self, visitor):
        try:
            with request.env.cr.savepoint():
                visitor_model = request.env["website.visitor"]
                observation = visitor_model._marketing_request_observation()
                if observation:
                    if not visitor:
                        visitor = visitor_model._get_visitor_from_request(
                            force_create=True
                        )
                    if visitor:
                        visitor._marketing_observe(observation)
                return observation
        except (SerializationFailure, DeadlockDetected):
            raise
        except Exception as error:
            _logger.warning(
                "Native submission IP enrichment unavailable (%s)", type(error).__name__
            )
            return {}

    def _capture_native_lead(self, lead):
        try:
            with request.env.cr.savepoint():
                request.env["marketing.website.crm.service"].sudo().with_context(
                    allowed_company_ids=[lead.company_id.id]
                ).with_company(lead.company_id)._capture_native_submission(lead)
        except (SerializationFailure, DeadlockDetected):
            raise
        except Exception as error:
            _logger.warning(
                "Native acquisition capture failed for CRM %s (%s)",
                lead.id,
                type(error).__name__,
            )
            lead.write(
                {
                    "marketing_native_capture_state": "error",
                    "marketing_native_capture_reason": type(error).__name__,
                }
            )
