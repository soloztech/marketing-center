from odoo import api, models


class ContactCenterCrmConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    @api.model_create_multi
    def create(self, vals_list):
        links = super().create(vals_list)
        # Persist only bounded queue intents in the originating request.  The
        # potentially large conversation link x touchpoint projection runs after commit.
        self.env[
            "marketing.contact.center.crm.service"
        ].sudo()._enqueue_conversation_links(links)
        return links

    def _transfer_to_lead(self, lead):
        result = super()._transfer_to_lead(lead)
        # Native CRM merge preserves the link ID while replacing lead_id.
        # Rebuild the current assertion identity after commit; Marketing's own
        # merge ledger preserves the original evidence in the meantime.
        self.env[
            "marketing.contact.center.crm.service"
        ].sudo()._enqueue_conversation_links(
            self.filtered(lambda link: link.state == "active" and bool(link.lead_id))
        )
        return result

    def _before_tombstone(self, reason):
        # The Contact Center core invokes this hook only after its graph lock and
        # repeated authorization check, while the live CRM identity is still
        # available. Revocation and tombstone therefore share one transaction.
        result = super()._before_tombstone(reason)
        self.env[
            "marketing.contact.center.crm.service"
        ].sudo()._revoke_conversation_links(self)
        return result


class MarketingAttributionContactCenterLink(models.Model):
    _inherit = "marketing.attribution.contact.center.link"

    @api.model_create_multi
    def create(self, vals_list):
        links = super().create(vals_list)
        # Each trigger owns a monotonic cursor, so concurrent creates cannot be
        # collapsed into a lossy conversation-level debounce.
        self.env[
            "marketing.contact.center.crm.service"
        ].sudo()._enqueue_attribution_links(links)
        return links


class ContactCenterAttributionTouchpoint(models.Model):
    _inherit = "contact.center.attribution.touchpoint"

    def write(self, values):
        result = super().write(values)
        # Contact Center can resolve a provider-only attribution event to its
        # conversation after the Marketing projection already exists.  That
        # association does not necessarily change the provider-neutral DTO, so
        # it needs its own convergence trigger rather than relying on creation
        # of another Marketing bridge row.
        if "channel_binding_id" in values:
            links = (
                self.env["marketing.attribution.contact.center.link"]
                .sudo()
                .search([("source_touchpoint_id", "in", self.ids)])
            )
            self.env[
                "marketing.contact.center.crm.service"
            ].sudo()._enqueue_attribution_links(links)
        return result
