"""Explicit refusal queues erasure; replacing a grant does not erase history."""

from odoo import _, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_website.models.consent import CONSENT_CONTEXT_TOKEN

from .journey_capture import CAPTURE_VERSION


class WebsiteConsent(models.Model):
    _inherit = "marketing.website.consent"

    whatsapp_erasure_queued = fields.Boolean(readonly=True, copy=False)

    def _internal_extension_fields(self):
        return super()._internal_extension_fields() | {"whatsapp_erasure_queued"}

    def write(self, values):
        if "whatsapp_erasure_queued" in values and (
            values["whatsapp_erasure_queued"] is not True
            or any(self.mapped("whatsapp_erasure_queued"))
        ):
            raise AccessError(_("A refusal intent cannot be reset."))
        return super().write(values)

    def _explicit_refusal(self):
        result = super()._explicit_refusal()
        for decision in self.sudo():
            first_refusal = not decision.whatsapp_erasure_queued
            if first_refusal:
                decision.with_context(
                    website_consent_internal=CONSENT_CONTEXT_TOKEN
                ).write({"whatsapp_erasure_queued": True})
            if first_refusal or self.env[
                "marketing.website.whatsapp.handoff"
            ].sudo().search_count(
                [
                    ("capture_consent_id", "=", decision.id),
                    ("capture_version", "=", CAPTURE_VERSION),
                    ("erased_at", "=", False),
                    ("company_id", "=", decision.company_id.id),
                ]
            ):
                # First refusal always leaves durable work, even if a capture
                # committed after this transaction's repeatable-read snapshot.
                # A technical reinvocation can recover a failed/exhausted job;
                # the browser deletes its consent cookie on refusal. Automatic
                # withdrawal retries and native operator requeue provide recovery.
                # queue_job deduplicates an existing pending identity.
                company = (
                    decision.company_id.sudo()
                    .with_context(allowed_company_ids=decision.company_id.ids)
                    .with_company(decision.company_id)
                )
                company.with_delay(
                    identity_key="whatsapp:withdrawal:%s:0" % decision.id,
                    max_retries=5,
                )._job_whatsapp_withdrawal(decision.id)
        return result
