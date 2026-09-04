from odoo import api, models


class ContactCenterCrmCaseLink(models.Model):
    _inherit = "contact.center.crm.case.link"

    @api.model_create_multi
    def create(self, vals_list):
        links = super().create(vals_list)
        # Persist only bounded queue intents in the originating request.  The
        # potentially large case x touchpoint projection runs after commit.
        self.env["marketing.contact.center.crm.service"].sudo()._enqueue_case_links(
            links
        )
        return links

    def _contact_center_before_tombstone(self, reason):
        # The Contact Center core invokes this hook only after its graph lock and
        # repeated authorization check, while the live CRM identity is still
        # available. Revocation and tombstone therefore share one transaction.
        result = super()._contact_center_before_tombstone(reason)
        self.env["marketing.contact.center.crm.service"].sudo()._revoke_case_links(self)
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
