"""One optional classifier for native CRM UTM fields, across acquisition channels."""
import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_CRM_UTM_TOKEN

UTM_FIELDS = ("campaign_id", "source_id", "medium_id")
STATES = [
    ("pending", "Pending"),
    ("disabled", "Disabled"),
    ("missing", "Awaiting evidence"),
    ("simulation", "Simulation"),
    ("applied", "Applied"),
    ("present", "Already present"),
    ("conflict", "Needs review"),
    ("manual", "Manual classification"),
    ("revoked", "Evidence removed"),
    ("reverted", "Reverted"),
]


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MarketingCrmUtmApplication(models.Model):
    _name = "marketing.crm.utm.application"
    _description = "CRM Native UTM Classification Receipt"
    _order = "id desc"
    _check_company_auto = True

    lead_id = fields.Many2one(
        "crm.lead", ondelete="set null", readonly=True, index=True
    )
    lead_res_id = fields.Integer(required=True, readonly=True)
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    state = fields.Selection(STATES, required=True, readonly=True)
    reason = fields.Char(readonly=True)
    fingerprint = fields.Char(required=True, readonly=True)
    source_id = fields.Many2one(
        "marketing.center.source", readonly=True, ondelete="restrict"
    )
    entity_id = fields.Many2one(
        "marketing.center.external.entity", readonly=True, ondelete="restrict"
    )
    before_json = fields.Json(required=True, readonly=True)
    after_json = fields.Json(required=True, readonly=True)
    evidence_json = fields.Json(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if (
            self.env.context.get("marketing_crm_utm_token")
            is not MARKETING_CRM_UTM_TOKEN
        ):
            raise AccessError(_("Classification receipts are created internally."))
        return super().create(values_list)

    def write(self, values):
        raise AccessError(_("Classification receipts are immutable."))

    def unlink(self):
        raise AccessError(_("Classification receipts are immutable."))


class CrmLeadNativeUtm(models.Model):
    _inherit = "crm.lead"

    marketing_utm_state = fields.Selection(
        STATES, default="pending", readonly=True, copy=False
    )
    marketing_utm_reason = fields.Char(readonly=True, copy=False)
    marketing_utm_manual = fields.Boolean(readonly=True, copy=False)
    marketing_utm_default_json = fields.Json(readonly=True, copy=False)
    marketing_utm_baseline_json = fields.Json(readonly=True, copy=False)
    marketing_utm_receipt_id = fields.Many2one(
        "marketing.crm.utm.application", readonly=True, copy=False, ondelete="restrict"
    )
    marketing_utm_application_ids = fields.One2many(
        "marketing.crm.utm.application", "lead_id", readonly=True
    )
    marketing_utm_effective_link_ids = fields.One2many(
        "marketing.attribution.crm.effective.link", "lead_id", readonly=True
    )

    @api.model
    def _native_utm_protected_fields(self):
        return {
            "marketing_utm_state",
            "marketing_utm_reason",
            "marketing_utm_manual",
            "marketing_utm_default_json",
            "marketing_utm_baseline_json",
            "marketing_utm_receipt_id",
        }

    @api.model_create_multi
    def create(self, values_list):
        protected = self._native_utm_protected_fields()
        if (
            self.env.context.get("marketing_crm_utm_token")
            is not MARKETING_CRM_UTM_TOKEN
        ):
            if any(protected.intersection(v) for v in values_list) or any(
                "default_" + key in self.env.context for key in protected
            ):
                raise AccessError(
                    _("Native classification provenance is managed internally.")
                )
        return super().create(values_list)

    def write(self, values):
        internal = (
            self.env.context.get("marketing_crm_utm_token") is MARKETING_CRM_UTM_TOKEN
        )
        if not internal and self._native_utm_protected_fields().intersection(values):
            raise AccessError(
                _("Native classification provenance is managed internally.")
            )
        if not internal and set(UTM_FIELDS).intersection(values):
            # Even an explicit write of the same value records ownership. No
            # attribution writer may infer manual intent from create_uid.
            self._native_utm_lock()
            values = dict(
                values,
                marketing_utm_manual=True,
                marketing_utm_state="manual",
                marketing_utm_reason="native_fields_edited",
            )
        return super().write(values)

    def _native_utm_lock(self):
        if self:
            self.env.cr.execute(
                "SELECT id FROM crm_lead WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(self.ids)],
            )
            self.invalidate_recordset(
                list(UTM_FIELDS) + list(self._native_utm_protected_fields())
            )

    def _native_utm_values(self):
        self.ensure_one()
        return {key: self[key].id or False for key in UTM_FIELDS}

    def _native_utm_write(self, values):
        return self.with_context(marketing_crm_utm_token=MARKETING_CRM_UTM_TOKEN).write(
            values
        )

    def _mark_native_utm_website_default(self):
        """Called only at native insertion after proving no submitted/cookie UTMs."""
        self.ensure_one()
        self._native_utm_lock()
        expected = {
            "campaign_id": False,
            "source_id": False,
            "medium_id": self.env.ref("utm.utm_medium_website").id,
        }
        if self._native_utm_values() == expected and not self.marketing_utm_manual:
            self._native_utm_write({"marketing_utm_default_json": expected})

    def _enqueue_native_utm(self):
        for lead in self.exists():
            company = (
                lead.marketing_event_company_id or lead.company_id or self.env.company
            )
            if not lead.marketing_utm_receipt_id and not self.env[
                "marketing.center.source"
            ].sudo().search(
                [
                    ("company_id", "=", company.id),
                    ("native_utm_mode", "!=", "disabled"),
                ],
                limit=1,
            ):
                continue
            lead.sudo().with_company(company).with_context(
                allowed_company_ids=[company.id]
            ).with_delay(
                identity_key="marketing_native_utm:lead:%s" % lead.id,
                max_retries=5,
                description="Resolve native CRM campaign #%s" % lead.id,
            )._job_resolve_native_utm()

    def _job_resolve_native_utm(self):
        self.ensure_one()
        if self.exists():
            return self.env["marketing.crm.native.utm.service"]._classify(
                self, apply=True
            )
        return False

    def action_preview_native_utm(self):
        self.ensure_one()
        self._check_native_utm_operator()
        result = self.env["marketing.crm.native.utm.service"]._classify(
            self, apply=False
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Campaign classification preview"),
                "message": result["reason"],
                "type": "info",
                "sticky": True,
            },
        }

    def action_reconcile_native_utm(self):
        self._check_native_utm_operator()
        self._enqueue_native_utm()
        return True

    def _check_native_utm_operator(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Marketing administrator access is required."))
        self.check_access_rights("write")
        self.check_access_rule("write")

    def action_revert_native_utm(self):
        self.ensure_one()
        self._check_native_utm_operator()
        self._native_utm_lock()
        receipt = self.marketing_utm_receipt_id
        if (
            not receipt
            or receipt.state != "applied"
            or self.marketing_utm_manual
            or self._native_utm_values() != receipt.after_json
        ):
            raise ValidationError(
                _(
                    "The automatic classification was changed; preserve the current values."
                )
            )
        before = self._native_utm_values()
        self._native_utm_write(
            dict(
                self.marketing_utm_baseline_json or receipt.before_json,
                marketing_utm_manual=True,
            )
        )
        self.env["marketing.crm.native.utm.service"]._record(
            self, "reverted", "operator_reverted", before, self._native_utm_values(), {}
        )
        return True


class MarketingCrmNativeUtmService(models.AbstractModel):
    _name = "marketing.crm.native.utm.service"
    _description = "Native CRM UTM Classification"

    @api.model
    def _record(self, lead, state, reason, before, after, evidence):
        value = {
            "state": state,
            "reason": reason,
            "before": before,
            "after": after,
            "evidence": evidence,
        }
        fingerprint = _fingerprint(value)
        previous = (
            self.env["marketing.crm.utm.application"]
            .sudo()
            .search([("lead_id", "=", lead.id)], order="id desc", limit=1)
        )
        if previous and previous.fingerprint == fingerprint:
            return previous
        company = lead.marketing_event_company_id or lead.company_id or self.env.company
        receipt = (
            self.env["marketing.crm.utm.application"]
            .sudo()
            .with_context(marketing_crm_utm_token=MARKETING_CRM_UTM_TOKEN)
            .create(
                {
                    "lead_id": lead.id,
                    "lead_res_id": lead.id,
                    "company_id": company.id,
                    "state": state,
                    "reason": reason,
                    "fingerprint": fingerprint,
                    "before_json": before,
                    "after_json": after,
                    "evidence_json": evidence,
                    "source_id": evidence.get("source_id"),
                    "entity_id": evidence.get("entity_id"),
                }
            )
        )
        values = {"marketing_utm_state": state, "marketing_utm_reason": reason}
        # Keep the last successful receipt to allow safe undo even after a replay.
        if state == "applied":
            values["marketing_utm_receipt_id"] = receipt.id
            if not lead.marketing_utm_baseline_json:
                values["marketing_utm_baseline_json"] = before
        lead._native_utm_write(values)
        return receipt

    @api.model
    def _classify(self, lead, apply=False):
        lead.ensure_one()
        if lead._name != "crm.lead":
            raise ValidationError(_("A CRM lead is required."))
        lead.check_access_rights("read")
        lead.check_access_rule("read")
        company = lead.marketing_event_company_id or lead.company_id or self.env.company
        if company not in self.env.companies:
            raise AccessError(_("The CRM company is not available."))
        if apply:
            lead._native_utm_lock()
        before = lead._native_utm_values()
        links = (
            self.env["marketing.attribution.crm.effective.link"]
            .sudo()
            .search([("company_id", "=", company.id), ("lead_id", "=", lead.id)])
        )
        links.invalidate_recordset()
        resolver = self.env["marketing.native.utm.service"].with_company(company)
        candidates = []
        for link in links:
            candidate = resolver._resolve_touchpoint(
                link.touchpoint_id.touchpoint_id, apply=False, lock=apply
            )
            candidate = dict(
                candidate, touchpoint_id=link.touchpoint_id.touchpoint_id.id
            )
            candidates.append(candidate)
        active = [item for item in candidates if item["state"] != "disabled"]
        ready = [item for item in active if item["state"] in ("ready", "would_create")]
        state, reason, evidence, after = (
            "missing",
            "awaiting_campaign_evidence",
            {},
            before,
        )
        if lead.marketing_utm_manual:
            state, reason = "manual", "native_fields_edited"
        elif not links:
            state, reason = (
                ("revoked", "effective_evidence_removed")
                if lead.marketing_utm_receipt_id
                else ("missing", "no_effective_link")
            )
        elif any(item["state"] == "conflict" for item in active):
            state, reason = "conflict", "ambiguous_campaign_evidence"
        elif any(item["reason"] == "campaign_resolution_pending" for item in active):
            state, reason = "missing", "campaign_resolution_pending"
        elif ready:
            # Several touchpoints may support one classification. Distinct unmapped
            # entities are not merged by name; mapped entities may share a tuple.
            identities = {
                (
                    item.get("campaign_id") or ("entity", item["entity_id"]),
                    item.get("utm_source_id"),
                    item.get("medium_id"),
                )
                for item in ready
            }
            if len(identities) != 1:
                state, reason = "conflict", "multiple_campaign_classifications"
            else:
                evidence = dict(ready[0])
                evidence["touchpoint_ids"] = sorted(
                    item["touchpoint_id"] for item in ready
                )
                desired = {
                    "campaign_id": evidence.get("campaign_id") or False,
                    "source_id": evidence.get("utm_source_id") or False,
                    "medium_id": evidence.get("medium_id") or False,
                }
                last = lead.marketing_utm_receipt_id
                writable = (
                    not any(before.values())
                    or before == lead.marketing_utm_default_json
                    or (last and before == last.after_json)
                )
                if not writable and before != desired:
                    state, reason = "conflict", "existing_native_values_preserved"
                elif evidence.get("campaign_id") and before == desired:
                    state, reason = "present", "native_values_already_match"
                elif not apply or any(
                    item.get("source_mode") != "apply" for item in ready
                ):
                    state, reason = (
                        "simulation",
                        "would_create_campaign"
                        if not desired["campaign_id"]
                        else "would_classify",
                    )
                else:
                    resolved = resolver._resolve_touchpoint(
                        self.env["marketing.attribution.touchpoint"].browse(
                            evidence["touchpoint_id"]
                        ),
                        apply=True,
                    )
                    if resolved["state"] != "ready":
                        state, reason = "missing", resolved.get(
                            "reason", "campaign_unavailable"
                        )
                    else:
                        evidence.update(resolved)
                        after = {
                            "campaign_id": resolved["campaign_id"],
                            "source_id": resolved["utm_source_id"],
                            "medium_id": resolved["medium_id"],
                        }
                        lead._native_utm_write(after)
                        state, reason = "applied", "confirmed_campaign_classification"
        elif candidates and not active:
            state, reason = "disabled", "source_classification_disabled"
        # A previously owned tuple must not keep claiming evidence which has
        # disappeared or become ambiguous. Configuration disable only pauses.
        last = lead.marketing_utm_receipt_id
        if (
            state in {"missing", "conflict", "revoked"}
            and last
            and not lead.marketing_utm_manual
            and before == last.after_json
        ):
            if last.source_id.native_utm_mode == "apply" and not any(
                item.get("source_mode") == "simulate" for item in candidates
            ):
                after = lead.marketing_utm_baseline_json or last.before_json
                if apply:
                    lead._native_utm_write(after)
            elif last.source_id.native_utm_mode == "disabled":
                state, reason = "disabled", "source_classification_disabled"
            else:
                state, reason = "simulation", "would_restore_previous_values"
        if apply:
            self._record(lead, state, reason, before, after, evidence)
        return {
            "state": state,
            "reason": reason,
            "before": before,
            "after": after,
            "evidence": evidence,
        }


class MarketingCrmUtmLinkHooks(models.AbstractModel):
    _inherit = "marketing.crm.service"

    @api.model
    def _link_touchpoint_lead(self, touchpoint, lead, *args, **kwargs):
        result = super()._link_touchpoint_lead(touchpoint, lead, *args, **kwargs)
        lead._enqueue_native_utm()
        return result

    @api.model
    def _revoke_attribution_assertions(self, assertions, *args, **kwargs):
        leads = assertions.mapped("lead_id")
        leads._native_utm_lock()
        # Besides serializing current writers, advance the row version so an
        # older repeatable-read snapshot retries after a committed revocation.
        for lead in leads:
            lead._native_utm_write({"marketing_utm_state": lead.marketing_utm_state})
        result = super()._revoke_attribution_assertions(assertions, *args, **kwargs)
        leads._enqueue_native_utm()
        return result


class MarketingNativeUtmCrmHook(models.AbstractModel):
    _inherit = "marketing.native.utm.service"

    @api.model
    def _after_native_utm_change(
        self, source_ids=None, entity_ids=None, touchpoint_ids=None
    ):
        result = super()._after_native_utm_change(
            source_ids=source_ids, entity_ids=entity_ids, touchpoint_ids=touchpoint_ids
        )
        scope = {
            "source_ids": sorted(set(source_ids or [])),
            "entity_ids": sorted(set(entity_ids or [])),
            "touchpoint_ids": sorted(set(touchpoint_ids or [])),
        }
        if not any(scope.values()):
            return result
        for company in self.env.companies:
            if not (scope["source_ids"] or scope["entity_ids"]):
                enabled = (
                    self.env["marketing.center.source"]
                    .sudo()
                    .search(
                        [
                            ("company_id", "=", company.id),
                            ("native_utm_mode", "!=", "disabled"),
                        ],
                        limit=1,
                    )
                )
                owned = (
                    self.env["crm.lead"]
                    .sudo()
                    .with_context(active_test=False)
                    .search(
                        [
                            ("marketing_event_company_id", "=", company.id),
                            ("marketing_utm_receipt_id", "!=", False),
                        ],
                        limit=1,
                    )
                )
                if not enabled and not owned:
                    continue
            company.sudo().with_company(company).with_context(
                allowed_company_ids=[company.id]
            ).with_delay(
                identity_key="marketing_native_utm:scope:%s:%s"
                % (company.id, _fingerprint(scope)),
                max_retries=5,
            )._job_reconcile_native_utm(
                scope
            )
        return result


class CompanyNativeUtmReconcile(models.Model):
    _inherit = "res.company"

    def _job_reconcile_native_utm(self, scope, after_id=0):
        self.ensure_one()
        if self not in self.env.companies or not isinstance(scope, dict):
            raise AccessError(_("Invalid classification reconciliation scope."))
        if (
            set(scope) != {"source_ids", "entity_ids", "touchpoint_ids"}
            or any(
                not isinstance(ids, list)
                or len(ids) > 1000
                or any(type(i) is not int or i <= 0 for i in ids)
                for ids in scope.values()
            )
            or type(after_id) is not int
            or after_id < 0
        ):
            raise ValidationError(_("Invalid classification reconciliation cursor."))
        domains = []
        prefix = "marketing_utm_effective_link_ids.touchpoint_id.touchpoint_id.asset_resolution_ids."
        if scope["source_ids"]:
            domains.append([(prefix + "source_id", "in", scope["source_ids"])])
        if scope["entity_ids"]:
            domains.append([(prefix + "entity_id", "child_of", scope["entity_ids"])])
        if scope["touchpoint_ids"]:
            points = (
                self.env["marketing.attribution.touchpoint"]
                .sudo()
                .search(
                    [
                        ("id", "in", scope["touchpoint_ids"]),
                        ("company_id", "=", self.id),
                    ]
                )
            )
            domains.append(
                [
                    (
                        "marketing_attribution_link_ids.canonical_key",
                        "in",
                        points.mapped("canonical_key"),
                    )
                ]
            )
        if not domains:
            return 0
        from odoo.osv import expression

        domain = expression.AND(
            [
                [("id", ">", after_id), ("marketing_event_company_id", "=", self.id)],
                expression.OR(domains),
            ]
        )
        leads = (
            self.env["crm.lead"]
            .sudo()
            .with_context(active_test=False)
            .search(domain, order="id", limit=100)
        )
        leads._enqueue_native_utm()
        if len(leads) == 100:
            self.with_delay(max_retries=5)._job_reconcile_native_utm(
                scope, after_id=leads[-1].id
            )
        return len(leads)
