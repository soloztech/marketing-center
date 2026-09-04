import re

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)

from .tokens import MARKETING_META_CRM_WRITE_TOKEN

META_LEAD_SUBMISSION_AUTHORITY = "meta.lead_submission"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MarketingCenterMetaCrmService(models.AbstractModel):
    _name = "marketing.center.meta.crm.service"
    _description = "Meta Lead Ads CRM Projection Service"

    @api.model
    def _validated_submission(self, submission):
        submission = submission.sudo().exists()
        if (
            getattr(submission, "_name", "") != "marketing.center.meta.lead.submission"
            or len(submission) != 1
        ):
            raise ValidationError(
                _("A single authenticated Meta lead submission is required.")
            )
        touchpoint = submission.touchpoint_id
        expected_evidence_ref = "meta.lead.submission:%s" % submission.public_ref
        if (
            submission.state != "ingested"
            or not touchpoint
            or not _SHA256_RE.fullmatch(submission.provider_payload_sha256 or "")
            or touchpoint.company_id != submission.company_id
            or touchpoint.source_system != "meta.lead_ads"
            or touchpoint.source_evidence_ref != expected_evidence_ref
        ):
            raise ValidationError(
                _("A single authenticated Meta lead submission is required.")
            )
        if submission.company_id not in self.env.companies:
            raise AccessError(_("The Meta lead submission company is unavailable."))
        return submission

    @api.model
    def _ensure_projection(self, submission):
        submission = self._validated_submission(submission)
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_meta_crm_submission:%s:%s"
            % (submission.company_id.id, submission.id),
        )
        Projection = self.env["marketing.center.meta.crm.projection"].sudo()
        projection = Projection.search([("submission_id", "=", submission.id)], limit=1)
        if projection:
            if (
                projection.company_id != submission.company_id
                or projection.route_id != submission.route_id
                or projection.touchpoint_id != submission.touchpoint_id
            ):
                raise ValidationError(
                    _("The persisted Meta CRM projection conflicts with its evidence.")
                )
            return projection
        return (
            Projection.with_company(submission.company_id)
            .with_context(marketing_meta_crm_write_token=MARKETING_META_CRM_WRITE_TOKEN)
            .create(
                {
                    "company_id": submission.company_id.id,
                    "submission_id": submission.id,
                    "route_id": submission.route_id.id,
                    "touchpoint_id": submission.touchpoint_id.id,
                }
            )
        )

    @api.model
    def _ensure_and_enqueue(self, submissions, retry_terminal=False):
        submissions = submissions.sudo().exists()
        if getattr(submissions, "_name", "") != "marketing.center.meta.lead.submission":
            raise ValidationError(_("Valid Meta lead submissions are required."))
        projections = self.env["marketing.center.meta.crm.projection"]
        for submission in submissions.sorted("id"):
            if (
                submission.state == "ingested"
                and submission.touchpoint_id
                and submission.route_id.crm_auto_create_lead
            ):
                projections |= self._ensure_projection(submission)
        projections._enqueue(retry_terminal=retry_terminal)
        return projections

    @api.model
    def _first_value(self, fields_by_name, names, limit):
        for name in names:
            for raw_value in fields_by_name.get(name, ()):
                value = str(raw_value or "").strip()
                if value:
                    return value[:limit]
        return ""

    @api.model
    def _lead_values(self, projection):
        route = projection.route_id
        private_fields = projection.submission_id.field_ids.sudo()
        fields_by_name = {
            field.field_name: tuple(field.values_json or ())
            for field in private_fields
            if field.field_name
        }
        full_name = self._first_value(fields_by_name, ("full_name",), 255)
        if not full_name:
            first_name = self._first_value(fields_by_name, ("first_name",), 128)
            last_name = self._first_value(fields_by_name, ("last_name",), 128)
            full_name = " ".join(part for part in (first_name, last_name) if part)[:255]
        email = self._first_value(
            fields_by_name, ("email", "email_address"), 320
        ).lower()
        if email and not _EMAIL_RE.fullmatch(email):
            email = ""
        phone = self._first_value(fields_by_name, ("phone_number", "phone"), 64)
        if phone:
            digits = "".join(character for character in phone if character.isdigit())
            phone = phone if 7 <= len(digits) <= 15 else ""
        company_name = self._first_value(fields_by_name, ("company_name",), 255)
        prefix = (route.crm_lead_title_prefix or "Meta Lead Ads").strip()
        title = "%s — %s" % (prefix, full_name) if full_name else prefix
        values = {
            "name": title[:255],
            "company_id": projection.company_id.id,
            "type": route.crm_lead_type,
            "team_id": route.crm_team_id.id or False,
            "user_id": route.crm_user_id.id or False,
            # Never inherit a partner from the caller's default context.  The
            # projection creates a CRM lead, not a contact.
            "partner_id": False,
        }
        if full_name:
            values["contact_name"] = full_name
        if email:
            values["email_from"] = email
        if phone:
            values["phone"] = phone
        if company_name:
            values["partner_name"] = company_name
        if route.crm_tag_ids:
            values["tag_ids"] = [Command.set(route.crm_tag_ids.ids)]
        return values

    @api.model
    def _project(self, projection):
        projection = projection.sudo().exists()
        if (
            getattr(projection, "_name", "") != "marketing.center.meta.crm.projection"
            or len(projection) != 1
        ):
            raise ValidationError(_("A single valid Meta CRM projection is required."))
        self.env.cr.execute(
            "SELECT id FROM marketing_center_meta_crm_projection "
            "WHERE id = %s FOR UPDATE",
            [projection.id],
        )
        projection.invalidate_recordset(
            ["state", "lead_id", "assertion_id", "route_id", "submission_id"]
        )
        if projection.state == "done":
            return True
        submission = self._validated_submission(projection.submission_id)
        route = submission.route_id
        if not route.crm_auto_create_lead:
            projection._internal_write(
                {
                    "state": "skipped",
                    "queue_job_uuid": False,
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            return True
        lead = (
            self.env["crm.lead"]
            .sudo()
            .with_context(
                allowed_company_ids=[projection.company_id.id],
                mail_create_nosubscribe=True,
                tracking_disable=True,
            )
            .with_company(projection.company_id)
            .create(self._lead_values(projection))
        )
        source_ref = "meta:lead-submission:%s" % submission.public_ref
        assertion = (
            self.env["marketing.crm.service"]
            .sudo()
            .with_context(allowed_company_ids=[projection.company_id.id])
            .with_company(projection.company_id)
            ._link_touchpoint_lead(
                submission.touchpoint_id,
                lead,
                source_ref,
                authority_key=META_LEAD_SUBMISSION_AUTHORITY,
                authority_ref=submission.public_ref,
                assertion_ref="meta-lead-submission:%s" % submission.public_ref,
            )
        )
        projection._internal_write(
            {
                "state": "done",
                "lead_id": lead.id,
                **self.env["marketing.crm.service"]._lead_snapshot_values(lead),
                "assertion_id": assertion.id,
                "queue_job_uuid": False,
                "processed_at": fields.Datetime.now(),
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return True

    @api.model
    def _enqueue_backfill(self, company=None, after_submission_id=0, limit=200):
        if company is None:
            company = self.env.company
        if (
            getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The Meta CRM backfill company is unavailable."))
        after_submission_id = max(int(after_submission_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        submissions = (
            self.env["marketing.center.meta.lead.submission"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", after_submission_id),
                    ("state", "=", "ingested"),
                    ("touchpoint_id", "!=", False),
                    ("route_id.crm_auto_create_lead", "=", True),
                ],
                order="id asc",
                limit=limit,
            )
        )
        projections = self._ensure_and_enqueue(submissions, retry_terminal=True)
        return {
            "processed_submissions": len(submissions),
            "projection_count": len(projections),
            "last_submission_id": submissions[-1:].id or after_submission_id,
            "has_more": len(submissions) == limit,
        }
