import json
import logging

from psycopg2 import Error as PsycopgError

from odoo import models
from odoo.http import request

_logger = logging.getLogger(__name__)

_NATIVE_UTM_FIELDS = (
    ("utm_campaign", "campaign_id", "odoo_utm_campaign"),
    ("utm_source", "source_id", "odoo_utm_source"),
    ("utm_medium", "medium_id", "odoo_utm_medium"),
)
_NATIVE_UTM_SCOPE = object()
_UNSET = object()
_SCOPE_ATTRIBUTE = "_marketing_native_utm_scope"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _marketing_allows_native_utm(cls, response):
        """Authorize the native writer, never a general optional-cookie grant.

        The informational policy is an operator setting, not visitor consent.
        An explicit optional-cookie choice retains native semantics. Extending
        utm.mixin.tracking_fields also requires a separate review of this scope.
        """
        if (
            not request
            or not getattr(request, "is_frontend", False)
            or not getattr(request, "website", None)
            or not request.env.user._is_public()
            or request.httprequest.method != "GET"
            or request.httprequest.scheme != "https"
            or response.status_code != 200
            or response.mimetype != "text/html"
        ):
            return False
        if tuple(request.env["utm.mixin"].tracking_fields()) != _NATIVE_UTM_FIELDS:
            return False
        if not any(name in request.params for name, _, _ in _NATIVE_UTM_FIELDS):
            return False
        native_preference = request.httprequest.cookies.get("website_cookies_bar")
        if native_preference is not None:
            try:
                preference = json.loads(native_preference)
            except (TypeError, ValueError):
                return False
            if not isinstance(preference, dict) or "optional" in preference:
                return False

        website = request.website
        config = website._marketing_measurement_config()
        if not config.get("form"):
            return False
        binding = website._marketing_measurement_binding()
        if not binding or binding.company_id != website.company_id:
            return False
        endpoint = binding.endpoint_id
        if (
            endpoint.company_id != website.company_id
            or not endpoint._informational_notice()
            or not endpoint._capture_policy_allows()
        ):
            return False
        action = request.env["marketing.website.action"].sudo().search(
            [
                ("public_ref", "=", config["form"]),
                ("binding_id", "=", binding.id),
                ("website_id", "=", website.id),
                ("company_id", "=", website.company_id.id),
                ("source_path", "=", request.httprequest.path),
                ("active", "=", True),
                ("kind", "=", "form_submission"),
                ("form_model_id.website_form_access", "=", True),
            ],
            limit=2,
        )
        return bool(len(action) == 1 and action.form_model_name == "crm.lead")

    @classmethod
    def _set_utm(cls, response):
        try:
            allowed = cls._marketing_allows_native_utm(response)
        except PsycopgError:
            # Serialization/deadlock errors belong to Odoo's request retry.
            raise
        except Exception as error:  # pylint: disable=broad-except
            # Marketing configuration must not make a public page unavailable.
            _logger.warning("Native UTM policy unavailable (%s)", type(error).__name__)
            allowed = False
        if not allowed:
            return super()._set_utm(response)
        previous = getattr(request, _SCOPE_ATTRIBUTE, _UNSET)
        setattr(request, _SCOPE_ATTRIBUTE, _NATIVE_UTM_SCOPE)
        try:
            return super()._set_utm(response)
        finally:
            if previous is _UNSET:
                delattr(request, _SCOPE_ATTRIBUTE)
            else:
                setattr(request, _SCOPE_ATTRIBUTE, previous)

    @classmethod
    def _is_allowed_cookie(cls, cookie_type):
        if (
            cookie_type == "optional"
            and request
            and getattr(request, _SCOPE_ATTRIBUTE, None) is _NATIVE_UTM_SCOPE
        ):
            return True
        return super()._is_allowed_cookie(cookie_type)
