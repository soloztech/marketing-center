import base64
import binascii
import mimetypes
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext, html_sanitize, is_html_empty
from odoo.tools.mimetypes import guess_mimetype

SECTION_KINDS = {
    "information": ("text",),
    "materials": ("file", "link"),
    "faq": ("faq",),
}
PREVIEW_MIMETYPES = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "application/pdf": "pdf",
    "video/mp4": "video",
    "video/webm": "video",
    "video/ogg": "video",
}


class CatalogItem(models.Model):
    _name = "marketing.center.catalog.item"
    _description = "Catalog Content"
    _order = "subject_id, sequence, name, id"
    _check_company_auto = True

    name = fields.Char(string="Title", required=True, index=True)
    sequence = fields.Integer(default=10)
    subject_id = fields.Many2one(
        "marketing.center.catalog.subject",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="subject_id.company_id", store=True, index=True
    )
    active = fields.Boolean(default=True)
    kind = fields.Selection(
        [("text", "Text"), ("faq", "FAQ"), ("file", "File"), ("link", "Link")],
        required=True,
        default="text",
        index=True,
    )
    body = fields.Html(string="Text")
    question = fields.Char()
    answer = fields.Html()
    search_aliases = fields.Char(string="Alternative Search Terms")
    file_data = fields.Binary(string="File", attachment=True)
    file_name = fields.Char(string="Filename")
    mimetype = fields.Char(compute="_compute_media_preview", store=True)
    preview_kind = fields.Selection(
        [
            ("image", "Image"),
            ("pdf", "PDF"),
            ("video", "Video"),
            ("file", "File"),
            ("text", "Text"),
            ("faq", "FAQ"),
            ("link", "Link"),
        ],
        compute="_compute_media_preview",
        store=True,
    )
    material_kind = fields.Selection(
        [("file", "File"), ("link", "Link")],
        string="Material Format",
        compute="_compute_material_kind",
        inverse="_inverse_material_kind",
    )
    url = fields.Char(string="External Link")
    material_type = fields.Selection(
        [
            ("photo", "Photo"),
            ("video", "Video"),
            ("catalog", "Catalog"),
            ("datasheet", "Datasheet"),
            ("manual", "Manual"),
            ("other", "Other"),
        ],
        string="Material Category",
        default="other",
    )
    visibility = fields.Selection(
        [("internal", "Internal Use"), ("shareable", "Can Share with Customer")],
        required=True,
        default="internal",
        index=True,
    )
    product_tmpl_ids = fields.Many2many(
        "product.template",
        string="Only These Product Templates",
        check_company=True,
        help="Leave both product restrictions empty to follow the subject's products.",
    )
    product_ids = fields.Many2many(
        "product.product",
        string="Only These Variants",
        check_company=True,
    )
    applicability = fields.Char(compute="_compute_applicability")
    catalog_matches_context = fields.Boolean(
        store=False, search="_search_catalog_matches_context"
    )

    @api.model
    def _catalog_detect_mimetype(self, content, filename=None):
        detected = guess_mimetype(content, default="application/octet-stream")
        # Odoo's fallback detector lacks these standard container signatures.
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content[4:8] == b"ftyp" and content[8:12] in (
            b"isom",
            b"iso2",
            b"mp41",
            b"mp42",
            b"avc1",
            b"M4V ",
        ):
            return "video/mp4"
        if content.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in content[:1024]:
            return "video/webm"
        if content.startswith(b"OggS") and b"theora" in content[:4096]:
            return "video/ogg"
        if detected == "application/octet-stream":
            by_name = mimetypes.guess_type(filename or "")[0]
            # Never authorize inline rendering using only an untrusted extension.
            if by_name and by_name not in PREVIEW_MIMETYPES:
                return by_name
        return detected or "application/octet-stream"

    @api.depends("file_name", "file_data", "kind")
    def _compute_media_preview(self):
        for item in self:
            content = b""
            if item.kind == "file" and item.file_data:
                try:
                    content = base64.b64decode(item.file_data, validate=True)
                except (ValueError, binascii.Error):
                    # A bin_size read can return a human-readable size. Use the
                    # native binary storage in that case, without switching the
                    # item into a second binary cache during an upload write.
                    attachment = (
                        self.env["ir.attachment"]
                        .sudo()
                        .search(
                            [
                                ("res_model", "=", item._name),
                                ("res_id", "=", item.id),
                                ("res_field", "=", "file_data"),
                            ],
                            limit=1,
                        )
                        if item.id
                        else self.env["ir.attachment"]
                    )
                    content = attachment.raw or b""
            item.mimetype = item._catalog_detect_mimetype(content, item.file_name)
            item.preview_kind = (
                PREVIEW_MIMETYPES.get(item.mimetype, "file")
                if item.kind == "file"
                else item.kind
            )

    @api.depends("kind")
    def _compute_material_kind(self):
        for item in self:
            item.material_kind = item.kind if item.kind in ("file", "link") else False

    def _inverse_material_kind(self):
        for item in self:
            if item.material_kind:
                item.kind = item.material_kind

    @api.onchange("material_kind")
    def _onchange_material_kind(self):
        self._inverse_material_kind()

    @api.onchange("kind", "question")
    def _onchange_faq_title(self):
        if self.kind == "faq" and not self.name:
            self.name = self.question

    @api.model
    def _catalog_material_values(self, values):
        values = dict(values)
        material_kind = values.pop("material_kind", False)
        if material_kind:
            if material_kind not in ("file", "link"):
                raise ValidationError(_("Materials can only be files or links."))
            values["kind"] = material_kind
        return values

    @api.model_create_multi
    def create(self, values_list):
        prepared = []
        for values in values_list:
            values = self._catalog_material_values(values)
            kind = values.get("kind", self.env.context.get("default_kind", "text"))
            if kind == "faq" and not values.get("name"):
                question = values.get(
                    "question", self.env.context.get("default_question")
                )
                if not question:
                    raise ValidationError(_("Enter both the FAQ question and answer."))
                values["name"] = question
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        return super().write(self._catalog_material_values(values))

    @api.depends(
        "product_tmpl_ids",
        "product_ids",
        "subject_id.product_tmpl_ids",
        "subject_id.product_ids",
    )
    def _compute_applicability(self):
        for item in self:
            subject_scope = ", ".join(
                item.subject_id.product_tmpl_ids.mapped("display_name")
                + item.subject_id.product_ids.mapped("display_name")
            ) or _("All products / company information")
            restriction = ", ".join(
                item.product_tmpl_ids.mapped("display_name")
                + item.product_ids.mapped("display_name")
            )
            item.applicability = (
                _(
                    "Subject: %(subject)s; only: %(products)s",
                    subject=subject_scope,
                    products=restriction,
                )
                if restriction
                else subject_scope
            )

    @api.constrains("company_id", "subject_id", "product_tmpl_ids", "product_ids")
    def _check_catalog_company(self):
        for item in self:
            products = item.product_tmpl_ids | item.product_ids.product_tmpl_id
            if any(
                product.company_id and product.company_id != item.company_id
                for product in products
            ):
                raise ValidationError(
                    _(
                        "Restricted products must belong to the catalog company or be shared."
                    )
                )

    @api.constrains(
        "kind", "body", "question", "answer", "file_data", "file_name", "url"
    )
    def _check_content(self):
        for item in self:
            if item.kind == "text" and is_html_empty(item.body):
                raise ValidationError(_("Enter the content text."))
            if item.kind == "faq" and (not item.question or is_html_empty(item.answer)):
                raise ValidationError(_("Enter both the FAQ question and answer."))
            if item.kind == "file" and (not item.file_data or not item.file_name):
                raise ValidationError(_("Upload a file and provide its filename."))
            if item.kind == "link":
                try:
                    parts = urlsplit(item.url or "")
                    valid = parts.scheme.lower() in ("http", "https") and bool(
                        parts.netloc
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError(_("Use a complete http:// or https:// link."))

    @api.model
    def _catalog_scope_domain(self, prefix, product):
        return expression.OR(
            [
                [
                    (prefix + "product_tmpl_ids", "=", False),
                    (prefix + "product_ids", "=", False),
                ],
                [(prefix + "product_tmpl_ids", "in", product.product_tmpl_id.ids)],
                [(prefix + "product_ids", "in", product.ids)],
            ]
        )

    @api.model
    def _catalog_domain(
        self, company_id, product_ids=None, search=None, shareable_only=False
    ):
        """Build the current library domain. None/[] products means no product filter.

        Each variant must match both the subject and optional item restriction.
        A product template includes all its variants; a single variant never
        includes siblings. The caller still needs normal ORM read access.
        """
        self.check_access_rights("read")
        if type(company_id) is not int or company_id not in self.env.companies.ids:
            raise AccessError(_("The catalog company must be an active company."))
        domain = [
            ("company_id", "=", company_id),
            ("active", "=", True),
            ("subject_id.active", "=", True),
        ]
        if shareable_only:
            domain.append(("visibility", "=", "shareable"))
        if product_ids:
            if not isinstance(product_ids, (list, tuple)) or any(
                type(value) is not int or value <= 0 for value in product_ids
            ):
                raise ValidationError(_("Select valid product variants."))
            products = self.env["product.product"].browse(product_ids).exists()
            products.check_access_rights("read")
            products.check_access_rule("read")
            if set(products.ids) != set(product_ids):
                raise ValidationError(
                    _("One or more selected products no longer exist.")
                )
            if any(
                product.company_id and product.company_id.id != company_id
                for product in products
            ):
                raise AccessError(
                    _(
                        "Selected products must belong to the catalog company or be shared."
                    )
                )
            domain = expression.AND(
                [
                    domain,
                    expression.OR(
                        [
                            expression.AND(
                                [
                                    self._catalog_scope_domain("subject_id.", product),
                                    self._catalog_scope_domain("", product),
                                ]
                            )
                            for product in products
                        ]
                    ),
                ]
            )
        if search:
            search_fields = [
                "name",
                "body",
                "question",
                "answer",
                "search_aliases",
                "subject_id.name",
                "product_tmpl_ids.name",
                "product_ids.name",
                "product_ids.default_code",
                "product_tmpl_ids.product_variant_ids.default_code",
                "subject_id.product_tmpl_ids.name",
                "subject_id.product_ids.name",
                "subject_id.product_ids.default_code",
                "subject_id.product_tmpl_ids.product_variant_ids.default_code",
            ]
            domain = expression.AND(
                [
                    domain,
                    expression.OR(
                        [[(field, "ilike", search)] for field in search_fields]
                    ),
                ]
            )
        return domain

    @api.model
    def _catalog_page(self, offset, limit, query):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValidationError(_("Use a valid catalog page (1 to 100 records)."))
        if not isinstance(query, str) or len(query) > 500:
            raise ValidationError(_("Use a search text of at most 500 characters."))
        return offset, limit, query.strip()

    def _catalog_payload(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "preview_kind": self.preview_kind,
            "body_html": html_sanitize(
                self.body or "", sanitize_attributes=True, sanitize_style=True
            ),
            "answer_html": html_sanitize(
                self.answer or "", sanitize_attributes=True, sanitize_style=True
            ),
            "text": self._catalog_plaintext(),
            "question": self.question or "",
            "file_name": self.file_name or "",
            "mimetype": self.mimetype or "application/octet-stream",
            "preview_url": "/marketing_center/catalog/item/%s/preview" % self.id
            if self.kind == "file" and self.preview_kind != "file"
            else False,
            "download_url": "/marketing_center/catalog/item/%s/file" % self.id
            if self.kind == "file"
            else False,
            "url": self.url or "",
            "visibility": self.visibility,
            "shareable": self.visibility == "shareable",
            "applicability": self.applicability,
            "material_type": self.material_type or "other",
        }

    @api.model
    def catalog_search_items(
        self,
        company_id,
        subject_id,
        product_ids=None,
        query="",
        section="information",
        offset=0,
        limit=24,
    ):
        offset, limit, query = self._catalog_page(offset, limit, query)
        if section not in SECTION_KINDS:
            raise ValidationError(_("Choose Information, Materials or FAQ."))
        domain = self._catalog_domain(company_id, product_ids=product_ids, search=query)
        subject = self.env[
            "marketing.center.catalog.subject"
        ]._catalog_authorized_subject(company_id, subject_id)
        domain = expression.AND(
            [
                domain,
                [
                    ("subject_id", "=", subject.id),
                    ("kind", "in", SECTION_KINDS[section]),
                ],
            ]
        )
        items = self.search(domain, offset=offset, limit=limit + 1)
        counts = subject._catalog_counts(company_id, product_ids=product_ids)
        return {
            "items": [item._catalog_payload() for item in items[:limit]],
            "has_more": len(items) > limit,
            "subject": subject._catalog_payload(counts.get(subject.id)),
        }

    @api.model
    def catalog_search_products(self, company_id, query="", limit=20):
        self._catalog_domain(company_id)
        _offset, limit, query = self._catalog_page(0, limit, query)
        products = self.env["product.product"].search(
            expression.AND(
                [
                    [("company_id", "in", [False, company_id])],
                    expression.OR(
                        [[("name", "ilike", query)], [("default_code", "ilike", query)]]
                    )
                    if query
                    else [],
                ]
            ),
            limit=limit,
        )
        return [
            {"id": product.id, "name": product.display_name} for product in products
        ]

    def _catalog_plaintext(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.kind == "faq":
            return "%s\n\n%s" % (
                self.question or self.name,
                html2plaintext(self.answer or "").strip(),
            )
        if self.kind == "link":
            return "%s\n%s" % (self.name, self.url)
        if self.kind == "file":
            return self.name
        return html2plaintext(self.body or "").strip()

    @api.model
    def _search_catalog_matches_context(self, operator, value):
        domain = self._catalog_domain(
            self.env.context.get("catalog_company_id") or self.env.company.id,
            product_ids=self.env.context.get("catalog_product_ids"),
        )
        return (
            domain
            if (operator == "=" and value) or (operator == "!=" and not value)
            else ["!"] + expression.normalize_domain(domain)
        )

    @api.model
    def action_open_catalog(self, company_id=None, product_ids=None):
        company_id = company_id or self.env.company.id
        # Validate the requested products even though the UI filter is removable.
        self._catalog_domain(company_id, product_ids=product_ids)
        context = {
            key: value
            for key, value in self.env.context.items()
            if not key.startswith(("default_", "search_default_"))
            and key not in ("active_model", "active_id", "active_ids")
        }
        context.update(
            catalog_company_id=company_id,
            catalog_product_ids=product_ids or [],
            create=False,
            default_company_id=company_id,
        )
        return {
            "type": "ir.actions.client",
            "tag": "marketing_center_catalog.browser",
            "name": _("Content Catalog"),
            "context": context,
            "params": {"company_id": company_id, "product_ids": product_ids or []},
        }
