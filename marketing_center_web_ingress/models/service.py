import datetime
import hashlib
import uuid

from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.dto import (
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    PrivacySnapshotDTO,
)

from ..services.contracts import (
    ACTION_EVENT_TYPES,
    CLICK_ID_FIELDS,
    INGRESS_PROVENANCE,
    WebIngressContractError,
    WebIngressResult,
    canonical_request_digest,
    normalize_allowed_hosts,
    normalize_allowed_origins,
    normalize_origin,
    parse_web_ingress_payload,
)
from ..services.errors import WebIngressSerializationFailure
from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN

CLICK_NAMESPACES = {
    "gclid": "google.gclid",
    "gbraid": "google.gbraid",
    "wbraid": "google.wbraid",
    "fbclid": "meta.fbclid",
}
REFERENCE_NAMESPACES = {
    "session_ref": "web.session",
    "visitor_ref": "web.visitor",
}


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MarketingWebIngressService(models.AbstractModel):
    _name = "marketing.web.ingress.service"
    _description = "Atomic Marketing Web Ingress Service"

    @api.model
    def _ingest_payload(
        self,
        endpoint,
        payload,
        *,
        origin,
        body_size_bytes=0,
        observed_at=None,
        ingress_provenance="server_internal",
    ):
        """Validate and atomically project one first-party event.

        This underscore-prefixed method is the stable server-side integration
        seam for a future Website adapter. It intentionally accepts neither a
        partner nor arbitrary form values.
        """

        if (
            not endpoint
            or len(endpoint) != 1
            or getattr(endpoint, "_name", "") != "marketing.web.ingress.endpoint"
        ):
            raise ValidationError(_("A single web ingress endpoint is required."))
        endpoint = (
            self.env["marketing.web.ingress.endpoint"].browse(endpoint.id).exists()
        )
        if not endpoint:
            raise ValidationError(_("A single web ingress endpoint is required."))
        # Fence configuration for the server-side seam too. The public
        # controller already locks while checking the current public key.
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        endpoint.invalidate_recordset(
            [
                "active",
                "key_revision",
                "config_revision",
                "allowed_origins",
                "allowed_hosts",
                "replay_window_seconds",
                "max_body_bytes",
                "max_field_length",
                "privacy_policy_set_by",
            ]
            + list(endpoint._privacy_policy_fields())
        )
        if not endpoint.active:
            raise AccessError(_("The web ingress endpoint is inactive."))
        if endpoint.company_id not in self.env.companies:
            raise AccessError(_("The web ingress endpoint belongs to another company."))
        if not endpoint._capture_policy_allows():
            raise AccessError(
                _("Optional attribution is blocked by the endpoint policy.")
            )
        if (
            not isinstance(body_size_bytes, int)
            or isinstance(body_size_bytes, bool)
            or body_size_bytes < 0
            or body_size_bytes > endpoint.max_body_bytes
        ):
            raise ValidationError(_("The web ingress body size is invalid."))
        if ingress_provenance not in INGRESS_PROVENANCE:
            raise ValidationError(_("The web ingress provenance is invalid."))
        observed_at = self._normalized_observed_at(observed_at)
        try:
            normalized_origin = normalize_origin(origin)
            allowed_origins = normalize_allowed_origins(endpoint.allowed_origins)
            allowed_hosts = normalize_allowed_hosts(endpoint.allowed_hosts)
            if normalized_origin not in allowed_origins:
                raise WebIngressContractError("origin is not allowed")
            normalized = parse_web_ingress_payload(
                payload,
                allowed_hosts=allowed_hosts,
                expected_origin=normalized_origin,
                max_field_length=endpoint.max_field_length,
            )
            if (
                ingress_provenance == "browser_capability"
                and normalized.event_type != "entry_point"
            ) or (
                ingress_provenance == "website_confirmed_action"
                and normalized.event_type not in ACTION_EVENT_TYPES
            ):
                raise WebIngressContractError(
                    "event type is incompatible with ingress provenance"
                )
            if (
                ingress_provenance in {"browser_capability", "website_confirmed_action"}
                and normalized.consent_state != "unknown"
            ):
                raise WebIngressContractError(
                    "public ingress cannot assert a consent decision"
                )
        except WebIngressContractError as error:
            raise ValidationError(
                _("Invalid web ingress envelope: %s") % error
            ) from error
        elapsed = abs((observed_at - normalized.occurred_at).total_seconds())
        if elapsed > endpoint.replay_window_seconds:
            raise ValidationError(_("The web ingress timestamp is outside its window."))
        request_digest = canonical_request_digest(
            normalized,
            normalized_origin,
            server_assigned_timestamp=(
                ingress_provenance == "website_confirmed_action"
                and normalized.event_type == "organic_link"
            ),
        )
        with self.env.cr.savepoint():
            return self._ingest_atomic(
                endpoint,
                normalized,
                normalized_origin,
                request_digest,
                observed_at,
                body_size_bytes,
                ingress_provenance,
            )

    @api.model
    def _ingest_atomic(
        self,
        endpoint,
        payload,
        origin,
        request_digest,
        observed_at,
        body_size_bytes,
        ingress_provenance,
    ):
        lock_key = "marketing_web_ingress:%s:%s" % (
            endpoint.id,
            payload.event_key_hash,
        )
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        if not self.env.cr.fetchone()[0]:
            # Odoo runs transactions at REPEATABLE READ. Waiting on the lock
            # would keep the old snapshot, so the loser could not observe the
            # winner's committed event and would hit the unique constraint.
            # A serialization signal makes the HTTP transaction retry from a
            # fresh snapshot while preserving event/touchpoint atomicity.
            raise WebIngressSerializationFailure(
                "Concurrent web ingress event requires a fresh transaction"
            )
        company = endpoint.company_id
        event_model = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        existing = event_model.search(
            [
                ("endpoint_id", "=", endpoint.id),
                ("event_key_hash", "=", payload.event_key_hash),
            ],
            limit=1,
        )
        if existing:
            disposition = (
                "duplicate" if existing.request_digest == request_digest else "conflict"
            )
            return WebIngressResult(
                disposition=disposition,
                event_ref=existing.public_ref,
                touchpoint_id=existing.touchpoint_id.id,
            )

        event_ref = str(uuid.uuid4())
        internal_event_model = event_model.with_context(
            marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
        )
        event = internal_event_model.create(
            {
                "endpoint_id": endpoint.id,
                "public_ref": event_ref,
                "event_key_hash": payload.event_key_hash,
                "request_digest": request_digest,
                "origin": origin,
                "landing_host": payload.landing_host,
                "occurred_at": payload.occurred_at,
                "observed_at": observed_at,
                "body_size_bytes": body_size_bytes,
                "key_revision": endpoint.key_revision,
                "config_revision": endpoint.config_revision,
                "ingress_provenance": ingress_provenance,
                "state": "processing",
                "retain_until": endpoint._retention_deadline(observed_at),
                "retention_policy_version": endpoint.privacy_policy_version,
                "retention_assigned_by": endpoint.privacy_policy_set_by.id,
                "retention_assigned_at": observed_at,
            }
        )
        click_refs = self._create_click_values(event, payload, observed_at)
        dto = self._touchpoint_dto(
            endpoint,
            event_ref,
            payload,
            origin,
            request_digest,
            observed_at,
            click_refs,
            ingress_provenance,
        )
        attribution_service = (
            self.env["marketing.attribution.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        result = attribution_service._ingest_touchpoint(company, dto)
        event.with_context(
            marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
        ).write({"touchpoint_id": result.touchpoint_id, "state": "done"})
        return WebIngressResult(
            disposition="accepted",
            event_ref=event_ref,
            touchpoint_id=result.touchpoint_id,
        )

    @api.model
    def _create_click_values(self, event, payload, observed_at):
        refs = {}
        values = []
        for field_name in CLICK_ID_FIELDS:
            value = payload.click_values.get(field_name)
            if not value:
                continue
            value_ref = "web-click:%s" % uuid.uuid4()
            refs[field_name] = value_ref
            values.append(
                {
                    "event_id": event.id,
                    "namespace": CLICK_NAMESPACES[field_name],
                    "value_ref": value_ref,
                    "comparison_hash": _sha256(value),
                    "protected_value": value,
                    "observed_at": observed_at,
                }
            )
        if values:
            (
                self.env["marketing.web.ingress.click.value"]
                .sudo()
                .with_context(allowed_company_ids=[event.company_id.id])
                .with_company(event.company_id)
                .with_context(marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN)
                .create(values)
            )
        return refs

    @api.model
    def _touchpoint_dto(
        self,
        endpoint,
        event_ref,
        payload,
        origin,
        request_digest,
        observed_at,
        click_refs,
        ingress_provenance,
    ):
        identifiers = []
        for field_name, value in payload.click_values.items():
            identifiers.append(
                MarketingIdentifierDTO(
                    namespace=CLICK_NAMESPACES[field_name],
                    role="click",
                    comparison_hash=_sha256(value),
                    value_ref=click_refs[field_name],
                    source_field=field_name,
                    purpose=endpoint.capture_purpose,
                    retain_until=endpoint._retention_deadline(observed_at).date(),
                )
            )
        for field_name, comparison_hash in payload.reference_hashes.items():
            identifiers.append(
                MarketingIdentifierDTO(
                    namespace=REFERENCE_NAMESPACES[field_name],
                    role=("session" if field_name == "session_ref" else "visitor"),
                    comparison_hash=comparison_hash,
                    source_field=field_name,
                    purpose=endpoint.capture_purpose,
                    retain_until=endpoint._retention_deadline(observed_at).date(),
                )
            )
        # Click parameters are evidence, not a causal classification. Keep the
        # event kind stable and let a future attribution model interpret them.
        touchpoint_type = payload.event_type
        is_action_event = payload.event_type in ACTION_EVENT_TYPES
        asset_refs = {"web.endpoint": endpoint.public_ref}
        if is_action_event:
            asset_refs.update(
                {
                    "web.action": payload.action_ref,
                    "web.route": payload.route_ref,
                }
            )
            if payload.model_ref:
                asset_refs["web.model"] = payload.model_ref
        privacy_decided_at = (
            payload.occurred_at if payload.consent_state != "unknown" else None
        )
        return MarketingTouchpointDTO(
            source_system="web.ingress",
            source_scope_ref="endpoint:%s" % endpoint.public_ref,
            source_occurrence_ref="event:%s" % payload.event_key_hash,
            source_evidence_ref="web.ingress.event:%s" % event_ref,
            source_schema_version=(
                "marketing.web.ingress.v2"
                if is_action_event
                else "marketing.web.ingress.v1"
            ),
            occurred_at=payload.occurred_at,
            observed_at=observed_at,
            platform="web",
            channel="website",
            touchpoint_type=touchpoint_type,
            evidence_level="first_party",
            landing_url=payload.landing_url,
            referrer_url=payload.referrer_url,
            utm=payload.utm,
            asset_refs=asset_refs,
            identifiers=tuple(identifiers),
            privacy=PrivacySnapshotDTO(
                policy_version=endpoint.privacy_policy_version,
                notice_version=endpoint.privacy_notice_version,
                legal_basis_code=endpoint.privacy_legal_basis_code,
                consent_state=payload.consent_state,
                decision_source=(
                    "server_internal" if payload.consent_state != "unknown" else ""
                ),
                decided_at=privacy_decided_at,
            ),
            extensions={
                "web_ingress.origin": origin,
                "web_ingress.request_digest": request_digest,
                "web_ingress.key_revision": endpoint.key_revision,
                "web_ingress.config_revision": endpoint.config_revision,
                "web_ingress.provenance": ingress_provenance,
            },
            mapping_version=2 if is_action_event else 1,
        )

    @api.model
    def _normalized_observed_at(self, value):
        value = value or datetime.datetime.utcnow()
        if not isinstance(value, datetime.datetime):
            raise ValidationError(_("The observed timestamp is invalid."))
        if value.tzinfo:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0)
