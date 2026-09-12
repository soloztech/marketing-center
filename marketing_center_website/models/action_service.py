import datetime

from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.misc import consteq, hmac

from odoo.addons.marketing_center_web_ingress.services.contracts import (
    WebIngressContractError,
    normalize_allowed_origins,
    normalize_origin,
)

from ..services.contracts import (
    WebsiteActionContractError,
    canonical_body_size,
    opaque_uuid,
    parse_form_exchange,
    parse_whatsapp_claim,
)

_FORM_RECEIPT_SCOPE = "marketing_center_website.form_receipt.v1"


class MarketingWebsiteActionService(models.AbstractModel):
    _name = "marketing.website.action.service"
    _description = "Atomic Marketing Website Action Service"

    @api.model
    def _admit_public_website(self, website, request_kind):
        """Rate-limit a bounded same-origin request before action resolution.

        Invalid action references and receipts must consume the application
        safety net too; otherwise a custom client could bypass it by forcing
        indexed action lookups or HMAC checks that never reach ingestion.
        """

        company = website.company_id
        binding = (
            self.env["marketing.website.ingress.binding"]
            .sudo()
            .with_context(allowed_company_ids=[company.id], active_test=False)
            .with_company(company)
            .search(
                [
                    ("website_id", "=", website.id),
                    ("company_id", "=", company.id),
                    ("active", "=", True),
                    ("endpoint_id.active", "=", True),
                ],
                limit=1,
            )
        )
        if not binding:
            raise AccessError(_("The Website ingress binding is unavailable."))
        endpoint = binding.endpoint_id
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        endpoint.invalidate_recordset(
            ["active", "company_id"] + list(endpoint._privacy_policy_fields())
        )
        binding.invalidate_recordset(
            ["active", "website_id", "company_id", "endpoint_id"]
        )
        if not (
            binding.active
            and binding.website_id == website
            and binding.company_id == company
            and binding.endpoint_id == endpoint
            and endpoint.active
            and endpoint.company_id == company
        ):
            raise AccessError(_("The Website ingress binding is unavailable."))
        return (
            self.env["marketing.web.ingress.admission"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
            ._admit(endpoint, request_kind)
        )

    @api.model
    def _active_action(self, website, action_ref, kind):
        action_ref = opaque_uuid(action_ref, "action_ref")
        company = website.company_id
        action = (
            self.env["marketing.website.action"]
            .sudo()
            .with_context(allowed_company_ids=[company.id], active_test=False)
            .with_company(company)
            .search(
                [
                    ("public_ref", "=", action_ref),
                    ("website_id", "=", website.id),
                    ("company_id", "=", company.id),
                    ("kind", "=", kind),
                    ("active", "=", True),
                    ("binding_id.active", "=", True),
                    ("binding_id.endpoint_id.active", "=", True),
                ],
                limit=1,
            )
        )
        if not action:
            raise AccessError(_("The Website action is unavailable."))
        endpoint = self._lock_effective_configuration(action)
        if not (
            action.active
            and action.public_ref == action_ref
            and action.website_id == website
            and action.company_id == company
            and action.kind == kind
            and action.binding_id.active
            and endpoint.active
        ):
            raise AccessError(_("The Website action is unavailable."))
        return action

    @api.model
    def _lock_effective_configuration(self, action):
        action.ensure_one()
        endpoint = action.binding_id.endpoint_id
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint " "WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        endpoint.invalidate_recordset(["active", "company_id"])
        action.binding_id.invalidate_recordset(
            ["active", "website_id", "company_id", "endpoint_id"]
        )
        action.invalidate_recordset(
            ["active", "website_id", "company_id", "kind", "public_ref"]
        )
        if not (
            action.binding_id.endpoint_id == endpoint
            and endpoint.company_id == action.company_id
            and endpoint._capture_policy_allows()
        ):
            raise AccessError(_("The Website action configuration is unavailable."))
        return endpoint

    @api.model
    def _prepare_form_receipt(self, website, model_name, claim, origin, now=None):
        try:
            action_ref = opaque_uuid(claim.get("action_ref"), "action_ref")
            event_id = opaque_uuid(claim.get("event_id"), "event_id")
            session_ref = opaque_uuid(claim.get("session_ref"), "session_ref")
        except (AttributeError, WebsiteActionContractError) as error:
            raise ValidationError(_("Invalid Website form reference.")) from error
        action = self._active_action(website, action_ref, "form_submission")
        self._validate_runtime_origin(action, origin)
        if action.form_model_name != model_name:
            raise AccessError(_("The Website form model does not match the action."))
        now = self._utc(now)
        issued_epoch = int(now.replace(tzinfo=datetime.timezone.utc).timestamp())
        expires_epoch = issued_epoch + action.token_ttl_seconds
        signature = hmac(
            action.env,
            _FORM_RECEIPT_SCOPE,
            self._receipt_message(
                action,
                event_id,
                session_ref,
                issued_epoch,
                expires_epoch,
            ),
        )
        return "%s.%s.%s" % (issued_epoch, expires_epoch, signature)

    @api.model
    def _exchange_form_receipt(self, website, payload, origin, now=None):
        action, values, occurred_at = self._validate_form_receipt(
            website, payload, origin, now=now
        )
        return self._ingest_action(
            action,
            values["event_id"],
            values["session_ref"],
            "form_submission",
            origin,
            occurred_at,
        )

    @api.model
    def _validate_form_receipt(self, website, payload, origin, now=None):
        """Validate a native-form receipt without creating marketing evidence."""
        try:
            values = parse_form_exchange(payload)
        except WebsiteActionContractError as error:
            raise ValidationError(_("Invalid Website action envelope.")) from error
        action = self._active_action(website, values["action_ref"], "form_submission")
        self._validate_runtime_origin(action, origin)
        now = self._utc(now)
        now_epoch = int(now.replace(tzinfo=datetime.timezone.utc).timestamp())
        if (
            values["issued_epoch"] > now_epoch + 5
            or values["expires_epoch"] < now_epoch
            or values["expires_epoch"] - values["issued_epoch"]
            != action.token_ttl_seconds
        ):
            raise AccessError(_("The Website form receipt has expired."))
        expected = hmac(
            action.env,
            _FORM_RECEIPT_SCOPE,
            self._receipt_message(
                action,
                values["event_id"],
                values["session_ref"],
                values["issued_epoch"],
                values["expires_epoch"],
            ),
        )
        if not consteq(values["signature"], expected):
            raise AccessError(_("The Website form receipt is invalid."))
        occurred_at = datetime.datetime.utcfromtimestamp(values["issued_epoch"])
        return action, values, occurred_at

    @api.model
    def _claim_whatsapp_handoff(self, website, payload, origin, now=None):
        try:
            values = parse_whatsapp_claim(payload)
        except WebsiteActionContractError as error:
            raise ValidationError(_("Invalid Website action envelope.")) from error
        action = self._active_action(website, values["action_ref"], "whatsapp_handoff")
        self._validate_runtime_origin(action, origin)
        occurred_at = self._utc(now)
        # The public controller catches application failures and returns 503.
        # Keep the evidence and usable redirect atomic even when issuance fails.
        with self.env.cr.savepoint():
            result = self._ingest_action(
                action,
                values["event_id"],
                values["session_ref"],
                "organic_link",
                origin,
                occurred_at,
            )
            if result.disposition not in ("accepted", "duplicate"):
                return result, ""
            token = (
                self.env["marketing.website.redirect.grant"]
                .sudo()
                .with_context(allowed_company_ids=[action.company_id.id])
                .with_company(action.company_id)
                ._issue(action, values["event_id"], now=occurred_at)
            )
        return result, token

    @api.model
    def _ingest_action(
        self,
        action,
        event_id,
        session_ref,
        event_type,
        origin,
        occurred_at,
    ):
        endpoint = self._lock_effective_configuration(action)
        expected_kinds = {
            "form_submission": "form_submission",
            "organic_link": "whatsapp_handoff",
        }
        expected_kind = expected_kinds.get(event_type)
        if not expected_kind:
            raise ValidationError(_("The Website action event type is invalid."))
        if action.kind != expected_kind:
            raise ValidationError(_("The Website action kind is invalid."))
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at.replace(
                tzinfo=datetime.timezone.utc
            ).isoformat(),
            "landing_url": "%s%s" % (origin.rstrip("/"), action.source_path),
            "consent_state": "unknown",
            "session_ref": session_ref,
            "action_ref": action.public_ref,
            "route_ref": action.route_ref,
        }
        if event_type == "form_submission":
            payload["model_ref"] = action.form_model_name
        company = action.company_id
        return (
            self.env["marketing.web.ingress.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
            ._ingest_payload(
                endpoint,
                payload,
                origin=origin,
                body_size_bytes=canonical_body_size(payload),
                observed_at=occurred_at,
                ingress_provenance="website_confirmed_action",
            )
        )

    @api.model
    def _validate_runtime_origin(self, action, origin):
        try:
            normalized = normalize_origin(origin)
            allowed = normalize_allowed_origins(
                action.binding_id.endpoint_id.allowed_origins
            )
        except WebIngressContractError as error:
            raise AccessError(_("The Website origin is unavailable.")) from error
        if normalized not in allowed:
            raise AccessError(_("The Website origin is unavailable."))

    @api.model
    def _receipt_message(
        self,
        action,
        event_id,
        session_ref,
        issued_epoch,
        expires_epoch,
    ):
        endpoint = action.binding_id.endpoint_id
        return "|".join(
            [
                "v1",
                str(action.company_id.id),
                str(action.website_id.id),
                str(action.binding_id.id),
                endpoint.public_ref,
                str(endpoint.config_revision),
                action.public_ref,
                action.form_model_name,
                event_id,
                session_ref,
                str(issued_epoch),
                str(expires_epoch),
            ]
        )

    @api.model
    def _utc(self, value):
        value = value or datetime.datetime.utcnow()
        if not isinstance(value, datetime.datetime):
            raise ValidationError(_("The Website action timestamp is invalid."))
        if value.tzinfo:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0)
