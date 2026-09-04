import datetime
import hashlib
import json
import logging

from psycopg2 import Error as PsycopgError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY

from odoo.addons.marketing_center_web_ingress.services.errors import (
    WebIngressSerializationFailure,
)

from .tokens import WEBSITE_CRM_WRITE_TOKEN

_logger = logging.getLogger(__name__)
_MAX_NATIVE_RESULT_BYTES = 4096
_MAX_ATTEMPTS = 8
_RETRY_SECONDS = (10, 30, 60, 120, 300, 600, 1200, 3600)
_SESSION_AUTHORITY = "website.session.correlation.v1"
_SESSION_LOOKBACK = datetime.timedelta(hours=24)
_SESSION_LINK_LIMIT = 64


class MarketingWebsiteCrmService(models.AbstractModel):
    _name = "marketing.website.crm.service"
    _description = "Website Form CRM Correlation Service"

    @api.model
    def _native_result(self, result):
        if not isinstance(result, str) or len(result.encode("utf-8")) > (
            _MAX_NATIVE_RESULT_BYTES
        ):
            return 0, ""
        try:
            payload = json.loads(result)
        except (TypeError, ValueError, RecursionError):
            return 0, ""
        if not isinstance(payload, dict):
            return 0, ""
        record_id = payload.get("id")
        receipt = payload.get("marketing_center_receipt")
        if (
            isinstance(record_id, bool)
            or not isinstance(record_id, int)
            or record_id <= 0
            or not isinstance(receipt, str)
        ):
            return 0, ""
        return record_id, receipt

    @api.model
    def _capture_native_form_intent(
        self, website, model_name, claim, origin, native_result
    ):
        """Validate the signed native result, persist intent, then process it."""
        if model_name != "crm.lead":
            raise ValidationError(_("Only CRM lead Website forms can be correlated."))
        record_id, receipt = self._native_result(native_result)
        if not record_id:
            return self.env["marketing.website.crm.intent"]
        company = website.company_id
        action_service = (
            self.env["marketing.website.action.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        payload = dict(claim or {})
        payload["receipt"] = receipt
        action, values, occurred_at = action_service._validate_form_receipt(
            website, payload, origin
        )
        if action.form_model_name != "crm.lead":
            raise ValidationError(_("The Website form action is not a CRM lead."))
        lead = self.env["crm.lead"].sudo().browse(record_id).exists()
        if not lead or len(lead) != 1:
            raise ValidationError(_("The Website form CRM lead is unavailable."))
        lead_company = lead.marketing_event_company_id or lead.company_id
        if lead_company and lead_company != company:
            raise ValidationError(
                _("The Website form CRM lead belongs to another company.")
            )
        intent = self._get_or_create_intent(
            website,
            action,
            lead,
            values["event_id"],
            values["session_ref"],
            origin,
            occurred_at,
        )
        self._attempt_intent(intent)
        intent.invalidate_recordset(["state"])
        if intent.state == "retry":
            try:
                intent._enqueue()
            except PsycopgError:
                raise
            except Exception as error:
                _logger.error(
                    "Website CRM intent enqueue failed error_class=%s",
                    type(error).__name__,
                )
        return intent

    @api.model
    def _get_or_create_intent(
        self, website, action, lead, event_ref, session_ref, origin, occurred_at
    ):
        company = website.company_id
        endpoint = action.binding_id.endpoint_id
        session_hash = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
        reconcile_until = occurred_at + datetime.timedelta(
            seconds=endpoint.replay_window_seconds
        )
        lock_key = "marketing_website_crm_intent:%s:%s" % (endpoint.id, event_ref)
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        if not self.env.cr.fetchone()[0]:
            raise WebIngressSerializationFailure(
                "Concurrent Website CRM intent capture"
            )
        Intent = self.env["marketing.website.crm.intent"].sudo()
        intent = Intent.search(
            [
                ("endpoint_id", "=", endpoint.id),
                ("event_ref", "=", event_ref),
            ],
            limit=1,
        )
        if intent:
            expected = (
                intent.website_id == website
                and intent.action_id == action
                and intent.endpoint_id == action.binding_id.endpoint_id
                and intent.lead_id == lead
                and intent.session_ref == session_ref
                and intent.session_hash == session_hash
                and intent.origin == origin
                and intent.occurred_at == occurred_at
                and intent.session_reconcile_until == reconcile_until
            )
            if not expected:
                raise ValidationError(
                    _("The Website form event was already claimed differently.")
                )
            return intent
        return (
            Intent.with_company(company)
            .with_context(marketing_website_crm_write_token=WEBSITE_CRM_WRITE_TOKEN)
            .create(
                {
                    "company_id": company.id,
                    "website_id": website.id,
                    "action_id": action.id,
                    "endpoint_id": action.binding_id.endpoint_id.id,
                    "lead_id": lead.id,
                    **self.env["marketing.crm.service"]._lead_snapshot_values(lead),
                    "event_ref": event_ref,
                    "session_ref": session_ref,
                    "session_hash": session_hash,
                    "origin": origin,
                    "occurred_at": occurred_at,
                    "session_reconcile_until": reconcile_until,
                }
            )
        )

    @api.model
    def _attempt_intent(self, intent):
        intent = intent.sudo().exists()
        if not intent or intent.state in {"done", "failed", "tombstoned"}:
            return True
        self.env.cr.execute(
            "SELECT id FROM marketing_website_crm_intent WHERE id = %s FOR UPDATE",
            [intent.id],
        )
        intent.invalidate_recordset(
            ["state", "attempts", "correlation_id", "queue_job_uuid"]
        )
        if intent.state in {"done", "failed", "tombstoned"}:
            return True
        if not intent.lead_id:
            intent._internal_write(
                {
                    "state": "tombstoned",
                    "next_retry_at": False,
                    "queue_job_uuid": False,
                    "last_error_class": False,
                    "last_error_message": False,
                    "session_reconcile_state": "complete",
                    "session_reconciled_at": fields.Datetime.now(),
                    "next_session_reconcile_at": False,
                }
            )
            return True
        attempt = min(intent.attempts + 1, _MAX_ATTEMPTS)
        intent._internal_write(
            {
                "state": "processing",
                "attempts": attempt,
                "next_retry_at": False,
                "queue_job_uuid": False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        try:
            with self.env.cr.savepoint():
                correlation = self._process_intent(intent)
        except WebIngressSerializationFailure:
            raise
        except PsycopgError as error:
            if error.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                raise
            intent._internal_write(
                {
                    "state": "failed",
                    "next_retry_at": False,
                    "last_error_class": type(error).__name__[:128],
                    "last_error_message": "Terminal Website CRM database failure",
                }
            )
            return False
        except (ValidationError, AccessError) as error:
            intent._internal_write(
                {
                    "state": "failed",
                    "next_retry_at": False,
                    "last_error_class": type(error).__name__[:128],
                    "last_error_message": "Terminal Website CRM contract failure",
                }
            )
            return False
        except Exception as error:
            terminal = attempt >= _MAX_ATTEMPTS
            retry_at = fields.Datetime.now() + datetime.timedelta(
                seconds=_RETRY_SECONDS[attempt - 1]
            )
            intent._internal_write(
                {
                    "state": "failed" if terminal else "retry",
                    "next_retry_at": False if terminal else retry_at,
                    "last_error_class": type(error).__name__[:128],
                    "last_error_message": "Unexpected Website CRM processing failure",
                }
            )
            return False
        intent._internal_write(
            {
                "state": "done",
                "next_retry_at": False,
                "last_error_class": False,
                "last_error_message": False,
                "correlation_id": correlation.id,
                "processed_at": fields.Datetime.now(),
            }
        )
        return True

    @api.model
    def _process_intent(self, intent):
        action = intent.action_id
        if (
            action.website_id != intent.website_id
            or action.form_model_name != "crm.lead"
            or action.binding_id.endpoint_id != intent.endpoint_id
        ):
            raise ValidationError(_("The Website CRM intent configuration changed."))
        result = (
            self.env["marketing.website.action.service"]
            .sudo()
            .with_context(allowed_company_ids=[intent.company_id.id])
            .with_company(intent.company_id)
            ._ingest_action(
                action,
                intent.event_ref,
                intent.session_ref,
                "form_submission",
                intent.origin,
                intent.occurred_at,
            )
        )
        if result.disposition not in ("accepted", "duplicate"):
            raise ValidationError(_("The Website form evidence was not accepted."))
        event = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search(
                [
                    ("public_ref", "=", result.event_ref),
                    ("company_id", "=", intent.company_id.id),
                    ("endpoint_id", "=", intent.endpoint_id.id),
                    ("state", "=", "done"),
                ],
                limit=1,
            )
        )
        touchpoint = event.touchpoint_id
        asset_refs = touchpoint.asset_refs_json or {}
        if (
            not event
            or not touchpoint
            or touchpoint.id != result.touchpoint_id
            or touchpoint.source_system != "web.ingress"
            or touchpoint.touchpoint_type != "form_submission"
            or not isinstance(asset_refs, dict)
            or asset_refs.get("web.action") != action.public_ref
            or asset_refs.get("web.model") != "crm.lead"
        ):
            raise ValidationError(_("The Website form touchpoint is unavailable."))
        correlation = self._link_event(action, event, touchpoint, intent.lead_id)
        self._reconcile_session_now(intent)
        return correlation

    @api.model
    def _reconcile_session_now(self, intent):
        now = fields.Datetime.now()
        try:
            with self.env.cr.savepoint():
                touchpoints = self._session_touchpoints(intent)
                self._append_session_assertions(intent, touchpoints)
        except PsycopgError as error:
            if error.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                raise
            intent._internal_write(
                {
                    "session_reconcile_state": "failed",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": False,
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Terminal Website session database failure"
                    ),
                }
            )
            return False
        except (ValidationError, AccessError) as error:
            intent._internal_write(
                {
                    "session_reconcile_state": "failed",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": False,
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Terminal Website session reconciliation failure"
                    ),
                }
            )
            return False
        except Exception as error:
            _logger.error(
                "Immediate Website session reconciliation failed intent=%s "
                "error_class=%s",
                intent.public_ref,
                type(error).__name__,
            )
            intent._internal_write(
                {
                    "session_reconcile_state": "watching",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": now + datetime.timedelta(minutes=5),
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Transient Website session reconciliation failure"
                    ),
                }
            )
            return False
        intent._internal_write(
            {
                "session_reconcile_state": (
                    "watching" if intent.session_reconcile_until >= now else "complete"
                ),
                "session_reconciled_at": now,
                "next_session_reconcile_at": (
                    now + datetime.timedelta(minutes=1)
                    if intent.session_reconcile_until >= now
                    else False
                ),
                "session_reconcile_error_class": False,
                "session_reconcile_error_message": False,
            }
        )
        return True

    @api.model
    def _session_touchpoints(self, intent):
        """Resolve bounded, effective pre-form evidence for this exact session."""
        start = intent.occurred_at - _SESSION_LOOKBACK
        scope_ref = "endpoint:%s" % intent.endpoint_id.public_ref
        self.env.cr.execute(
            """
            SELECT effective.touchpoint_id
              FROM marketing_web_ingress_event AS event
              JOIN marketing_attribution_touchpoint AS original
                ON original.id = event.touchpoint_id
              JOIN marketing_attribution_effective_touchpoint AS effective
                ON effective.company_id = original.company_id
               AND effective.canonical_key = original.canonical_key
              JOIN marketing_attribution_identifier AS identifier
                ON identifier.touchpoint_id = effective.touchpoint_id
             WHERE event.company_id = %s
               AND event.endpoint_id = %s
               AND event.origin = %s
               AND event.state = 'done'
               AND event.occurred_at >= %s
               AND event.occurred_at <= %s
               AND effective.occurred_at >= %s
               AND effective.occurred_at <= %s
               AND effective.source_system = 'web.ingress'
               AND effective.source_scope_ref = %s
               AND effective.touchpoint_type IN ('entry_point', 'organic_link')
               AND identifier.namespace = 'web.session'
               AND identifier.role = 'session'
               AND identifier.comparison_hash = %s
             GROUP BY effective.touchpoint_id, effective.occurred_at
             ORDER BY effective.occurred_at, effective.touchpoint_id
             LIMIT %s
            """,
            [
                intent.company_id.id,
                intent.endpoint_id.id,
                intent.origin,
                start,
                intent.occurred_at,
                start,
                intent.occurred_at,
                scope_ref,
                intent.session_hash,
                _SESSION_LINK_LIMIT + 1,
            ],
        )
        touchpoint_ids = [row[0] for row in self.env.cr.fetchall()]
        if len(touchpoint_ids) > _SESSION_LINK_LIMIT:
            raise ValidationError(
                _("The Website session has too many eligible touchpoints.")
            )
        return (
            self.env["marketing.attribution.touchpoint"].sudo().browse(touchpoint_ids)
        )

    @api.model
    def _session_authority_ref(self, intent):
        return "website.form.intent:%s" % intent.public_ref

    @api.model
    def _session_assertion_ref(self, intent, touchpoint):
        material = "%s|%s|%s|%s" % (
            _SESSION_AUTHORITY,
            intent.company_id.id,
            intent.public_ref,
            touchpoint.canonical_key,
        )
        return (
            "website.session:%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()
        )

    @api.model
    def _append_session_assertions(self, intent, touchpoints):
        if not intent.lead_id:
            return self.env["marketing.attribution.crm.link"]
        authority_ref = self._session_authority_ref(intent)
        crm_service = (
            self.env["marketing.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[intent.company_id.id])
            .with_company(intent.company_id)
        )
        assertions = self.env["marketing.attribution.crm.link"]
        for touchpoint in touchpoints:
            assertions |= crm_service._link_touchpoint_lead(
                touchpoint,
                intent.lead_id,
                source_ref=authority_ref,
                authority_key=_SESSION_AUTHORITY,
                authority_ref=authority_ref,
                assertion_ref=self._session_assertion_ref(intent, touchpoint),
            )
        return assertions

    @api.model
    def _recover_session_intent(self, intent, now=None, final=False):
        now = now or fields.Datetime.now()
        intent = intent.sudo().exists()
        if (
            not intent
            or intent.state != "done"
            or intent.session_reconcile_state not in {"pending", "watching"}
            or (intent.session_reconcile_until < now and not final)
        ):
            return False
        if not intent.lead_id:
            intent._internal_write(
                {
                    "session_reconcile_state": "complete",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": False,
                    "session_reconcile_error_class": False,
                    "session_reconcile_error_message": False,
                }
            )
            return True
        try:
            with self.env.cr.savepoint():
                touchpoints = self._session_touchpoints(intent)
                self._append_session_assertions(intent, touchpoints)
        except PsycopgError as error:
            if error.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                raise
            intent._internal_write(
                {
                    "session_reconcile_state": "failed",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": False,
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Terminal Website session database failure"
                    ),
                }
            )
            return False
        except (ValidationError, AccessError) as error:
            intent._internal_write(
                {
                    "session_reconcile_state": "failed",
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Terminal Website session reconciliation failure"
                    ),
                    "next_session_reconcile_at": False,
                }
            )
            return False
        except Exception as error:
            _logger.error(
                "Website session reconciliation failed intent=%s error_class=%s",
                intent.public_ref,
                type(error).__name__,
            )
            intent._internal_write(
                {
                    "session_reconcile_state": "failed" if final else "watching",
                    "session_reconciled_at": now,
                    "next_session_reconcile_at": (
                        False if final else now + datetime.timedelta(minutes=5)
                    ),
                    "session_reconcile_error_class": type(error).__name__[:128],
                    "session_reconcile_error_message": (
                        "Terminal Website session reconciliation failure"
                        if final
                        else "Transient Website session reconciliation failure"
                    ),
                }
            )
            return False
        intent._internal_write(
            {
                "session_reconcile_state": "complete" if final else "watching",
                "session_reconciled_at": now,
                "next_session_reconcile_at": (
                    False if final else now + datetime.timedelta(minutes=1)
                ),
                "session_reconcile_error_class": False,
                "session_reconcile_error_message": False,
            }
        )
        return True

    @api.model
    def _revoke_session_assertions(self, intent, revocation_ref, reason):
        intent = intent.sudo().exists()
        if not intent:
            return self.env["marketing.attribution.crm.revocation"]
        authority_ref = self._session_authority_ref(intent)
        crm_service = (
            self.env["marketing.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[intent.company_id.id])
            .with_company(intent.company_id)
        )
        assertions = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .with_company(intent.company_id)
            .search(
                [
                    ("company_id", "=", intent.company_id.id),
                    ("lead_model", "=", intent.lead_model),
                    ("lead_res_id", "=", intent.lead_res_id),
                    ("authority_key", "=", _SESSION_AUTHORITY),
                    ("authority_ref", "=", authority_ref),
                    ("revocation_ids", "=", False),
                ],
                order="id",
            )
        )
        return crm_service._revoke_attribution_assertions(
            assertions,
            _SESSION_AUTHORITY,
            authority_ref,
            revocation_ref,
            reason,
        )

    @api.model
    def _link_event(self, action, event, touchpoint, lead):
        company = action.company_id
        lock_key = "marketing_website_crm:%s:%s" % (company.id, event.id)
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        if not self.env.cr.fetchone()[0]:
            raise WebIngressSerializationFailure(
                "Concurrent Website CRM evidence correlation"
            )
        Correlation = self.env["marketing.website.crm.correlation"].sudo()
        existing = Correlation.search(
            [("company_id", "=", company.id), ("event_id", "=", event.id)],
            limit=1,
        )
        if existing:
            if (
                existing.action_id != action
                or existing.touchpoint_id != touchpoint
                or existing.lead_id != lead
            ):
                raise ValidationError(
                    _("The Website form event is already linked to another CRM lead.")
                )
            return existing
        authority_ref = "website.form.event:%s" % event.public_ref
        assertion = (
            self.env["marketing.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
            ._link_touchpoint_lead(
                touchpoint,
                lead,
                source_ref=authority_ref,
                authority_key="website.form",
                authority_ref=authority_ref,
                assertion_ref=authority_ref,
            )
        )
        return (
            Correlation.with_company(company)
            .with_context(marketing_website_crm_write_token=WEBSITE_CRM_WRITE_TOKEN)
            .create(
                {
                    "company_id": company.id,
                    "event_id": event.id,
                    "action_id": action.id,
                    "touchpoint_id": touchpoint.id,
                    "lead_id": lead.id,
                    **self.env["marketing.crm.service"]._lead_snapshot_values(lead),
                    "assertion_id": assertion.id,
                }
            )
        )
