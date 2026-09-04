from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class MarketingWebsiteIngressBinding(models.Model):
    _name = "marketing.website.ingress.binding"
    _description = "Marketing Website Ingress Binding"
    _order = "company_id, website_id, id"
    _rec_name = "website_id"
    _check_company_auto = True

    active = fields.Boolean(default=True, index=True)
    website_id = fields.Many2one(
        "website",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="website_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
    )
    endpoint_public_ref = fields.Char(
        related="endpoint_id.public_ref",
        string="Endpoint public reference",
        readonly=True,
    )
    endpoint_config_revision = fields.Integer(
        related="endpoint_id.config_revision",
        string="Endpoint configuration revision",
        readonly=True,
    )
    action_ids = fields.One2many(
        "marketing.website.action",
        "binding_id",
        string="Website actions",
    )

    _sql_constraints = [
        (
            "website_unique",
            "unique(website_id)",
            "A website can have only one marketing ingress binding.",
        ),
    ]

    @api.model
    def _fence_configuration_rows(self, website_ids, endpoint_ids):
        """Write one MVCC row version for every cross-model configuration root.

        Odoo 16 runs at REPEATABLE READ. A waiting ``SELECT FOR UPDATE`` alone
        can continue with a stale snapshot when the lock holder did not update
        that row. The no-op updates below deliberately create row versions so a
        conflicting configuration transaction is retried from a fresh snapshot.
        """

        website_ids = tuple(sorted(set(website_ids)))
        endpoint_ids = tuple(sorted(set(endpoint_ids)))
        websites = self.env["website"].browse(website_ids).exists()
        endpoints = (
            self.env["marketing.web.ingress.endpoint"].browse(endpoint_ids).exists()
        )
        if set(websites.ids) != set(website_ids) or set(endpoints.ids) != set(
            endpoint_ids
        ):
            raise ValidationError(
                _("The Website ingress configuration is unavailable.")
            )
        websites.check_access_rights("read")
        websites.check_access_rule("read")
        endpoints.check_access_rights("read")
        endpoints.check_access_rule("read")
        for website_id in website_ids:
            self.env.cr.execute(
                "UPDATE website SET write_date = write_date "
                "WHERE id = %s RETURNING id",
                [website_id],
            )
        self.env["website"].browse(website_ids).invalidate_recordset(
            ["company_id", "write_date"]
        )
        for endpoint_id in endpoint_ids:
            self.env.cr.execute(
                "UPDATE marketing_web_ingress_endpoint SET write_date = write_date "
                "WHERE id = %s RETURNING id",
                [endpoint_id],
            )
        self.env["marketing.web.ingress.endpoint"].browse(
            endpoint_ids
        ).invalidate_recordset(["active", "company_id", "write_date"])

    def _fence_effective_configuration(self):
        self.check_access_rights("read")
        self.check_access_rule("read")
        self._fence_configuration_rows([], self.mapped("endpoint_id").ids)

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access_rights("create")
        self._fence_configuration_rows(
            [
                values.get("website_id")
                for values in vals_list
                if values.get("website_id")
            ],
            [
                values.get("endpoint_id")
                for values in vals_list
                if values.get("endpoint_id")
            ],
        )
        return super().create(vals_list)

    def write(self, values):
        self.check_access_rights("write")
        self.check_access_rule("write")
        website_ids = self.mapped("website_id").ids
        endpoint_ids = self.mapped("endpoint_id").ids
        if values.get("website_id"):
            website_ids.append(values["website_id"])
        if values.get("endpoint_id"):
            endpoint_ids.append(values["endpoint_id"])
        self._fence_configuration_rows(website_ids, endpoint_ids)
        return super().write(values)

    @api.constrains("active", "website_id", "endpoint_id")
    def _check_active_endpoint(self):
        for binding in self:
            if binding.endpoint_id.company_id != binding.website_id.company_id:
                raise ValidationError(
                    _("The website and ingress endpoint must use the same company.")
                )
            if binding.active and not binding.endpoint_id.active:
                raise ValidationError(
                    _("An active website binding requires an active endpoint.")
                )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(
            _("Website ingress bindings must be archived instead of deleted.")
        )
