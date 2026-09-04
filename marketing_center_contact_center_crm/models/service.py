from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_crm.models.stage_sync import stage_sync_graph_is_locked
from odoo.addons.marketing_center_contact_center.services.mapper import MAPPING_VERSION

CONTACT_CENTER_CASE_AUTHORITY = "contact_center.case"


class MarketingContactCenterCrmService(models.AbstractModel):
    _name = "marketing.contact.center.crm.service"
    _description = "Marketing Contact Center CRM Convergence Service"

    @api.model
    def _valid_channel(self, channel):
        channel = channel.exists()
        if getattr(channel, "_name", "") != "mail.channel" or len(channel) != 1:
            raise ValidationError(_("A single valid conversation is required."))
        if (
            channel.channel_type != "contact_center"
            or not channel.contact_center_company_id
        ):
            raise ValidationError(
                _("Marketing CRM convergence requires a Contact Center conversation.")
            )
        return channel

    @api.model
    def _lock_channel(self, channel):
        lock_key = "marketing_contact_center_crm:%s:%s" % (
            channel.contact_center_company_id.id,
            channel.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [lock_key],
        )
        return True

    @api.model
    def _lock_case_link_graph(self, case_links):
        """Acquire the Contact graph before this bridge's channel lock."""

        case_links = case_links.sudo().exists()
        if not case_links or stage_sync_graph_is_locked(self.env):
            return case_links
        cases = case_links.mapped("case_id")
        cases._contact_center_lock_crm_graph(
            lead_ids=case_links.mapped("lead_id").ids,
        )
        case_links.invalidate_recordset(
            ["case_id", "channel_id", "company_id", "lead_id", "state"]
        )
        return case_links.exists()

    @api.model
    def _source_reference(self, case_link, attribution_link):
        return "contact_center:case:%s:link:%s:canonical:%s" % (
            case_link.case_id.case_ref,
            case_link.id,
            attribution_link.marketing_touchpoint_id.canonical_key,
        )

    @api.model
    def _assertion_reference(self, case_link, touchpoint):
        return "case-link:%s:canonical:%s:lead:%s" % (
            case_link.id,
            touchpoint.canonical_key,
            case_link.lead_id.id,
        )

    @api.model
    def _case_links_for_channel(self, channel):
        company = channel.contact_center_company_id
        return (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("channel_id", "=", channel.id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="id asc",
            )
        )

    @api.model
    def _case_link_page(self, channel, after_id=0, limit=100):
        channel = self._valid_channel(channel)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 100), 1), 500)
        return (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", channel.contact_center_company_id.id),
                    ("channel_id", "=", channel.id),
                    ("id", ">", after_id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="id asc",
                limit=limit,
            )
        )

    @api.model
    def _attribution_links_for_channel(self, channel):
        company = channel.contact_center_company_id
        return (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("mapping_version", "=", MAPPING_VERSION),
                    (
                        "source_touchpoint_id.channel_binding_id.channel_id",
                        "=",
                        channel.id,
                    ),
                ],
                order="marketing_touchpoint_id asc, id asc",
            )
        )

    @api.model
    def _attribution_link_page(self, channel, after_id=0, limit=100):
        channel = self._valid_channel(channel)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 100), 1), 500)
        return (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", channel.contact_center_company_id.id),
                    ("id", ">", after_id),
                    ("mapping_version", "=", MAPPING_VERSION),
                    (
                        "source_touchpoint_id.channel_binding_id.channel_id",
                        "=",
                        channel.id,
                    ),
                ],
                order="id asc",
                limit=limit,
            )
        )

    @api.model
    def _active_case_link(self, company, case_link_id):
        return (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", case_link_id),
                    ("company_id", "=", company.id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                limit=1,
            )
        )

    @api.model
    def _current_attribution_link(self, company, attribution_link_id):
        return (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", attribution_link_id),
                    ("company_id", "=", company.id),
                    ("mapping_version", "=", MAPPING_VERSION),
                    ("source_touchpoint_id.channel_binding_id", "!=", False),
                ],
                limit=1,
            )
        )

    @api.model
    def _enqueue_case_links(self, case_links):
        case_links = case_links.sudo().exists()
        if getattr(case_links, "_name", "") != "contact.center.crm.case.link":
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        count = 0
        for case_link in case_links.filtered(
            lambda link: link.state == "active" and bool(link.lead_id)
        ).sorted("id"):
            company = case_link.company_id.sudo()
            company._enqueue_marketing_contact_center_crm_case_link(case_link.id)
            count += 1
        return count

    @api.model
    def _enqueue_attribution_links(self, attribution_links):
        attribution_links = attribution_links.sudo().exists()
        if (
            getattr(attribution_links, "_name", "")
            != "marketing.attribution.contact.center.link"
        ):
            raise ValidationError(
                _("Valid Contact Center attribution links are required.")
            )
        count = 0
        for attribution_link in attribution_links.filtered(
            lambda link: link.mapping_version == MAPPING_VERSION
            and bool(link.source_touchpoint_id.channel_binding_id)
        ).sorted("id"):
            company = attribution_link.company_id.sudo()
            company._enqueue_marketing_contact_center_crm_attribution_link(
                attribution_link.id
            )
            count += 1
        return count

    @api.model
    def _link_pairs(self, channel, case_links, attribution_links):
        company = channel.contact_center_company_id
        if not case_links or not attribution_links:
            return self.env["marketing.attribution.crm.link"]
        crm_service = self.env["marketing.crm.service"].sudo().with_company(company)
        Link = self.env["marketing.attribution.crm.link"].sudo().with_company(company)
        result = Link.browse()
        seen = set()
        for attribution_link in attribution_links:
            touchpoint = attribution_link.marketing_touchpoint_id
            for case_link in case_links:
                assertion_ref = self._assertion_reference(case_link, touchpoint)
                pair = (CONTACT_CENTER_CASE_AUTHORITY, assertion_ref)
                if pair in seen:
                    continue
                seen.add(pair)
                result |= crm_service._link_touchpoint_lead(
                    touchpoint,
                    case_link.lead_id,
                    self._source_reference(case_link, attribution_link),
                    authority_key=CONTACT_CENTER_CASE_AUTHORITY,
                    authority_ref=case_link.case_id.case_ref,
                    assertion_ref=assertion_ref,
                )
        return result

    @api.model
    def _revoke_case_links(self, case_links):
        """Revoke Contact Center assertions before their case links disappear."""

        case_links = case_links.sudo().exists()
        if getattr(case_links, "_name", "") != "contact.center.crm.case.link":
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        result = self.env["marketing.attribution.crm.revocation"].sudo()
        for channel in case_links.mapped("channel_id").sorted("id"):
            self._valid_channel(channel)
            channel_links = case_links.filtered(
                lambda link, channel=channel: link.channel_id == channel
            )
            channel_links = self._lock_case_link_graph(channel_links)
            self._lock_channel(channel)
            for case_link in channel_links.sorted("id"):
                if case_link.company_id != channel.contact_center_company_id:
                    raise ValidationError(
                        _(
                            "Convergence records must belong to one company and "
                            "conversation."
                        )
                    )
                result |= (
                    self.env["marketing.crm.service"]
                    .sudo()
                    .with_company(case_link.company_id)
                    ._revoke_authority_assertions(
                        case_link.lead_id,
                        CONTACT_CENTER_CASE_AUTHORITY,
                        case_link.case_id.case_ref,
                        "contact-center-case-link:%s:unlink" % case_link.id,
                        reason="contact_center_case_unlinked",
                    )
                )
        return result

    @api.model
    def _reconcile_channel(self, channel, case_links=None, attribution_links=None):
        """Create a complete or bounded M:N projection for one conversation.

        A conversation may contain multiple Contact Center cases, each linked to
        a CRM lead, while a lead may be linked to cases in several conversations.
        The immutable source ledgers remain untouched; this method only asks the
        CRM integration service to create missing attribution links.

        Durable live jobs provide only the newly-created side plus one bounded
        page of the opposite side.  Omitting both arguments remains an explicit
        full-repair primitive; request hooks and installation backfill never use
        that unbounded form.
        """

        channel = self._valid_channel(channel).sudo()
        company = channel.contact_center_company_id
        if case_links is None:
            case_links = self._case_links_for_channel(channel)
        else:
            case_links = case_links.sudo().exists()
        if attribution_links is None:
            attribution_links = self._attribution_links_for_channel(channel)
        else:
            attribution_links = attribution_links.sudo().exists()
        if any(
            link.company_id != company
            or link.channel_id != channel
            or link.state != "active"
            or not link.lead_id
            for link in case_links
        ) or any(
            link.company_id != company
            or link.mapping_version != MAPPING_VERSION
            or link.source_touchpoint_id.channel_binding_id.channel_id != channel
            for link in attribution_links
        ):
            raise ValidationError(
                _("Convergence records must belong to one company and conversation.")
            )
        case_links = self._lock_case_link_graph(case_links)
        if any(
            link.company_id != company
            or link.channel_id != channel
            or link.state != "active"
            or not link.lead_id
            for link in case_links
        ):
            raise ValidationError(
                _("Convergence records changed while their graph was being locked.")
            )
        self._lock_channel(channel)
        return self._link_pairs(channel, case_links, attribution_links)

    @api.model
    def _reconcile_case_links(self, case_links):
        case_links = case_links.sudo().exists()
        if getattr(case_links, "_name", "") != "contact.center.crm.case.link":
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        case_links = case_links.filtered(
            lambda link: link.state == "active" and bool(link.lead_id)
        )
        result = self.env["marketing.attribution.crm.link"]
        for channel in case_links.mapped("channel_id").sorted("id"):
            result |= self._reconcile_channel(
                channel,
                case_links=case_links.filtered(
                    lambda link, channel=channel: link.channel_id == channel
                ),
            )
        return result

    @api.model
    def _reconcile_attribution_links(self, attribution_links):
        attribution_links = attribution_links.sudo().exists()
        if (
            getattr(attribution_links, "_name", "")
            != "marketing.attribution.contact.center.link"
        ):
            raise ValidationError(
                _("Valid Contact Center attribution links are required.")
            )
        attribution_links = attribution_links.filtered(
            lambda link: link.mapping_version == MAPPING_VERSION
        )
        channels = attribution_links.mapped(
            "source_touchpoint_id.channel_binding_id.channel_id"
        )
        result = self.env["marketing.attribution.crm.link"]
        for channel in channels.sorted("id"):
            result |= self._reconcile_channel(
                channel,
                attribution_links=attribution_links.filtered(
                    lambda link, channel=channel: (
                        link.source_touchpoint_id.channel_binding_id.channel_id
                        == channel
                    )
                ),
            )
        return result

    @api.model
    def _reconcile_existing(self, company=None, after_case_link_id=0, limit=50):
        """Reconcile one bounded installation/backfill page, in stable order."""

        if company is None:
            company = self.env.company
        if (
            getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The reconciliation company is not available."))
        after_case_link_id = max(int(after_case_link_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        case_links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", after_case_link_id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="id asc",
                limit=limit,
            )
        )
        enqueued = self._enqueue_case_links(case_links)
        return {
            "processed_case_links": len(case_links),
            "enqueued_case_links": enqueued,
            "last_case_link_id": case_links[-1:].id or after_case_link_id,
            "has_more": len(case_links) == limit,
        }
