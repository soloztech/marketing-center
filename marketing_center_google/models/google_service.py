from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.google_api_base.services.errors import (
    GoogleApiError,
    GoogleApiPausedError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
)
from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)
from odoo.addons.marketing_center_base.services.tokens import (
    MARKETING_CONFIGURATION_RUNTIME_TOKEN,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import GoogleMarketingReadAdapter
from ..services.catalog import GOOGLE_ADAPTER_KEY, GOOGLE_ADS_SERVICE
from ..services.tokens import (
    MARKETING_GOOGLE_CONNECTION_TOKEN,
    MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN,
)

_CAPABILITIES = {
    "api_version": "v25",
    "mutations": False,
    "read_catalog": True,
    "read_customers": True,
    "read_metrics": True,
}


class MarketingCenterGoogleService(models.AbstractModel):
    _name = "marketing.center.google.service"
    _description = "Marketing Center Google Ads Read Service"

    @api.model
    def _configuration_runtime_token(self):
        return MARKETING_CONFIGURATION_RUNTIME_TOKEN

    @api.model
    def _validate_profile(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_identity_revision,
    ):
        profile = self._validated_profile(
            profile,
            expected_profile_revision,
            expected_identity_revision,
        )
        if not profile:
            return {"stale": True}
        try:
            result = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=expected_profile_revision,
                expected_identity_revision=expected_identity_revision,
            ).validate()
        except (GoogleApiRateLimitError, GoogleApiTransientError) as error:
            terminal = self._terminalize_retry_exhaustion(
                profile,
                error,
                expected_profile_revision=expected_profile_revision,
                expected_identity_revision=expected_identity_revision,
                expected_method="_job_validate_google_profile",
                operation="validation",
            )
            if terminal:
                return (
                    {"stale": True}
                    if terminal == "stale"
                    else {"failed": True, "retry_exhausted": True}
                )
            raise RetryableJobError(
                "Google Ads validation is temporarily unavailable",
                seconds=error.retry_after_seconds or None,
            ) from None
        except GoogleApiError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_identity_revision,
            ):
                return {"stale": True}
            self._mark_profile_failure(profile, error)
            return {"failed": True}
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_identity_revision,
        ):
            return {"stale": True}
        self._mark_profile_success(
            profile,
            accessible_root_count=result["accessible_root_count"],
            request_id=result["request_id"],
        )
        return {"validated": True, "root_count": result["accessible_root_count"]}

    @api.model
    def _discover_sources(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_identity_revision,
    ):
        profile = self._validated_profile(
            profile,
            expected_profile_revision,
            expected_identity_revision,
        )
        if not profile:
            return {"discovered": 0, "stale": True}
        try:
            result = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=expected_profile_revision,
                expected_identity_revision=expected_identity_revision,
            ).discover_customers(
                max_roots=profile.max_discovery_roots,
                max_depth=profile.max_discovery_depth,
                max_customers=profile.max_discovery_customers,
                max_pages_per_manager=profile.max_discovery_pages,
            )
        except (GoogleApiRateLimitError, GoogleApiTransientError) as error:
            terminal = self._terminalize_retry_exhaustion(
                profile,
                error,
                expected_profile_revision=expected_profile_revision,
                expected_identity_revision=expected_identity_revision,
                expected_method="_job_discover_google_sources",
                operation="discovery",
            )
            if terminal:
                return (
                    {"discovered": 0, "stale": True}
                    if terminal == "stale"
                    else {
                        "discovered": 0,
                        "failed": True,
                        "retry_exhausted": True,
                    }
                )
            raise RetryableJobError(
                "Google Ads discovery is temporarily unavailable",
                seconds=error.retry_after_seconds or None,
            ) from None
        except GoogleApiError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_identity_revision,
            ):
                return {"discovered": 0, "stale": True}
            self._mark_profile_failure(profile, error)
            return {"discovered": 0, "failed": True}
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_identity_revision,
        ):
            return {"discovered": 0, "stale": True}
        projected = 0
        skipped_managers = 0
        failed = 0
        for customer in result.customers:
            if customer.manager:
                skipped_managers += 1
                continue
            try:
                with self.env.cr.savepoint():
                    self._project_customer(profile, customer)
                    projected += 1
            except (AccessError, ValidationError):
                failed += 1
        self._mark_profile_success(
            profile,
            accessible_root_count=result.root_count,
            discovered_customer_count=projected,
            discovery_request_count=result.request_count,
            degraded=bool(failed),
        )
        return {
            "discovered": projected,
            "failed": failed,
            "managers": skipped_managers,
            "requests": result.request_count,
        }

    @api.model
    def _validated_profile(
        self,
        profile,
        expected_profile_revision,
        expected_identity_revision,
    ):
        if (
            not profile
            or getattr(profile, "_name", "") != "marketing.center.google.profile"
            or len(profile) != 1
        ):
            raise ValidationError(_("A single Google profile is required."))
        profile = profile.sudo().exists()
        if not profile or profile.company_id not in self.env.companies:
            raise AccessError(_("The Google profile is not available."))
        return (
            profile
            if self._profile_is_current(
                profile,
                expected_profile_revision,
                expected_identity_revision,
            )
            else self.env["marketing.center.google.profile"]
        )

    @api.model
    def _profile_is_current(
        self,
        profile,
        expected_profile_revision,
        expected_identity_revision,
    ):
        """Read a pre-I/O revision snapshot without retaining database locks."""

        if (
            not isinstance(expected_profile_revision, int)
            or isinstance(expected_profile_revision, bool)
            or expected_profile_revision <= 0
            or not isinstance(expected_identity_revision, int)
            or isinstance(expected_identity_revision, bool)
            or expected_identity_revision <= 0
        ):
            raise ValidationError(_("Google profile fencing revisions are invalid."))
        identity = profile.google_identity_id.sudo()
        identity.invalidate_recordset(["active", "company_id", "revision"])
        profile.invalidate_recordset(
            [
                "active",
                "company_id",
                "google_identity_id",
                "identity_revision",
                "profile_revision",
            ]
        )
        return bool(
            profile.active
            and identity.active
            and profile.company_id == identity.company_id
            and profile.google_identity_id == identity
            and profile.profile_revision == expected_profile_revision
            and profile.identity_revision == expected_identity_revision
            and identity.revision == expected_identity_revision
        )

    @api.model
    def _lock_current_profile(
        self,
        profile,
        expected_profile_revision,
        expected_identity_revision,
    ):
        if (
            not isinstance(expected_profile_revision, int)
            or isinstance(expected_profile_revision, bool)
            or expected_profile_revision <= 0
            or not isinstance(expected_identity_revision, int)
            or isinstance(expected_identity_revision, bool)
            or expected_identity_revision <= 0
        ):
            raise ValidationError(_("Google profile fencing revisions are invalid."))
        identity = profile.google_identity_id.sudo()
        self.env.cr.execute(
            "SELECT id FROM google_api_identity WHERE id = %s FOR UPDATE",
            [identity.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM marketing_center_google_profile "
            "WHERE id = %s FOR UPDATE",
            [profile.id],
        )
        if not self.env.cr.fetchone():
            return False
        identity.invalidate_recordset(["active", "company_id", "revision"])
        profile.invalidate_recordset(
            [
                "active",
                "company_id",
                "google_identity_id",
                "identity_revision",
                "profile_revision",
            ]
        )
        return bool(
            profile.active
            and identity.active
            and profile.company_id == identity.company_id
            and profile.google_identity_id == identity
            and profile.profile_revision == expected_profile_revision
            and profile.identity_revision == expected_identity_revision
            and identity.revision == expected_identity_revision
        )

    @api.model
    def _terminalize_retry_exhaustion(
        self,
        profile,
        error,
        *,
        expected_profile_revision,
        expected_identity_revision,
        expected_method,
        operation,
    ):
        """Persist a fenced health result when OCA reaches its retry ceiling."""

        if not profile._queue_job_attempt_is_terminal(expected_method):
            return False
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_identity_revision,
        ):
            return "stale"
        classification = self._safe_error_class(
            getattr(error, "classification", "transient")
        )
        profile.with_context(
            marketing_google_profile_runtime_token=(
                MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
            )
        ).write(
            {
                "health_state": "degraded",
                "verified_at": fields.Datetime.now(),
                "last_error_class": classification,
                "last_error_message": (
                    "Google Ads reader %s exhausted its transient retry budget."
                    % operation
                ),
                "last_request_id": getattr(error, "request_id", "") or False,
            }
        )
        return "failed"

    @api.model
    def _project_customer(self, profile, customer):
        lock_key = "marketing_google_customer:%s:%s" % (
            profile.company_id.id,
            customer.resource_name,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        currency = (
            self.env["res.currency"]
            .sudo()
            .search([("name", "=", customer.currency)], limit=1)
        )
        if not currency:
            raise ValidationError(_("The Google customer currency is unavailable."))
        source_model = (
            self.env["marketing.center.source"].sudo().with_context(active_test=False)
        )
        source = source_model.search(
            [
                ("company_id", "=", profile.company_id.id),
                ("service", "=", GOOGLE_ADS_SERVICE),
                ("external_account_ref", "=", customer.resource_name),
            ],
            limit=1,
        )
        enabled = customer.status == "enabled" and not customer.hidden
        observed_at = fields.Datetime.now()
        runtime_context = {
            "marketing_configuration_runtime_token": (
                MARKETING_CONFIGURATION_RUNTIME_TOKEN
            )
        }
        if not source:
            source = (
                source_model.with_company(profile.company_id)
                .with_context(**runtime_context)
                .create(
                    {
                        "name": customer.name,
                        "company_id": profile.company_id.id,
                        "service": GOOGLE_ADS_SERVICE,
                        "external_account_ref": customer.resource_name,
                        "external_account_id": customer.customer_id,
                        "currency_id": currency.id,
                        "timezone": customer.timezone,
                        "state": "active" if enabled else "attention",
                        "read_enabled": True,
                        "write_enabled": False,
                        "effective_capabilities_json": _CAPABILITIES,
                        "first_observed_at": observed_at,
                        "last_observed_at": observed_at,
                    }
                )
            )
        else:
            values = {"last_observed_at": observed_at}
            desired = {
                "name": customer.name,
                "external_account_id": customer.customer_id,
                "currency_id": currency.id,
                "timezone": customer.timezone,
            }
            for field_name, value in desired.items():
                current = source[field_name]
                if hasattr(current, "id"):
                    current = current.id
                if hasattr(value, "id"):
                    value = value.id
                if current != value:
                    values[field_name] = value
            if (source.effective_capabilities_json or {}) != _CAPABILITIES:
                values["effective_capabilities_json"] = _CAPABILITIES
            if source.active and source.state not in {"paused", "disabled"}:
                state = "active" if enabled else "attention"
                if source.state != state:
                    values["state"] = state
            source.with_context(**runtime_context).write(values)
        if not source.active or source.state == "disabled":
            return source
        self._project_connection(
            profile,
            source,
            enabled,
            observed_at,
            customer.access_login_customer_id,
        )
        return source

    @api.model
    def _project_connection(
        self,
        profile,
        source,
        enabled,
        observed_at,
        login_customer_id,
    ):
        connection_model = (
            self.env["marketing.center.connection"]
            .sudo()
            .with_context(active_test=False)
        )
        connection = connection_model.search(
            [
                ("source_id", "=", source.id),
                ("adapter_key", "=", GOOGLE_ADAPTER_KEY),
                ("purpose", "=", "reader"),
            ],
            limit=1,
        )
        if connection:
            self.env.cr.execute(
                "SELECT id FROM marketing_center_connection WHERE id = %s FOR UPDATE",
                [connection.id],
            )
            if not self.env.cr.fetchone():
                raise ValidationError(
                    _("The Google reader connection no longer exists.")
                )
            connection.invalidate_recordset(
                [
                    "active",
                    "state",
                    "google_profile_id",
                    "profile_public_ref",
                    "profile_revision",
                    "google_identity_revision",
                    "google_login_customer_id",
                    "effective_capabilities_hash",
                    "health_state",
                    "verified_at",
                    "cooldown_until",
                    "last_health_error_class",
                    "last_health_error_message",
                ]
            )
        if connection and connection.google_profile_id != profile:
            raise ValidationError(
                _("The Google customer is already bound to another profile.")
            )
        capabilities_hash = sha256_text(canonical_json(_CAPABILITIES))
        values = {"verified_at": observed_at}
        binding_values = {
            "google_profile_id": profile.id,
            "profile_public_ref": profile.public_ref,
            "profile_revision": profile.profile_revision,
            "google_identity_revision": profile.identity_revision,
            "google_login_customer_id": login_customer_id or False,
        }
        if not connection:
            values.update(binding_values)
        else:
            for field_name, value in binding_values.items():
                current = connection[field_name]
                if hasattr(current, "id"):
                    current = current.id
                if current != value:
                    values[field_name] = value
        if (
            not connection
            or connection.effective_capabilities_hash != capabilities_hash
        ):
            values.update(
                {
                    "effective_capabilities_json": _CAPABILITIES,
                    "effective_capabilities_hash": capabilities_hash,
                }
            )
        can_activate = not connection or connection.active
        cooling_down = bool(
            connection
            and connection.cooldown_until
            and connection.cooldown_until > observed_at
        )
        if (
            can_activate
            and enabled
            and (
                not connection
                or connection.state not in {"disabled", "paused"}
                or connection.last_health_error_class == "profile_changed"
            )
        ):
            if not cooling_down:
                values.update(
                    {
                        "health_state": "healthy",
                        "cooldown_until": False,
                        "last_health_error_class": False,
                        "last_health_error_message": False,
                    }
                )
            if not connection or connection.state != "ready":
                values["state"] = "ready"
        elif (
            can_activate
            and not enabled
            and (not connection or connection.state != "disabled")
        ):
            values.update(
                {
                    "health_state": "degraded",
                    "last_health_error_class": "customer_unavailable",
                    "last_health_error_message": (
                        "Google Ads customer is not enabled for reading."
                    ),
                }
            )
            if not connection or connection.state != "degraded":
                values["state"] = "degraded"
        context = {
            "marketing_google_connection_token": MARKETING_GOOGLE_CONNECTION_TOKEN,
            "marketing_configuration_runtime_token": (
                MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ),
        }
        if connection:
            connection.with_context(**context).write(values)
        else:
            values.update(
                {
                    "name": "%s reader" % source.name,
                    "source_id": source.id,
                    "adapter_key": GOOGLE_ADAPTER_KEY,
                    "purpose": "reader",
                    "active": True,
                }
            )
            connection_model.with_company(profile.company_id).with_context(
                **context
            ).create(values)

    @api.model
    def _mark_profile_success(
        self,
        profile,
        *,
        accessible_root_count=0,
        discovered_customer_count=0,
        discovery_request_count=0,
        request_id="",
        degraded=False,
    ):
        profile.with_context(
            marketing_google_profile_runtime_token=MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "degraded" if degraded else "healthy",
                "verified_at": fields.Datetime.now(),
                "last_error_class": "projection_partial" if degraded else False,
                "last_error_message": (
                    "Some Google customers could not be projected."
                    if degraded
                    else False
                ),
                "last_request_id": request_id or False,
                "accessible_root_count": accessible_root_count,
                "discovered_customer_count": discovered_customer_count,
                "discovery_request_count": discovery_request_count,
            }
        )

    @api.model
    def _mark_profile_failure(self, profile, error):
        classification = self._safe_error_class(
            getattr(error, "classification", "permanent")
        )
        paused = isinstance(error, GoogleApiPausedError)
        profile.with_context(
            marketing_google_profile_runtime_token=MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "unhealthy" if paused else "degraded",
                "verified_at": fields.Datetime.now(),
                "last_error_class": classification,
                "last_error_message": "Google Ads reader validation failed safely.",
                "last_request_id": getattr(error, "request_id", "") or False,
            }
        )
        if paused:
            self._pause_profile_connections(profile, classification)

    @api.model
    def _pause_profile_connections(self, profile, classification):
        connections = profile.connection_ids.filtered(
            lambda connection: connection.active and connection.state != "disabled"
        )
        if not connections:
            return
        for connection in connections:
            values = {
                "health_state": "unhealthy",
                "last_health_error_class": classification,
                "last_health_error_message": (
                    "Google Ads authorization is unavailable."
                ),
            }
            if connection.state != "paused":
                values["state"] = "paused"
            connection.with_context(
                marketing_google_connection_token=MARKETING_GOOGLE_CONNECTION_TOKEN,
                marketing_configuration_runtime_token=(
                    MARKETING_CONFIGURATION_RUNTIME_TOKEN
                ),
            ).write(values)

    @api.model
    def _safe_error_class(self, value):
        value = str(value or "permanent").strip().lower()
        allowed = {
            "limit_exceeded",
            "paused",
            "permission_denied",
            "permanent",
            "rate_limited",
            "transient",
        }
        return value if value in allowed else "permanent"
