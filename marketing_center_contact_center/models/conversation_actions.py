from odoo import models


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        result = super()._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )
        domain = [("channel_binding_id", "in", bindings.ids)]
        # Canonical acquisition and business events survive. Only the live
        # response-processing projections depend on the deleted chat history.
        self.env["marketing.contact.center.response.cursor"].sudo().search(
            domain
        ).unlink()
        episodes = (
            self.env["marketing.contact.center.response.episode"].sudo().search(domain)
        )
        self.env["marketing.contact.center.response"].sudo().search(
            [("episode_id", "in", episodes.ids)]
        ).unlink()
        episodes.unlink()
        self.env["marketing.contact.center.response.signal"].sudo().search(
            domain
        ).unlink()
        return result
