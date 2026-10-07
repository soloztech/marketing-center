"""Frozen acquisition and its lifetime; no identity inferred from a network."""
# Keep optional feature extensions separate in the cooperative ORM chain.
# pylint: disable=consider-merging-classes-inherited


import datetime
import hashlib
import logging
import secrets

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_base.models.attribution import (
    ATTRIBUTION_ERASURE_TOKEN,
)
from odoo.addons.marketing_center_base.services import (
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    PrivacySnapshotDTO,
)
from odoo.addons.marketing_center_website.models.consent import CONSENT_CONTEXT_TOKEN

from .handoff import ADMIN

_logger = logging.getLogger(__name__)
ERASURE_TOKEN = object()
VISITOR_MERGE_TOKEN = object()
CAPTURE_VERSION = 2


def captured_datetime(value):
    """The server snapshot accepts ISO UTC, never a client time zone guess."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo:
            result = result.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return result
    except ValueError:
        return None


class WebsiteWhatsappHandoff(models.Model):
    _inherit = "marketing.website.whatsapp.handoff"

    visitor_id = fields.Many2one(index=True)

    capture_version = fields.Integer(readonly=True, copy=False, index=True)
    capture_endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint", readonly=True, ondelete="restrict"
    )
    capture_consent_id = fields.Many2one(
        "marketing.website.consent", readonly=True, ondelete="restrict", groups=ADMIN
    )
    capture_privacy_json = fields.Json(readonly=True, groups=ADMIN)
    capture_config_json = fields.Json(readonly=True, groups=ADMIN)
    retain_until = fields.Datetime(readonly=True, index=True)
    retention_manual = fields.Boolean(readonly=True)
    retention_failures = fields.Integer(
        default=0, required=True, readonly=True, copy=False, index=True, groups=ADMIN
    )
    erased_at = fields.Datetime(readonly=True, index=True)
    visit_at = fields.Datetime(readonly=True)
    acquisition_provenance = fields.Selection(
        [(key, key) for key in ("referrer", "track", "cookie", "none", "legacy")],
        readonly=True,
    )
    journey_touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        readonly=True,
        ondelete="restrict",
        groups=ADMIN,
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_service()
        prepared = []
        for incoming in vals_list:
            values = dict(incoming)
            # Existing database rows keep NULL/0. Only admissions after this
            # release get a policy snapshot; no migration invents an old grant.
            if values.get("capture_version", CAPTURE_VERSION) == CAPTURE_VERSION:
                action = (
                    self.env["marketing.website.action"]
                    .sudo()
                    .browse(values["action_id"])
                )
                endpoint = action.binding_id.endpoint_id
                when = (
                    fields.Datetime.to_datetime(values.get("clicked_at"))
                    or fields.Datetime.now()
                )
                decision = (
                    self.env["marketing.website.consent"].sudo()._current(endpoint)
                    if endpoint._requires_individual_consent()
                    else self.env["marketing.website.consent"]
                )
                track = (
                    self.env["website.track"]
                    .sudo()
                    .browse(values.get("track_id", 0))
                    .exists()
                )
                acquisition = values.get("acquisition_json") or {}
                values.update(
                    capture_version=CAPTURE_VERSION,
                    capture_endpoint_id=endpoint.id,
                    capture_consent_id=decision.id or False,
                    capture_config_json={
                        "config_revision": endpoint.config_revision,
                        "capture_purpose": endpoint.capture_purpose,
                        "website_tracking_policy": endpoint.website_tracking_policy,
                    },
                    capture_privacy_json={
                        "policy_version": endpoint.privacy_policy_version or "",
                        "notice_version": endpoint.privacy_notice_version or "",
                        "legal_basis_code": endpoint.privacy_legal_basis_code or "",
                        "consent_state": "granted" if decision else "unknown",
                        "decision_source": "website_consent" if decision else "",
                        "decided_at": fields.Datetime.to_string(decision.decided_at)
                        if decision
                        else None,
                    },
                    retain_until=endpoint._retention_deadline(when),
                    retention_manual=endpoint.retention_mode == "manual",
                    visit_at=track.visit_datetime if track else False,
                    acquisition_provenance=acquisition.get(
                        "acquisition_provenance", "legacy"
                    ),
                )
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        self._check_service()
        erasure = self.env.context.get("whatsapp_journey_erasure") is ERASURE_TOKEN
        merge = (
            self.env.context.get("whatsapp_journey_visitor_merge")
            is VISITOR_MERGE_TOKEN
        )
        allowed = {"event_refs", "journey_touchpoint_id"}
        if merge:
            allowed.update({"visitor_id", "track_id"})
        if not erasure and set(values) - allowed:
            raise AccessError(_("A captura original do site é imutável."))
        return super().write(values)

    def _acquisition_time(self):
        self.ensure_one()
        acquisition = self.sudo().acquisition_json or {}
        when = captured_datetime(acquisition.get("acquisition_at"))
        return when if when and when <= self.clicked_at else self.clicked_at

    def _journey_legacy_event(self):
        self.ensure_one()
        record = self.sudo()
        return (
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search(
                [
                    ("endpoint_id", "=", record.capture_endpoint_id.id),
                    (
                        "event_key_hash",
                        "=",
                        hashlib.sha256(record.event_id.encode()).hexdigest(),
                    ),
                ],
                limit=1,
            )
        )

    def _journey_privacy_status(self, *, first_projection=False):
        self.ensure_one()
        record = self.sudo()
        if record.erased_at or (
            not record.retention_manual
            and record.retain_until
            and record.retain_until <= fields.Datetime.now()
        ):
            return "privacy_unavailable"
        point = record.journey_touchpoint_id
        if point and (point.privacy_erased_at or point.consent_state == "denied"):
            return "privacy_unavailable"
        if not first_projection:
            return "ready"
        if record.capture_version != CAPTURE_VERSION:
            return "legacy"
        endpoint = record.capture_endpoint_id
        frozen = record.capture_config_json or {}
        privacy = record.capture_privacy_json or {}
        if any(
            [
                endpoint.config_revision != frozen.get("config_revision"),
                endpoint.capture_purpose != frozen.get("capture_purpose"),
                endpoint.privacy_policy_version != privacy.get("policy_version"),
                endpoint.privacy_notice_version != privacy.get("notice_version"),
            ]
        ):
            return "capture_unavailable"
        scoped = endpoint.with_context(
            website_consent_internal=CONSENT_CONTEXT_TOKEN,
            website_consent_id=record.capture_consent_id.id,
        )
        if not scoped._capture_policy_allows():
            decision = record.capture_consent_id
            if decision and decision.revoked_at:
                return "decision_changed"
            if decision and (
                not decision.granted or decision.expires_at <= fields.Datetime.now()
            ):
                return "privacy_unavailable"
            return "capture_unavailable"
        return "ready"

    def _journey_dto(self):
        """Pure immutable snapshot; no match, business, current policy or clock."""
        self.ensure_one()
        record = self.sudo()
        acquisition = record.acquisition_json or {}
        identifiers = []
        for key, namespace in (
            ("gclid", "google.gclid"),
            ("gbraid", "google.gbraid"),
            ("wbraid", "google.wbraid"),
            ("fbclid", "meta.fbclid"),
        ):
            value = acquisition.get(key)
            if value:
                identifiers.append(
                    MarketingIdentifierDTO(
                        namespace=namespace,
                        role="click",
                        comparison_hash=hashlib.sha256(value.encode()).hexdigest(),
                        value_ref="",
                        source_field=key,
                        purpose=(record.capture_config_json or {}).get(
                            "capture_purpose", "website_attribution"
                        ),
                        retain_until=record.retain_until.date()
                        if record.retain_until
                        else None,
                    )
                )
        assets = {"website.whatsapp.handoff": str(record.id)}
        if acquisition.get("gad_campaignid"):
            assets.update(
                campaign_id=acquisition["gad_campaignid"], campaign_provider="google"
            )
        return MarketingTouchpointDTO(
            source_system="website.whatsapp",
            source_scope_ref="website:%s" % record.website_id.id,
            source_occurrence_ref="handoff:%s" % record.id,
            source_evidence_ref="website.whatsapp.handoff:%s" % record.id,
            source_schema_version="website.whatsapp.v2",
            mapping_version=2,
            occurred_at=record._acquisition_time(),
            observed_at=record.clicked_at,
            platform="web",
            channel="website",
            touchpoint_type="entry_point",
            evidence_level="first_party",
            landing_url=record.landing_url or record.page_url,
            utm={
                key: acquisition.get("utm_" + key, "")
                for key in ("source", "medium", "campaign", "content", "term")
            },
            asset_refs=assets,
            identifiers=tuple(identifiers),
            privacy=PrivacySnapshotDTO.from_dict(record.capture_privacy_json or {}),
            extensions={
                "website.whatsapp.acquisition_provenance": record.acquisition_provenance
                or "legacy"
            },
        )

    def _erase_journey_values(self, now=None):
        self.ensure_one()
        record = self._service()
        now = now or fields.Datetime.now()
        self.env.cr.execute(
            "SELECT id FROM marketing_website_whatsapp_handoff WHERE id=%s FOR UPDATE",
            [record.id],
        )
        record.invalidate_recordset()
        if record.erased_at or record.capture_version != CAPTURE_VERSION:
            return False
        if record.journey_touchpoint_id:
            record.journey_touchpoint_id._erase_private_values(
                token=ATTRIBUTION_ERASURE_TOKEN, now=now
            )
        record.with_context(whatsapp_journey_erasure=ERASURE_TOKEN).write(
            {
                "erased_at": now,
                "page_url": "about:blank",
                "landing_url": False,
                "acquisition_json": {},
                "visitor_id": False,
                "track_id": False,
                "capture_consent_id": False,
                "session_key": secrets.token_hex(32),
                "acquisition_provenance": "none",
            }
        )
        record.company_id._enqueue_whatsapp_erasure(record)
        return True

    def _record_retention_failure(self):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._service().with_context(
                    whatsapp_journey_erasure=ERASURE_TOKEN
                ).write({"retention_failures": self.retention_failures + 1})
        except OperationalError:
            raise
        except Exception as error:
            _logger.warning(
                "Website WhatsApp retention marker failed (%s)", type(error).__name__
            )

    def _detach_visitor_references(self):
        """Optional cleanup must not prevent native visitor deletion or login."""
        if not self:
            return
        try:
            with self.env.cr.savepoint():
                self._service().with_context(
                    whatsapp_journey_visitor_merge=VISITOR_MERGE_TOKEN
                ).write({"visitor_id": False, "track_id": False})
            return
        except OperationalError:
            raise
        except Exception as error:
            _logger.warning(
                "Website WhatsApp visitor detach failed (%s)", type(error).__name__
            )
        # A failing optional ORM hook must not retain a track moved to another
        # visitor. This private fallback only clears navigation, as native FK
        # SET NULL would; it cannot alter frozen acquisition or grant credit.
        try:
            with self.env.cr.savepoint():
                self.flush_recordset(["visitor_id", "track_id"])
                self.env.cr.execute(
                    "UPDATE marketing_website_whatsapp_handoff "
                    "SET visitor_id=NULL, track_id=NULL WHERE id IN %s",
                    [tuple(self.ids)],
                )
                self.invalidate_recordset(["visitor_id", "track_id"])
                self.modified(["visitor_id", "track_id"])
        except OperationalError:
            raise
        except Exception as error:
            _logger.warning(
                "Website WhatsApp visitor detach fallback failed (%s)",
                type(error).__name__,
            )


class WebsiteVisitor(models.Model):
    _inherit = "website.visitor"

    def unlink(self):
        # PostgreSQL can apply the visitor SET NULL before the track CASCADE:
        # that intermediate handoff still references a track already deleted by
        # the other cascade. Clear both references together before native unlink.
        # During a merge native tracks have already moved to the target; preserve
        # those tracks so _merge_visitor can repoint the handoff afterwards.
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        handoffs = (
            self.env["marketing.website.whatsapp.handoff"]
            .sudo()
            .search(
                [
                    "|",
                    ("visitor_id", "in", self.ids),
                    ("track_id.visitor_id", "in", self.ids),
                ]
            )
            ._service()
            .with_context(whatsapp_journey_visitor_merge=VISITOR_MERGE_TOKEN)
        )
        losing_tracks = handoffs.filtered(lambda row: row.track_id.visitor_id in self)
        if losing_tracks:
            losing_tracks._detach_visitor_references()
        # Let the native visitor FK clear the other rows. During a merge their
        # track already belongs to the target, so an intermediate ORM write
        # visitor=False would violate the handoff's visitor/track constraint.
        return super().unlink()

    def _merge_visitor(self, target):
        self.ensure_one()
        handoffs = (
            self.env["marketing.website.whatsapp.handoff"]
            .sudo()
            .search([("visitor_id", "=", self.id)])
        )
        repoint = handoffs.filtered(
            lambda row: row.website_id == target.website_id
            and row.company_id == target.website_id.company_id
        )
        ids = repoint.ids
        # Native login also moves tracks across Websites. Keep neither pointer
        # when that native target is outside this capture's Website/company.
        detached = handoffs - repoint
        if detached:
            detached._detach_visitor_references()
        result = super()._merge_visitor(target)
        try:
            with self.env.cr.savepoint():
                self.env["marketing.website.whatsapp.handoff"].browse(
                    ids
                ).exists()._service().with_context(
                    whatsapp_journey_visitor_merge=VISITOR_MERGE_TOKEN
                ).write(
                    {"visitor_id": target.id}
                )
        except OperationalError:
            raise
        except Exception as error:
            _logger.warning(
                "Website WhatsApp visitor merge failed (%s)", type(error).__name__
            )
            # The source is already gone; a target-owned track on a detached
            # capture would violate its visitor/track invariant and leak a
            # misleading navigation. Preserve the frozen acquisition instead.
            self.env["marketing.website.whatsapp.handoff"].browse(
                ids
            ).exists()._detach_visitor_references()
        return result


class WebIngressEvent(models.Model):
    _inherit = "marketing.web.ingress.event"

    @api.model
    def _cron_expire_retained_values(self, limit=100):
        result = super()._cron_expire_retained_values(limit=limit)
        handoffs = (
            self.env["marketing.website.whatsapp.handoff"]
            .sudo()
            .search(
                [
                    ("company_id", "in", self.env.companies.ids),
                    ("capture_version", "=", CAPTURE_VERSION),
                    ("erased_at", "=", False),
                    ("retention_manual", "=", False),
                    ("retain_until", "!=", False),
                    ("retain_until", "<=", fields.Datetime.now()),
                ],
                # Keep the batch bounded, but rotate failures behind captures
                # attempted fewer times so bad rows cannot starve later ones.
                order="retention_failures,retain_until,id",
                limit=max(1, min(limit, 100)),
            )
        )
        for handoff in handoffs:
            try:
                with self.env.cr.savepoint():
                    handoff._erase_journey_values()
            except OperationalError:
                raise
            except Exception as error:
                _logger.warning(
                    "Website WhatsApp retention failed (%s)", type(error).__name__
                )
                handoff._record_retention_failure()
        return result
