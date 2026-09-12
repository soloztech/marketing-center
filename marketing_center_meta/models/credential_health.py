import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.tokens import MARKETING_META_PROFILE_RUNTIME_TOKEN


class MarketingMetaCredentialHealth(models.Model):
    _inherit = "marketing.center.meta.profile"

    next_health_check_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_health_check_at = fields.Datetime(readonly=True, copy=False)
    health_check_failures = fields.Integer(readonly=True, copy=False)
    last_error_http_status = fields.Integer(readonly=True, copy=False)
    last_error_provider_code = fields.Integer(readonly=True, copy=False)
    last_error_provider_subcode = fields.Integer(readonly=True, copy=False)
    last_error_trace_id = fields.Char(readonly=True, copy=False, size=128)
    last_error_retry_seconds = fields.Integer(readonly=True, copy=False)
    last_usage_call_percent = fields.Float(readonly=True, copy=False)
    last_usage_cpu_percent = fields.Float(readonly=True, copy=False)
    last_usage_time_percent = fields.Float(readonly=True, copy=False)
    last_estimated_cooldown_seconds = fields.Integer(readonly=True, copy=False)
    credential_alert = fields.Selection(
        [
            ("none", "No alert"),
            ("review", "Validation needs attention"),
            ("expiring", "Credential expires within 7 days"),
            ("expired", "Credential expired"),
        ],
        compute="_compute_credential_alert",
    )

    @api.model
    def _runtime_fields(self):
        return super()._runtime_fields() | set(self._credential_health_reset_values())

    @api.model
    def _credential_health_reset_values(self):
        return {
            "next_health_check_at": False,
            "last_health_check_at": False,
            "health_check_failures": 0,
            **self._credential_diagnostics_values(),
        }

    @api.model
    def _credential_diagnostics_values(self, error=None):
        # Revalidate attributes even for a custom adapter subclass. Only these
        # typed values cross from the API error into persistent profile state.
        safe = MetaApiError(
            "diagnostic",
            **{
                name: getattr(error, name, 0)
                for name in (
                    "http_status",
                    "provider_code",
                    "provider_subcode",
                    "provider_trace_id",
                    "retry_after_seconds",
                    "usage_call_count_percent",
                    "usage_cpu_percent",
                    "usage_time_percent",
                    "estimated_cooldown_seconds",
                )
            },
        )
        return {
            "last_error_http_status": safe.http_status,
            "last_error_provider_code": safe.provider_code,
            "last_error_provider_subcode": safe.provider_subcode,
            "last_error_trace_id": safe.provider_trace_id or False,
            "last_error_retry_seconds": safe.retry_after_seconds,
            "last_usage_call_percent": safe.usage_call_count_percent,
            "last_usage_cpu_percent": safe.usage_cpu_percent,
            "last_usage_time_percent": safe.usage_time_percent,
            "last_estimated_cooldown_seconds": safe.estimated_cooldown_seconds,
        }

    @api.depends(
        "active",
        "health_state",
        "verified_at",
        "token_expires_at",
        "data_access_expires_at",
    )
    def _compute_credential_alert(self):
        now = fields.Datetime.now()
        for profile in self:
            expiries = [
                value
                for value in (profile.token_expires_at, profile.data_access_expires_at)
                if value
            ]
            expires = min(expiries) if expiries else None
            if not profile.active:
                alert = "none"
            elif expires and expires <= now:
                alert = "expired"
            elif expires and expires <= now + datetime.timedelta(days=7):
                alert = "expiring"
            elif profile.health_state != "healthy" or not profile.verified_at:
                alert = "review"
            else:
                alert = "none"
            profile.credential_alert = alert

    def _credential_health_success_values(self, now, validation):
        self.ensure_one()
        expires = [
            stamp
            for stamp in (validation.expires_at, validation.data_access_expires_at)
            if stamp
        ]
        near_expiry = bool(
            expires
            and min(expires)
            <= (now + datetime.timedelta(days=7))
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
        hours = 6 if near_expiry else 24
        return {
            **self._credential_diagnostics_values(),
            "last_health_check_at": now,
            "health_check_failures": 0,
            "next_health_check_at": now
            + datetime.timedelta(hours=hours, seconds=self.id % 300),
        }

    def _credential_health_failure_values(self, error):
        self.ensure_one()
        now = fields.Datetime.now()
        failures = min(self.health_check_failures + 1, 8)
        diagnostics = self._credential_diagnostics_values(error)
        seconds = max(
            min(3600 * 2 ** (failures - 1), 86_400),
            diagnostics["last_error_retry_seconds"],
        )
        return {
            **diagnostics,
            "last_health_check_at": now,
            "health_check_failures": failures,
            "next_health_check_at": now + datetime.timedelta(seconds=seconds),
        }

    @api.model
    def _cron_enqueue_credential_health(self, limit=50, now=None):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValidationError(_("The credential health batch size is invalid."))
        now = fields.Datetime.to_datetime(now) if now else fields.Datetime.now()
        candidates = self.sudo().search(
            [
                ("company_id", "in", self.env.companies.ids),
                ("active", "=", True),
                ("reader_kind", "=", "ads_reader"),
                ("meta_app_id.active", "=", True),
                "|",
                ("next_health_check_at", "=", False),
                ("next_health_check_at", "<=", now),
            ],
            order="next_health_check_at asc, id",
            limit=limit,
        )
        queued = skipped = 0
        for profile in candidates:
            self.env.cr.execute(
                "SELECT id FROM marketing_center_meta_profile "
                "WHERE id = %s FOR UPDATE SKIP LOCKED",
                [profile.id],
            )
            if not self.env.cr.fetchone():
                skipped += 1
                continue
            profile.invalidate_recordset(
                ["next_health_check_at", "profile_revision", "meta_app_id"]
            )
            profile = profile.with_company(profile.company_id).with_context(
                allowed_company_ids=profile.company_id.ids
            )
            if profile.next_health_check_at and profile.next_health_check_at > now:
                skipped += 1
                continue
            identity = profile._validation_job_identity()
            active_job = (
                self.env["queue.job"]
                .sudo()
                .search_count(
                    [
                        ("identity_key", "=", identity),
                        ("model_name", "=", profile._name),
                        (
                            "state",
                            "in",
                            ("pending", "enqueued", "started", "wait_dependencies"),
                        ),
                    ]
                )
            )
            # Reserve the next scheduler slot even when a slow job is already
            # active. Job completion sets the normal interval or failure backoff.
            profile.with_context(
                marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
            ).write(
                {
                    "next_health_check_at": now + datetime.timedelta(hours=1),
                }
            )
            if active_job:
                skipped += 1
                continue
            profile._enqueue_validation(
                eta=now + datetime.timedelta(seconds=profile.id % 300)
            )
            queued += 1
        return {"queued": queued, "skipped": skipped}
