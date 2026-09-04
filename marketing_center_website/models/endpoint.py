from odoo import _, models
from odoo.exceptions import ValidationError


class MarketingWebIngressEndpoint(models.Model):
    _inherit = "marketing.web.ingress.endpoint"

    def write(self, values):
        if self and "active" in values and not values["active"]:
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.env.cr.execute(
                "SELECT id FROM marketing_web_ingress_endpoint "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(sorted(self.ids))],
            )
            binding = (
                self.env["marketing.website.ingress.binding"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("endpoint_id", "in", self.ids),
                    ],
                    limit=1,
                )
            )
            if binding:
                raise ValidationError(
                    _(
                        "Archive the active website ingress binding before "
                        "deactivating its endpoint."
                    )
                )
        return super().write(values)
