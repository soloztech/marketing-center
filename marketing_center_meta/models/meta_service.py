import datetime

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)
from odoo.addons.marketing_center_base.services.tokens import (
    MARKETING_CONFIGURATION_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    META_ADAPTER_KEY,
    META_ADS_SERVICE,
    MetaMarketingReadAdapter,
)
from ..services.tokens import (
    MARKETING_META_CONNECTION_TOKEN,
    MARKETING_META_PROFILE_RUNTIME_TOKEN,
)

_META_DISCOVERY_MISSING_ERROR = "meta_account_not_discovered"


class MarketingCenterMetaService(models.AbstractModel):
    _name = "marketing.center.meta.service"
    _description = "Marketing Center Meta Read Service"

    @api.model
    def _validate_profile(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_app_revision,
    ):
        profile = self._validated_profile(
            profile,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )
        if not profile:
            return {"stale": True}
        try:
            validation = MetaMarketingReadAdapter(
                profile,
                expected_app_revision=expected_app_revision,
            ).validate()
        except (MetaApiRateLimitError, MetaApiTransientError) as error:
            terminal = self._terminalize_retry_exhaustion(
                profile,
                error,
                expected_profile_revision=expected_profile_revision,
                expected_app_revision=expected_app_revision,
                expected_method="_job_validate_read_profile",
                operation="validation",
            )
            if terminal:
                return {"stale": True} if terminal == "stale" else False
            rate_limited = isinstance(error, MetaApiRateLimitError)
            raise RetryableJobError(
                (
                    "Meta read validation is rate limited"
                    if rate_limited
                    else "Meta read validation is temporarily unavailable"
                ),
                seconds=error.retry_after_seconds if rate_limited else None,
            ) from None
        except MetaApiPausedError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_app_revision,
            ):
                return {"stale": True}
            self._mark_profile_failure(profile, error, paused=True)
            return False
        except MetaApiError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_app_revision,
            ):
                return {"stale": True}
            self._mark_profile_failure(profile, error, paused=False)
            return False
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_app_revision,
        ):
            return {"stale": True}
        self._mark_profile_success(profile, validation)
        return validation

    @api.model
    def _discover_sources(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_app_revision,
    ):
        profile = self._validated_profile(
            profile,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )
        if not profile:
            return {"discovered": 0, "stale": True}
        if profile.reader_kind != "ads_reader":
            raise ValidationError(
                _("Lead Ads reader profiles do not discover ad accounts.")
            )
        try:
            adapter = MetaMarketingReadAdapter(
                profile,
                expected_app_revision=expected_app_revision,
            )
            validation = adapter.validate()
            accounts = adapter.discover_ad_accounts()
        except (MetaApiRateLimitError, MetaApiTransientError) as error:
            terminal = self._terminalize_retry_exhaustion(
                profile,
                error,
                expected_profile_revision=expected_profile_revision,
                expected_app_revision=expected_app_revision,
                expected_method="_job_discover_read_sources",
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
            rate_limited = isinstance(error, MetaApiRateLimitError)
            raise RetryableJobError(
                (
                    "Meta ad account discovery is rate limited"
                    if rate_limited
                    else "Meta ad account discovery is temporarily unavailable"
                ),
                seconds=error.retry_after_seconds if rate_limited else None,
            ) from None
        except MetaApiPausedError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_app_revision,
            ):
                return {"discovered": 0, "stale": True}
            self._mark_profile_failure(profile, error, paused=True)
            return {"discovered": 0, "failed": True}
        except MetaApiError as error:
            if not self._lock_current_profile(
                profile,
                expected_profile_revision,
                expected_app_revision,
            ):
                return {"discovered": 0, "stale": True}
            self._mark_profile_failure(profile, error, paused=False)
            return {"discovered": 0, "failed": True}
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_app_revision,
        ):
            return {"discovered": 0, "stale": True}
        observed_refs = {account.external_ref for account in accounts}
        source_ids = []
        account_failures = 0
        for account in accounts:
            try:
                with self.env.cr.savepoint():
                    source = self._upsert_source(
                        profile,
                        validation.capabilities,
                        account,
                    )
            except ValidationError:
                account_failures += 1
                continue
            source_ids.append(source.id)
        missing_count, reconciliation_failures = self._reconcile_missing_sources(
            profile,
            observed_refs,
        )
        self._mark_profile_success(profile, validation)
        failure_count = account_failures + reconciliation_failures
        if failure_count:
            self._mark_profile_projection_degraded(profile, failure_count)
        return {
            "discovered": len(source_ids),
            "failed_accounts": account_failures,
            "failed_reconciliations": reconciliation_failures,
            "missing": missing_count,
            "source_ids": source_ids,
        }

    @api.model
    def _validated_profile(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_app_revision,
    ):
        profile = profile.sudo().exists()
        if (
            not profile
            or profile._name != "marketing.center.meta.profile"
            or len(profile) != 1
            or profile.company_id not in self.env.companies
        ):
            raise AccessError(_("The Meta profile is not available."))
        if (
            not isinstance(expected_profile_revision, int)
            or isinstance(expected_profile_revision, bool)
            or expected_profile_revision <= 0
        ):
            raise ValidationError(_("The Meta profile fencing revision is invalid."))
        if (
            not isinstance(expected_app_revision, int)
            or isinstance(expected_app_revision, bool)
            or expected_app_revision <= 0
        ):
            raise ValidationError(_("The Meta App fencing revision is invalid."))
        if (
            not profile.active
            or profile.profile_revision != expected_profile_revision
            or profile.meta_app_id.revision != expected_app_revision
        ):
            return self.env["marketing.center.meta.profile"]
        return profile

    @api.model
    def _lock_current_profile(
        self,
        profile,
        expected_profile_revision,
        expected_app_revision,
    ):
        self.env.cr.execute(
            "SELECT id FROM marketing_center_meta_profile WHERE id = %s FOR UPDATE",
            [profile.id],
        )
        profile.invalidate_recordset(
            [
                "active",
                "meta_app_id",
                "credential_backend",
                "access_token_ref",
                "reader_kind",
                "required_scopes",
                "profile_revision",
            ]
        )
        profile.meta_app_id.invalidate_recordset(["active", "revision"])
        return bool(
            profile.exists()
            and profile.active
            and profile.profile_revision == expected_profile_revision
            and profile.meta_app_id.revision == expected_app_revision
        )

    @api.model
    def _terminalize_retry_exhaustion(
        self,
        profile,
        error,
        *,
        expected_profile_revision,
        expected_app_revision,
        expected_method,
        operation,
    ):
        """Persist a fenced terminal health result on the last OCA attempt."""

        if not profile._queue_job_attempt_is_terminal(expected_method):
            return False
        if not self._lock_current_profile(
            profile,
            expected_profile_revision,
            expected_app_revision,
        ):
            return "stale"
        classification = str(getattr(error, "classification", "transient"))
        if classification not in {"rate_limited", "transient"}:
            classification = "transient"
        profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "degraded",
                "verified_at": fields.Datetime.now(),
                "last_error_class": classification,
                "last_error_message": _(
                    "Meta reader %s exhausted its transient retry budget."
                )
                % operation,
            }
        )
        return "failed"

    @api.model
    def _mark_profile_success(self, profile, validation):
        now = fields.Datetime.now()
        profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "healthy",
                "verified_at": now,
                "last_error_class": False,
                "last_error_message": False,
                "verified_scopes_json": list(validation.scopes),
                "token_type": validation.token_type,
                "token_expires_at": self._unix_datetime(validation.expires_at),
                "data_access_expires_at": self._unix_datetime(
                    validation.data_access_expires_at
                ),
            }
        )

    @api.model
    def _mark_profile_failure(self, profile, error, *, paused):
        error_class = getattr(error, "classification", "permanent")
        error_message = str(error)[:512]
        profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "unhealthy",
                "verified_at": fields.Datetime.now(),
                "last_error_class": error_class,
                "last_error_message": error_message,
            }
        )
        context = {
            "marketing_configuration_runtime_token": (
                MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ),
            "marketing_meta_connection_token": MARKETING_META_CONNECTION_TOKEN,
        }
        for connection in profile.connection_ids.filtered(
            lambda item: item.active and item.state != "disabled"
        ):
            values = {"verified_at": fields.Datetime.now()}
            if connection.state != "disabled":
                expected_state = "paused" if paused else "error"
                if connection.state != expected_state:
                    values["state"] = expected_state
            if connection.health_state != "unhealthy":
                values["health_state"] = "unhealthy"
            if connection.last_health_error_class != error_class:
                values["last_health_error_class"] = error_class
            if connection.last_health_error_message != error_message:
                values["last_health_error_message"] = error_message
            connection.with_context(**context).write(values)

    @api.model
    def _mark_profile_projection_degraded(self, profile, failure_count):
        profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "health_state": "degraded",
                "last_error_class": "configuration",
                "last_error_message": _(
                    "Some discovered Meta accounts could not be projected (%s)."
                )
                % failure_count,
            }
        )

    @api.model
    def _reconcile_missing_sources(self, profile, observed_refs):
        connection_model = (
            self.env["marketing.center.connection"]
            .sudo()
            .with_context(active_test=False)
        )
        connections = connection_model.search(
            [
                ("meta_profile_id", "=", profile.id),
                ("adapter_key", "=", META_ADAPTER_KEY),
                ("purpose", "=", "reader"),
            ]
        )
        missing_count = 0
        failure_count = 0
        for connection in connections:
            source = connection.source_id
            if (
                not connection.active
                or connection.state == "disabled"
                or not source.active
                or source.state == "disabled"
                or source.service != META_ADS_SERVICE
                or source.external_account_ref in observed_refs
            ):
                continue
            try:
                with self.env.cr.savepoint():
                    self._pause_missing_source(source, connection)
            except ValidationError:
                failure_count += 1
                continue
            missing_count += 1
        return missing_count, failure_count

    @api.model
    def _pause_missing_source(self, source, connection):
        runtime_context = {
            "marketing_configuration_runtime_token": (
                MARKETING_CONFIGURATION_RUNTIME_TOKEN
            )
        }
        source_values = {}
        if source.state != "attention":
            source_values["state"] = "attention"
        if source.read_enabled:
            source_values["read_enabled"] = False
        if source_values:
            source.with_context(**runtime_context).write(source_values)

        message = _(
            "The Meta source was not returned by the latest authorized account "
            "discovery."
        )
        system_pause = bool(
            connection.state != "paused"
            or connection.last_health_error_class == _META_DISCOVERY_MISSING_ERROR
        )
        if system_pause:
            connection_context = dict(
                runtime_context,
                marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN,
            )
            connection_values = {"verified_at": fields.Datetime.now()}
            if connection.state != "paused":
                connection_values["state"] = "paused"
            if connection.health_state != "degraded":
                connection_values["health_state"] = "degraded"
            if connection.last_health_error_class != _META_DISCOVERY_MISSING_ERROR:
                connection_values[
                    "last_health_error_class"
                ] = _META_DISCOVERY_MISSING_ERROR
            if connection.last_health_error_message != message:
                connection_values["last_health_error_message"] = message
            connection.with_context(**connection_context).write(connection_values)

    @api.model
    def _upsert_source(self, profile, capabilities, account):
        if account.timezone not in pytz.all_timezones_set:
            raise ValidationError(_("A discovered Meta account timezone is invalid."))
        currency = self._active_currency(account.currency)
        source_model = (
            self.env["marketing.center.source"].sudo().with_context(active_test=False)
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [
                "marketing_meta_source:%s:%s"
                % (profile.company_id.id, account.external_ref)
            ],
        )
        source = source_model.search(
            [
                ("company_id", "=", profile.company_id.id),
                ("service", "=", META_ADS_SERVICE),
                ("external_account_ref", "=", account.external_ref),
            ],
            limit=1,
        )
        now = fields.Datetime.now()
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
                        "name": account.name,
                        "company_id": profile.company_id.id,
                        "service": META_ADS_SERVICE,
                        "external_account_ref": account.external_ref,
                        "external_account_id": account.external_id,
                        "currency_id": currency.id,
                        "timezone": account.timezone,
                        "state": (
                            "active" if account.account_status == 1 else "attention"
                        ),
                        "read_enabled": True,
                        "write_enabled": False,
                        "effective_capabilities_json": capabilities,
                        "first_observed_at": now,
                        "last_observed_at": now,
                    }
                )
            )
        else:
            values = {"last_observed_at": now}
            if source.name != account.name:
                values["name"] = account.name
            if source.external_account_id != account.external_id:
                values["external_account_id"] = account.external_id
            if source.currency_id != currency:
                values["currency_id"] = currency.id
            if source.timezone != account.timezone:
                values["timezone"] = account.timezone
            if source.effective_capabilities_json != capabilities:
                values["effective_capabilities_json"] = capabilities
            if not source.active:
                values.update(
                    {
                        "active": True,
                        "state": (
                            "active" if account.account_status == 1 else "attention"
                        ),
                        "read_enabled": True,
                    }
                )
            elif source.state in {"draft", "active", "attention"}:
                expected_state = (
                    "active" if account.account_status == 1 else "attention"
                )
                if source.state != expected_state:
                    values["state"] = expected_state
                if not source.read_enabled:
                    values["read_enabled"] = True
            source.with_context(**runtime_context).write(values)
        self._upsert_connection(profile, source, capabilities)
        return source

    @api.model
    def _active_currency(self, currency_name):
        currency = (
            self.env["res.currency"]
            .sudo()
            .with_context(active_test=False)
            .search([("name", "=", currency_name)], limit=1)
        )
        if not currency:
            raise ValidationError(
                _("A discovered Meta account currency is unavailable.")
            )
        currency.invalidate_recordset(["active"])
        if not currency.active:
            currency.write({"active": True})
        return currency

    @api.model
    def _upsert_connection(self, profile, source, capabilities):
        # An archived connection still owns the database uniqueness key.  It must
        # be observed (and left untouched) instead of being hidden by Odoo's
        # implicit active_test domain and accidentally recreated.
        connection_model = (
            self.env["marketing.center.connection"]
            .sudo()
            .with_context(active_test=False)
        )
        connection = connection_model.search(
            [
                ("source_id", "=", source.id),
                ("adapter_key", "=", META_ADAPTER_KEY),
                ("purpose", "=", "reader"),
            ],
            limit=1,
        )
        if connection and connection.meta_profile_id != profile:
            raise ValidationError(_("The Meta source is bound to another profile."))
        if connection and (not connection.active or connection.state == "disabled"):
            return connection
        capability_hash = sha256_text(canonical_json(capabilities))
        context = {
            "marketing_configuration_runtime_token": (
                MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ),
            "marketing_meta_connection_token": MARKETING_META_CONNECTION_TOKEN,
        }
        now = fields.Datetime.now()
        if connection:
            values = self._connection_success_values(
                connection,
                profile,
                capabilities,
                capability_hash,
                now,
            )
            connection.with_context(**context).write(values)
            return connection
        values = {
            "name": "%s - Meta reader" % source.name,
            "source_id": source.id,
            "adapter_key": META_ADAPTER_KEY,
            "purpose": "reader",
            "meta_profile_id": profile.id,
            "state": "ready",
            "health_state": "healthy",
            "verified_at": now,
            "effective_capabilities_json": capabilities,
            "effective_capabilities_hash": capability_hash,
        }
        return (
            connection_model.with_company(profile.company_id)
            .with_context(**context)
            .create(values)
        )

    @api.model
    def _connection_success_values(
        self,
        connection,
        profile,
        capabilities,
        capability_hash,
        verified_at,
    ):
        values = {"verified_at": verified_at}
        expected = {
            "profile_public_ref": profile.public_ref,
            "profile_revision": profile.profile_revision,
            "health_state": "healthy",
            "last_health_error_class": False,
            "last_health_error_message": False,
        }
        for field_name, value in expected.items():
            if connection[field_name] != value:
                values[field_name] = value
        recover_paused = bool(
            connection.state == "paused"
            and (
                connection.profile_revision != profile.profile_revision
                or connection.health_state == "unhealthy"
                or connection.last_health_error_class == _META_DISCOVERY_MISSING_ERROR
            )
        )
        if connection.state not in {"paused", "disabled"} or recover_paused:
            if connection.state != "ready":
                values["state"] = "ready"
        if (
            connection.effective_capabilities_hash != capability_hash
            or connection.effective_capabilities_json != capabilities
        ):
            values.update(
                {
                    "effective_capabilities_json": capabilities,
                    "effective_capabilities_hash": capability_hash,
                }
            )
        return values

    @api.model
    def _unix_datetime(self, value):
        if not value:
            return False
        try:
            return datetime.datetime.utcfromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            raise ValidationError(_("The Meta token expiry is invalid.")) from None
