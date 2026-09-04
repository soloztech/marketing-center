from odoo import _, models
from odoo.exceptions import ValidationError


class Website(models.Model):
    _inherit = "website"

    def write(self, values):
        company_id = values.get("company_id")
        if self and "company_id" in values:
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.env.cr.execute(
                "SELECT id FROM website WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(sorted(self.ids))],
            )
            binding = (
                self.env["marketing.website.ingress.binding"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("website_id", "in", self.ids),
                        ("endpoint_id.company_id", "!=", company_id),
                    ],
                    limit=1,
                )
            )
            if binding:
                raise ValidationError(
                    _(
                        "Archive or reconfigure the website ingress binding before "
                        "moving the website to another company."
                    )
                )
        return super().write(values)
