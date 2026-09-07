from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_crm.models.conversation_link import (
    conversation_graph_is_locked,
)
from odoo.addons.marketing_center_contact_center.services.mapper import MAPPING_VERSION

CONTACT_CENTER_CONVERSATION_AUTHORITY = "contact_center.conversation"


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
    def _lock_conversation_link_graph(self, conversation_links):
        """Acquire the Contact graph before this bridge's channel lock."""

        conversation_links = conversation_links.sudo().exists()
        if not conversation_links or conversation_graph_is_locked(self.env):
            return conversation_links
        conversation_links = conversation_links._lock_crm_graph()
        conversation_links.invalidate_recordset(
            ["channel_id", "company_id", "lead_id", "state"]
        )
        return conversation_links.exists()

    @api.model
    def _source_reference(self, conversation_link, attribution_link):
        return "contact_center:conversation:%s:link:%s:canonical:%s" % (
            str(conversation_link.channel_id.id),
            conversation_link.id,
            attribution_link.marketing_touchpoint_id.canonical_key,
        )

    @api.model
    def _assertion_reference(self, conversation_link, touchpoint):
        return "conversation-link:%s:canonical:%s:lead:%s" % (
            conversation_link.id,
            touchpoint.canonical_key,
            conversation_link.lead_id.id,
        )

    @api.model
    def _conversation_links_for_channel(self, channel):
        company = channel.contact_center_company_id
        return (
            self.env["contact.center.crm.conversation.link"]
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
    def _conversation_link_page(self, channel, after_id=0, limit=100):
        channel = self._valid_channel(channel)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 100), 1), 500)
        return (
            self.env["contact.center.crm.conversation.link"]
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
    def _active_conversation_link(self, company, conversation_link_id):
        return (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", conversation_link_id),
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
    def _enqueue_conversation_links(self, conversation_links):
        conversation_links = conversation_links.sudo().exists()
        if (
            getattr(conversation_links, "_name", "")
            != "contact.center.crm.conversation.link"
        ):
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        count = 0
        for conversation_link in conversation_links.filtered(
            lambda link: link.state == "active" and bool(link.lead_id)
        ).sorted("id"):
            company = conversation_link.company_id.sudo()
            company._enqueue_marketing_contact_center_crm_conversation_link(
                conversation_link.id
            )
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
    def _link_pairs(self, channel, conversation_links, attribution_links):
        company = channel.contact_center_company_id
        if not conversation_links or not attribution_links:
            return self.env["marketing.attribution.crm.link"]
        crm_service = self.env["marketing.crm.service"].sudo().with_company(company)
        Link = self.env["marketing.attribution.crm.link"].sudo().with_company(company)
        result = Link.browse()
        seen = set()
        for attribution_link in attribution_links:
            touchpoint = attribution_link.marketing_touchpoint_id
            for conversation_link in conversation_links:
                assertion_ref = self._assertion_reference(conversation_link, touchpoint)
                pair = (CONTACT_CENTER_CONVERSATION_AUTHORITY, assertion_ref)
                if pair in seen:
                    continue
                seen.add(pair)
                result |= crm_service._link_touchpoint_lead(
                    touchpoint,
                    conversation_link.lead_id,
                    self._source_reference(conversation_link, attribution_link),
                    authority_key=CONTACT_CENTER_CONVERSATION_AUTHORITY,
                    authority_ref=str(conversation_link.channel_id.id),
                    assertion_ref=assertion_ref,
                )
        return result

    @api.model
    def _revoke_conversation_links(self, conversation_links):
        """Revoke Contact Center assertions before their conversation links disappear."""

        conversation_links = conversation_links.sudo().exists()
        if (
            getattr(conversation_links, "_name", "")
            != "contact.center.crm.conversation.link"
        ):
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        result = self.env["marketing.attribution.crm.revocation"].sudo()
        for channel in conversation_links.mapped("channel_id").sorted("id"):
            self._valid_channel(channel)
            channel_links = conversation_links.filtered(
                lambda link, channel=channel: link.channel_id == channel
            )
            channel_links = self._lock_conversation_link_graph(channel_links)
            self._lock_channel(channel)
            for conversation_link in channel_links.sorted("id"):
                if conversation_link.company_id != channel.contact_center_company_id:
                    raise ValidationError(
                        _(
                            "Convergence records must belong to one company and "
                            "conversation."
                        )
                    )
                result |= (
                    self.env["marketing.crm.service"]
                    .sudo()
                    .with_company(conversation_link.company_id)
                    ._revoke_authority_assertions(
                        conversation_link.lead_id,
                        CONTACT_CENTER_CONVERSATION_AUTHORITY,
                        str(conversation_link.channel_id.id),
                        "contact-center-conversation-link:%s:unlink"
                        % conversation_link.id,
                        reason="contact_center_conversation_unlinked",
                    )
                )
        return result

    @api.model
    def _reconcile_channel(
        self, channel, conversation_links=None, attribution_links=None
    ):
        """Create a complete or bounded M:N projection for one conversation.

        A conversation may contain an explicit Contact Center CRM link to
        a CRM lead, while a lead may be linked to several conversations.
        The immutable source ledgers remain untouched; this method only asks the
        CRM integration service to create missing attribution links.

        Durable live jobs provide only the newly-created side plus one bounded
        page of the opposite side.  Omitting both arguments remains an explicit
        full-repair primitive; request hooks and installation backfill never use
        that unbounded form.
        """

        channel = self._valid_channel(channel).sudo()
        company = channel.contact_center_company_id
        if conversation_links is None:
            conversation_links = self._conversation_links_for_channel(channel)
        else:
            conversation_links = conversation_links.sudo().exists()
        if attribution_links is None:
            attribution_links = self._attribution_links_for_channel(channel)
        else:
            attribution_links = attribution_links.sudo().exists()
        if any(
            link.company_id != company
            or link.channel_id != channel
            or link.state != "active"
            or not link.lead_id
            for link in conversation_links
        ) or any(
            link.company_id != company
            or link.mapping_version != MAPPING_VERSION
            or link.source_touchpoint_id.channel_binding_id.channel_id != channel
            for link in attribution_links
        ):
            raise ValidationError(
                _("Convergence records must belong to one company and conversation.")
            )
        conversation_links = self._lock_conversation_link_graph(conversation_links)
        if any(
            link.company_id != company
            or link.channel_id != channel
            or link.state != "active"
            or not link.lead_id
            for link in conversation_links
        ):
            raise ValidationError(
                _("Convergence records changed while their graph was being locked.")
            )
        self._lock_channel(channel)
        return self._link_pairs(channel, conversation_links, attribution_links)

    @api.model
    def _reconcile_conversation_links(self, conversation_links):
        conversation_links = conversation_links.sudo().exists()
        if (
            getattr(conversation_links, "_name", "")
            != "contact.center.crm.conversation.link"
        ):
            raise ValidationError(_("Valid Contact Center CRM links are required."))
        conversation_links = conversation_links.filtered(
            lambda link: link.state == "active" and bool(link.lead_id)
        )
        result = self.env["marketing.attribution.crm.link"]
        for channel in conversation_links.mapped("channel_id").sorted("id"):
            result |= self._reconcile_channel(
                channel,
                conversation_links=conversation_links.filtered(
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
    def _reconcile_existing(self, company=None, after_conversation_link_id=0, limit=50):
        """Reconcile one bounded installation/backfill page, in stable order."""

        if company is None:
            company = self.env.company
        if (
            getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The reconciliation company is not available."))
        after_conversation_link_id = max(int(after_conversation_link_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        conversation_links = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", after_conversation_link_id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="id asc",
                limit=limit,
            )
        )
        enqueued = self._enqueue_conversation_links(conversation_links)
        return {
            "processed_conversation_links": len(conversation_links),
            "enqueued_conversation_links": enqueued,
            "last_conversation_link_id": conversation_links[-1:].id
            or after_conversation_link_id,
            "has_more": len(conversation_links) == limit,
        }
