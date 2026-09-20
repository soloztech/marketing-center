"""Bounded, best-effort attribution after Contact Center persists a message."""

import logging

from psycopg2 import OperationalError

from odoo import api, models

_logger = logging.getLogger(__name__)


class ContactCenterMessageBinding(models.Model):
    _inherit = "contact.center.message.binding"

    @api.model_create_multi
    def create(self, vals_list):
        bindings = super().create(vals_list)
        for binding in bindings.sudo().filtered(
            lambda item: item.direction == "inbound"
            and item.origin == "provider"
            and item.channel_binding_id.conversation_type == "direct"
            and item.account_id.platform == "whatsapp"
        ):
            try:
                with self.env.cr.savepoint():
                    self.env["marketing.website.whatsapp.correlation"].sudo().with_context(
                        allowed_company_ids=[binding.company_id.id],
                    ).with_company(binding.company_id)._analyze_inbound(binding)
            except OperationalError:
                # Odoo must retry a fresh transaction after deadlock/serialization.
                raise
            except Exception as error:  # Attribution must not discard a customer message.
                _logger.warning(
                    "Website WhatsApp association failed for binding %s (%s)",
                    binding.id, type(error).__name__,
                )
        return bindings
