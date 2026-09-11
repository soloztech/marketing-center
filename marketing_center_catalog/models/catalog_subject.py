from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression


class CatalogSubject(models.Model):
    _name = "marketing.center.catalog.subject"
    _description = "Catalog Subject"
    _order = "name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    kind = fields.Selection(
        [("company", "Company"), ("solution", "Solution / Product")],
        required=True,
        default="solution",
    )
    image = fields.Image(max_width=1920, max_height=1920)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    product_tmpl_ids = fields.Many2many(
        "product.template", string="Product Templates", check_company=True
    )
    product_ids = fields.Many2many(
        "product.product", string="Product Variants", check_company=True
    )
    item_ids = fields.One2many(
        "marketing.center.catalog.item", "subject_id", string="Content"
    )
    information_ids = fields.One2many(
        "marketing.center.catalog.item",
        "subject_id",
        string="Information",
        domain=[("kind", "=", "text")],
    )
    material_ids = fields.One2many(
        "marketing.center.catalog.item",
        "subject_id",
        string="Materials",
        domain=[("kind", "in", ["file", "link"])],
    )
    faq_ids = fields.One2many(
        "marketing.center.catalog.item",
        "subject_id",
        string="FAQ",
        domain=[("kind", "=", "faq")],
    )
    information_count = fields.Integer(compute="_compute_content_counts")
    material_count = fields.Integer(compute="_compute_content_counts")
    faq_count = fields.Integer(compute="_compute_content_counts")

    @api.depends("item_ids.kind", "item_ids.active")
    def _compute_content_counts(self):
        counts = self._catalog_counts()
        for subject in self:
            values = counts.get(subject.id, {})
            subject.information_count = values.get("information", 0)
            subject.material_count = values.get("materials", 0)
            subject.faq_count = values.get("faq", 0)

    def _catalog_counts(self, company_id=None, product_ids=None):
        self.check_access_rights("read")
        self.check_access_rule("read")
        items = self.env["marketing.center.catalog.item"]
        domain = [
            ("subject_id", "in", self.ids),
            ("active", "=", True),
            ("subject_id.active", "=", True),
        ]
        if company_id is not None:
            domain = expression.AND(
                [domain, items._catalog_domain(company_id, product_ids=product_ids)]
            )
        rows = items.read_group(
            domain, ["subject_id", "kind"], ["subject_id", "kind"], lazy=False
        )
        counts = {
            subject.id: {"information": 0, "materials": 0, "faq": 0} for subject in self
        }
        section = {
            "text": "information",
            "faq": "faq",
            "file": "materials",
            "link": "materials",
        }
        for row in rows:
            counts[row["subject_id"][0]][section[row["kind"]]] += row["__count"]
        return counts

    @api.model
    def _catalog_authorized_subject(self, company_id, subject_id):
        self.env["marketing.center.catalog.item"]._catalog_domain(company_id)
        self.check_access_rights("read")
        if type(subject_id) is not int or subject_id <= 0:
            raise ValidationError(_("Select a valid catalog subject."))
        subject = self.browse(subject_id).exists()
        subject.check_access_rule("read")
        if not subject or not subject.active or subject.company_id.id != company_id:
            raise AccessError(_("This subject is unavailable in the selected company."))
        return subject

    def _catalog_payload(self, counts=None):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        if counts is None:
            counts = self._catalog_counts().get(self.id, {})
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "image_url": "/web/image/marketing.center.catalog.subject/%s/image"
            % self.id
            if self.with_context(bin_size=True).image
            else False,
            "counts": counts,
        }

    @api.model
    def catalog_search_subjects(
        self, company_id, product_ids=None, query="", offset=0, limit=24
    ):
        self.check_access_rights("read")
        items = self.env["marketing.center.catalog.item"]
        offset, limit, query = items._catalog_page(offset, limit, query)
        matching_items = items._catalog_domain(
            company_id, product_ids=product_ids, search=query
        )
        domain = [("company_id", "=", company_id), ("active", "=", True)]
        if product_ids:
            products = self.env["product.product"].browse(product_ids)
            domain = expression.AND(
                [
                    domain,
                    expression.OR(
                        [
                            items._catalog_scope_domain("", product)
                            for product in products
                        ]
                    ),
                ]
            )
        if query:
            matching_subjects = items.read_group(
                matching_items, ["subject_id"], ["subject_id"]
            )
            own_fields = (
                "name",
                "product_tmpl_ids.name",
                "product_ids.name",
                "product_ids.default_code",
                "product_tmpl_ids.product_variant_ids.default_code",
            )
            domain = expression.AND(
                [
                    domain,
                    expression.OR(
                        [[(name, "ilike", query)] for name in own_fields]
                        + [
                            [
                                (
                                    "id",
                                    "in",
                                    [row["subject_id"][0] for row in matching_subjects],
                                )
                            ]
                        ]
                    ),
                ]
            )
        subjects = self.search(domain, offset=offset, limit=limit + 1)
        page = subjects[:limit]
        counts = page._catalog_counts(company_id, product_ids=product_ids)
        return {
            "subjects": [
                subject._catalog_payload(counts.get(subject.id)) for subject in page
            ],
            "has_more": len(subjects) > limit,
        }

    @api.constrains("company_id", "product_tmpl_ids", "product_ids")
    def _check_catalog_company(self):
        for subject in self:
            products = subject.product_tmpl_ids | subject.product_ids.product_tmpl_id
            if any(
                product.company_id and product.company_id != subject.company_id
                for product in products
            ):
                raise ValidationError(
                    _(
                        "Linked products must belong to the catalog company or be shared."
                    )
                )
            subject.with_context(active_test=False).item_ids._check_catalog_company()
