import logging

from psycopg2 import Error as PsycopgError

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.marketing_center_website.controllers.website_action import (
    MarketingWebsiteFormController,
    _response_text,
    _same_origin,
)
from odoo.addons.marketing_center_website.services.contracts import FORM_QUERY_FIELDS
from odoo.addons.website_crm.controllers.website_form import (
    WebsiteForm as NativeWebsiteCrmFormController,
)

_logger = logging.getLogger(__name__)


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

    def insert_record(self, request, model, values, custom, meta=None):
        native_default = False
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
        result = super().insert_record(request, model, values, custom, meta=meta)
        if native_default and result:
            request.env["crm.lead"].sudo().browse(
                result
            )._mark_native_utm_website_default()
        return result

    @http.route()
    def website_form(self, model_name, **kwargs):
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
