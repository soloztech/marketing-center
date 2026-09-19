"""One acquisition snapshot on the native lead; no parallel session ledger."""
import datetime
import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from psycopg2.errors import DeadlockDetected, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)
from odoo.addons.marketing_center_website.models.consent import CONSENT_CONTEXT_TOKEN

from .tokens import WEBSITE_NATIVE_SUBMISSION_TOKEN as _TOKEN

_QUERY_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "gad_campaignid",
    "gad_source",
)
_CLICK_FIELDS = {"gclid", "gbraid", "wbraid", "fbclid"}
_TECHNICAL = ("/web", "/website", "/marketing", "/my", "/portal", "/auth")
_logger = logging.getLogger(__name__)


def safe_page(value, origin):
    """Keep only an exact same-origin public URL, without its query or fragment."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or urlunsplit((parsed.scheme, parsed.netloc, "", "", "")) != origin
            or len(value) > 8192
        ):
            return ""
        path = unquote(parsed.path or "/").lower()
        if any(
            path == prefix or path.startswith(prefix + "/") for prefix in _TECHNICAL
        ):
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return ""


def acquisition_values(url):
    try:
        query = parse_qs(urlsplit(url).query, max_num_fields=100)
    except (TypeError, ValueError):
        return {}
    values = {}
    for key in _QUERY_FIELDS:
        candidates = query.get(key, [])
        if len(candidates) != 1:
            continue
        value = candidates[0].strip()
        if not value or len(value) > 512 or re.search(r"[\x00-\x1f\x7f]", value):
            continue
        if key in _CLICK_FIELDS and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,511}", value
        ):
            continue
        if key in {"gad_campaignid", "gad_source"} and not re.fullmatch(
            r"[0-9]{1,32}", value
        ):
            continue
        values[key] = value
    return values


def utc_iso(value):
    return value.replace(tzinfo=datetime.timezone.utc, microsecond=0).isoformat()


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_native_website_id = fields.Many2one(
        "website", readonly=True, copy=False, ondelete="restrict"
    )
    marketing_native_event_id = fields.Char(
        readonly=True, copy=False, index=True, size=36
    )
    marketing_native_request_hash = fields.Char(
        readonly=True,
        copy=False,
        size=64,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    marketing_native_snapshot = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    marketing_native_capture_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("done", "Captured"),
            ("skipped", "Unavailable"),
            ("error", "Capture failed"),
        ],
        readonly=True,
        copy=False,
    )
    marketing_native_capture_reason = fields.Char(readonly=True, copy=False, size=128)

    _sql_constraints = [
        (
            "native_website_submission_unique",
            "unique(marketing_native_website_id, marketing_native_event_id)",
            "This Website submission already created a CRM lead.",
        )
    ]

    def write(self, values):
        immutable = {
            "marketing_native_website_id",
            "marketing_native_event_id",
            "marketing_native_request_hash",
            "marketing_native_snapshot",
        }
        if (
            immutable.intersection(values)
            and self.env.context.get("marketing_native_submission_token") is not _TOKEN
        ):
            raise AccessError(_("Native acquisition snapshots cannot be rewritten."))
        return super().write(values)

    def action_retry_native_acquisition(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only marketing administrators can retry acquisition."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        self.filtered(
            lambda lead: lead.marketing_native_snapshot
            and lead.marketing_native_capture_state != "done"
        ).write(
            {
                "marketing_native_capture_state": "pending",
                "marketing_native_capture_reason": False,
            }
        )
        return True


class MarketingWebsiteCrmService(models.AbstractModel):
    _inherit = "marketing.website.crm.service"

    @api.model
    def _native_utm_values(self, snapshot, submitted):
        if not snapshot:
            return {}
        payload = (snapshot or {}).get("payload", {})
        lead_model = self.env["crm.lead"].sudo()
        values = {}
        for suffix, field_name in (
            ("campaign", "campaign_id"),
            ("source", "source_id"),
            ("medium", "medium_id"),
        ):
            if field_name in submitted:
                continue
            value = payload.get("utm_" + suffix)
            if value:
                values[field_name] = lead_model._find_or_create_record(
                    lead_model._fields[field_name].comodel_name, value
                ).id
            else:
                # Explicit empty defaults stop utm.mixin from mixing an older
                # cookie campaign with the frozen acquisition of this form.
                values[field_name] = (
                    self.env.ref("utm.utm_medium_website").id
                    if field_name == "medium_id"
                    else False
                )
        return values

    @api.model
    def _mark_native_utm_defaults(self, lead, assigned, submitted):
        """Allow catalog completion of a partial tuple captured at insertion."""
        fields_utm = {"campaign_id", "source_id", "medium_id"}
        if not assigned or fields_utm.intersection(submitted):
            return
        lead._native_utm_lock()
        if lead.campaign_id or lead.marketing_utm_manual:
            return
        current = lead._native_utm_values()
        expected = {
            "campaign_id": False,
            "source_id": False,
            "medium_id": self.env.ref("utm.utm_medium_website").id,
            **assigned,
        }
        # Native direct creates can also have an empty medium. An unrelated
        # cookie/manual value must never gain automatic ownership here.
        if not current["medium_id"] and "medium_id" not in assigned:
            expected["medium_id"] = False
        if current == expected:
            lead._native_utm_write({"marketing_utm_default_json": current})

    @api.model
    def _native_existing_submission(self, website, event_id, request_hash):
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_native_form:%s:%s" % (website.id, event_id),
            "Concurrent native Website submission requires a fresh transaction",
        )
        lead = (
            self.env["crm.lead"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("marketing_native_website_id", "=", website.id),
                    ("marketing_native_event_id", "=", event_id),
                ],
                limit=1,
            )
        )
        if lead and lead.marketing_native_request_hash != request_hash:
            raise ValidationError(
                _("This submission reference was already used for different form data.")
            )
        return lead

    @api.model
    def _native_snapshot(
        self,
        website,
        binding,
        event_id,
        origin,
        referrer,
        submitted_at,
        visitor=None,
        cookies=None,
    ):
        """Choose once at submission. Neither retries nor later visits reselect it."""
        if (
            binding.capture_mode != "native"
            or not binding.active
            or binding.company_id != website.company_id
            or not binding.endpoint_id._capture_policy_allows()
        ):
            return {}
        form_url = safe_page(referrer, origin)
        if not form_url:
            return {}
        actions = (
            self.env["marketing.website.action"]
            .sudo()
            .search(
                [
                    ("binding_id", "=", binding.id),
                    ("active", "=", True),
                    ("kind", "=", "form_submission"),
                    ("form_model_name", "=", "crm.lead"),
                    ("source_path", "=", urlsplit(form_url).path),
                ],
                limit=2,
            )
        )
        if len(actions) != 1:
            return {}
        action = actions
        chosen_url = form_url
        values = acquisition_values(referrer)
        track_id = False
        acquired_at = None
        # Bound historical inference to the preceding 24h and to the actual form
        # page. A visitor may span many days or tabs and is not a session token.
        if visitor and visitor.exists():
            tracks = (
                self.env["website.track"]
                .sudo()
                .search(
                    [
                        ("visitor_id", "=", visitor.id),
                        ("visit_datetime", "<=", submitted_at),
                        (
                            "visit_datetime",
                            ">=",
                            submitted_at - datetime.timedelta(hours=24),
                        ),
                    ],
                    order="visit_datetime desc, id desc",
                    limit=200,
                )
            )
            tracks = tracks.filtered(
                lambda t: safe_page(t.url, origin)
                and (
                    not t.page_id
                    or not t.page_id.website_id
                    or t.page_id.website_id == website
                )
            )
            anchor = next(
                (
                    t
                    for t in tracks
                    if safe_page(t.url, origin) == form_url
                    and (not values or acquisition_values(t.url) == values)
                ),
                None,
            )
            if anchor:
                # Use the page's recorded time even if its Referer carries the
                # click. Otherwise a next-day form invents a next-day click.
                acquired_at = anchor.visit_datetime
                track_id = anchor.id
                if not values:
                    candidates = tracks.filtered(
                        lambda t: (t.visit_datetime, t.id)
                        <= (anchor.visit_datetime, anchor.id)
                    )
                    chosen = next(
                        (t for t in candidates if acquisition_values(t.url)), None
                    )
                    if chosen:
                        values = acquisition_values(chosen.url)
                        chosen_url = safe_page(chosen.url, origin)
                        acquired_at, track_id = chosen.visit_datetime, chosen.id
        # Native cookie UTMs remain a fallback; never mix their campaign with a
        # different track's click. Cookie-only acquisition has no known click date.
        if not values:
            for name in ("source", "medium", "campaign"):
                value = unquote((cookies or {}).get("odoo_utm_" + name, ""))
                if (
                    value
                    and len(value) <= 512
                    and not re.search(r"[\x00-\x1f\x7f]", value)
                ):
                    values["utm_" + name] = value
        payload = {
            "event_id": event_id,
            "event_type": "form_submission",
            "occurred_at": utc_iso(submitted_at),
            "landing_url": chosen_url,
            "consent_state": "unknown",
            "action_ref": action.public_ref,
            "route_ref": action.route_ref,
            "model_ref": "crm.lead",
            **values,
        }
        if acquired_at:
            payload["acquisition_at"] = utc_iso(acquired_at)
        consent = (
            self.env["marketing.website.consent"]._current(binding.endpoint_id)
            if binding.endpoint_id._requires_individual_consent()
            else False
        )
        return {
            "payload": payload,
            "action_id": action.id,
            "endpoint_id": binding.endpoint_id.id,
            "origin": origin,
            "track_id": track_id,
            "form_url": form_url,
            "consent_id": consent.id if consent else False,
        }

    @api.model
    def _capture_native_submission(self, lead):
        lead.ensure_one()
        if lead.marketing_native_capture_state == "done":
            return True
        snapshot = lead.marketing_native_snapshot
        if not isinstance(snapshot, dict) or not snapshot.get("payload"):
            return False
        if snapshot.get("consent_id"):
            self = self.with_context(
                website_consent_internal=CONSENT_CONTEXT_TOKEN,
                website_consent_id=snapshot["consent_id"],
            )
            lead = lead.with_env(self.env)
        website = lead.marketing_native_website_id
        action = (
            self.env["marketing.website.action"]
            .sudo()
            .browse(snapshot["action_id"])
            .exists()
        )
        if (
            not action
            or action.website_id != website
            or action.company_id != lead.company_id
            or action.binding_id.capture_mode != "native"
            or action.binding_id.endpoint_id.id != snapshot["endpoint_id"]
        ):
            raise AccessError(_("The native acquisition configuration changed."))
        endpoint = self.env[
            "marketing.website.action.service"
        ]._lock_effective_configuration(action)
        if (
            not action.active
            or not action.binding_id.active
            or action.binding_id.capture_mode != "native"
        ):
            raise AccessError(_("The native acquisition configuration is unavailable."))
        payload = snapshot["payload"]
        result = self.env["marketing.web.ingress.service"]._ingest_payload(
            endpoint,
            payload,
            origin=snapshot["origin"],
            # The frozen submission timestamp makes an immediate HTTP replay
            # deterministic; delayed manual capture still uses the endpoint window.
            ingress_provenance="website_confirmed_action",
        )
        if result.disposition not in {"accepted", "duplicate"}:
            raise ValidationError(
                _("The native acquisition conflicts with an existing submission.")
            )
        event = (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search(
                [
                    ("public_ref", "=", result.event_ref),
                    ("endpoint_id", "=", endpoint.id),
                ],
                limit=1,
            )
        )
        self._link_event(action, event, event.touchpoint_id, lead)
        # The ingress vault now owns raw click values. The CRM snapshot keeps
        # context and a reference, without a second durable copy of those IDs.
        snapshot = dict(
            snapshot,
            event_ref=event.public_ref,
            payload={
                key: value for key, value in payload.items() if key not in _CLICK_FIELDS
            },
        )
        lead.with_context(marketing_native_submission_token=_TOKEN).write(
            {
                "marketing_native_capture_state": "done",
                "marketing_native_capture_reason": False,
                "marketing_native_snapshot": snapshot,
            }
        )
        return True

    @api.model
    def _cron_recover_native_submissions(self, now=None, limit=50):
        """Bounded recovery using the already scheduled Website CRM cron."""
        now = now or fields.Datetime.now()
        leads = (
            self.env["crm.lead"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "in", self.env.companies.ids),
                    ("marketing_native_capture_state", "in", ["pending", "error"]),
                    ("marketing_native_snapshot", "!=", False),
                ],
                order="write_date, id",
                limit=min(max(int(limit), 1), 100),
            )
        )
        for lead in leads:
            scoped = (
                self.sudo()
                .with_company(lead.company_id)
                .with_context(allowed_company_ids=[lead.company_id.id])
            )
            snapshot = lead.marketing_native_snapshot
            endpoint = (
                self.env["marketing.web.ingress.endpoint"]
                .sudo()
                .browse(snapshot.get("endpoint_id"))
                .exists()
            )
            try:
                occurred = datetime.datetime.fromisoformat(
                    snapshot["payload"]["occurred_at"]
                ).replace(tzinfo=None)
                if (
                    not endpoint
                    or (now - occurred).total_seconds() > endpoint.replay_window_seconds
                ):
                    lead.write(
                        {
                            "marketing_native_capture_state": "skipped",
                            "marketing_native_capture_reason": "capture_window_expired",
                        }
                    )
                    continue
                with self.env.cr.savepoint():
                    scoped._capture_native_submission(lead.with_env(scoped.env))
            except (SerializationFailure, DeadlockDetected):
                raise
            except (AccessError, ValidationError, KeyError, ValueError) as error:
                lead.write(
                    {
                        "marketing_native_capture_state": "skipped",
                        "marketing_native_capture_reason": type(error).__name__,
                    }
                )
            except Exception as error:
                _logger.warning(
                    "Native acquisition recovery failed for CRM %s (%s)",
                    lead.id,
                    type(error).__name__,
                )
                lead.write(
                    {
                        "marketing_native_capture_state": "error",
                        "marketing_native_capture_reason": type(error).__name__,
                    }
                )
        return len(leads)
