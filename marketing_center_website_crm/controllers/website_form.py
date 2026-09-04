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
