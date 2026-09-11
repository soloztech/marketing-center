import base64
import re
from ast import literal_eval

from lxml import etree

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import Form, TransactionCase
from odoo.tools.safe_eval import safe_eval


@tagged("post_install", "-at_install")
class TestContentCatalog(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create(
            {"name": "Other Catalog Company"}
        )
        cls.Subject = cls.env["marketing.center.catalog.subject"]
        cls.Item = cls.env["marketing.center.catalog.item"]
        attribute = cls.env["product.attribute"].create({"name": "Catalog Finish"})
        values = cls.env["product.attribute.value"].create(
            [
                {"name": "A", "attribute_id": attribute.id},
                {"name": "B", "attribute_id": attribute.id},
            ]
        )
        cls.template = cls.env["product.template"].create(
            {
                "name": "Modular Line",
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {
                            "attribute_id": attribute.id,
                            "value_ids": [(6, 0, values.ids)],
                        },
                    )
                ],
            }
        )
        cls.variant, cls.sibling = cls.template.product_variant_ids.sorted("id")
        cls.variant.default_code = "CATALOG-A"
        cls.sibling.default_code = "CATALOG-B"
        cls.unrelated = cls.env["product.product"].create({"name": "Different Product"})
        cls.parent_subject = cls.Subject.create(
            {
                "name": "Modular Line Content",
                "company_id": cls.company.id,
                "product_tmpl_ids": [(6, 0, cls.template.ids)],
            }
        )
        cls.variant_subject = cls.Subject.create(
            {
                "name": "Finish A Only",
                "company_id": cls.company.id,
                "product_ids": [(6, 0, cls.variant.ids)],
            }
        )
        cls.institutional = cls.Subject.create(
            {
                "name": "Example Company",
                "kind": "company",
                "company_id": cls.company.id,
            }
        )
        cls.other_subject = cls.Subject.create(
            {
                "name": "Other Company Information",
                "company_id": cls.other_company.id,
            }
        )
        cls.general = cls.Item.create(
            {
                "name": "General description",
                "subject_id": cls.parent_subject.id,
                "body": "<p>General specifications.</p>",
                "visibility": "shareable",
            }
        )
        cls.restricted = cls.Item.create(
            {
                "name": "Finish A specifications",
                "subject_id": cls.parent_subject.id,
                "body": "<p>Finish A only.</p>",
                "product_ids": [(6, 0, cls.variant.ids)],
            }
        )
        cls.variant_content = cls.Item.create(
            {
                "name": "Variant content",
                "subject_id": cls.variant_subject.id,
                "body": "<p>Variant only.</p>",
            }
        )
        cls.company_content = cls.Item.create(
            {
                "name": "Warranty",
                "kind": "faq",
                "subject_id": cls.institutional.id,
                "question": "What is the warranty?",
                "answer": "<p>See the order terms.</p>",
                "search_aliases": "guarantee coverage",
                "visibility": "shareable",
            }
        )
        cls.other_content = cls.Item.create(
            {
                "name": "Other terms",
                "subject_id": cls.other_subject.id,
                "body": "<p>Other company.</p>",
            }
        )
        cls.file = cls.Item.create(
            {
                "name": "Datasheet",
                "kind": "file",
                "subject_id": cls.parent_subject.id,
                "file_data": base64.b64encode(b"%PDF-1.4\nCatalog fixture\n%%EOF"),
                "file_name": "datasheet.pdf",
                "material_type": "datasheet",
                "visibility": "shareable",
            }
        )
        cls.reader = cls._create_user("catalog-reader", "group_catalog_reader")
        cls.editor = cls._create_user("catalog-editor", "group_catalog_editor")
        cls.outsider = cls._create_user("catalog-outsider", None)

    @classmethod
    def _create_user(cls, login, group):
        group_id = (
            cls.env.ref("marketing_center_catalog." + group).id
            if group
            else cls.env.ref("base.group_user").id
        )
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": login,
                    "login": login,
                    "email": login + "@example.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, [cls.company.id, cls.other_company.id])],
                    "groups_id": [(6, 0, [group_id])],
                }
            )
        )

    def _search(self, products=None, **kwargs):
        items = self.Item.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        return items.search(
            items._catalog_domain(
                self.company.id,
                product_ids=products.ids if products else None,
                **kwargs
            )
        )

    def test_template_includes_variants_but_variant_does_not_include_siblings(self):
        for variant in (self.variant, self.sibling):
            found = self._search(variant)
            self.assertIn(self.general, found)
            self.assertIn(self.company_content, found)
        self.assertIn(self.variant_content, self._search(self.variant))
        self.assertNotIn(self.variant_content, self._search(self.sibling))
        self.assertNotIn(self.general, self._search(self.unrelated))
        self.assertIn(self.company_content, self._search(self.unrelated))

    def test_item_scope_intersects_subject_for_the_same_requested_product(self):
        self.assertIn(self.restricted, self._search(self.variant))
        self.assertNotIn(self.restricted, self._search(self.sibling))
        # Two matches across different products must not imply a match together.
        self.variant_content.product_ids = self.unrelated
        self.assertNotIn(
            self.variant_content, self._search(self.variant | self.unrelated)
        )
        self.assertIn(self.variant_content, self._search())

    def test_template_restriction_and_multiple_variant_parents(self):
        self.parent_subject.product_ids = self.unrelated
        self.general.product_tmpl_ids = self.template
        self.assertIn(self.general, self._search(self.sibling))
        self.assertNotIn(self.general, self._search(self.unrelated))
        self.parent_subject.product_tmpl_ids = False
        self.parent_subject.product_ids = self.variant | self.unrelated
        self.assertIn(self.file, self._search(self.unrelated))
        self.assertNotIn(self.file, self._search(self.sibling))

    def test_search_text_product_reference_aliases_visibility_and_archival(self):
        self.assertIn(self.general, self._search(search="General specifications"))
        self.assertIn(self.general, self._search(search="CATALOG-B"))
        self.assertIn(self.company_content, self._search(search="guarantee"))
        self.assertNotIn(self.restricted, self._search(shareable_only=True))
        self.general.active = False
        self.assertNotIn(self.general, self._search())
        self.parent_subject.active = False
        self.assertNotIn(self.restricted, self._search())
        self.assertNotIn(self.file, self._search())
        self.assertIn(self.company_content, self._search())

    def test_reader_editor_and_active_company_boundaries(self):
        reader_item = self.general.with_user(self.reader)
        self.assertTrue(reader_item.read(["body"]))
        with self.assertRaises(AccessError):
            reader_item.write({"body": "<p>Changed.</p>"})
        with self.assertRaises(AccessError):
            self.Subject.with_user(self.reader).create({"name": "Denied"})
        with self.assertRaises(AccessError):
            self.general.with_user(self.outsider).read(["body"])
        self.general.with_user(self.editor).write({"body": "<p>Saved directly.</p>"})
        self.assertIn("Saved directly", self.general.body)
        self.assertNotIn(self.other_content, self._search())
        with self.assertRaises(AccessError):
            self.other_content.with_user(self.reader).with_context(
                allowed_company_ids=[self.company.id]
            ).read(["body"])
        items = self.Item.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        with self.assertRaises(AccessError):
            items.action_open_catalog(company_id=self.other_company.id)
        both = items.with_context(
            allowed_company_ids=[self.company.id, self.other_company.id]
        )
        self.assertNotIn(
            self.other_content, both.search(both._catalog_domain(self.company.id))
        )

    def test_file_uses_private_attachment_and_native_binary_access(self):
        attachment = self.env["ir.attachment"].search(
            [
                ("res_model", "=", self.Item._name),
                ("res_id", "=", self.file.id),
                ("res_field", "=", "file_data"),
            ]
        )
        self.assertEqual(len(attachment), 1)
        self.assertFalse(attachment.public)
        self.assertFalse(attachment.access_token)
        self.assertEqual(self.file.mimetype, "application/pdf")
        binary = self.env["ir.binary"].with_user(self.reader)
        record = binary._find_record(res_model=self.Item._name, res_id=self.file.id)
        self.assertEqual(record.id, self.file.id)
        # Native binary-field attachments are deliberately not read directly.
        with self.assertRaises(AccessError):
            attachment.with_user(self.reader).check("read")
        with self.assertRaises(AccessError):
            self.env["ir.binary"].with_user(self.outsider)._find_record(
                res_model=self.Item._name, res_id=self.file.id
            )
        with self.assertRaises(AccessError):
            self.env["ir.binary"].with_user(
                self.env.ref("base.public_user")
            )._find_record(res_model=self.Item._name, res_id=self.file.id)
        self.file.subject_id = self.other_subject
        with self.assertRaises(AccessError):
            binary.with_context(allowed_company_ids=[self.company.id])._find_record(
                res_model=self.Item._name, res_id=self.file.id
            )

    def test_plaintext_and_technical_constraints(self):
        self.assertEqual(
            self.company_content._catalog_plaintext(),
            "What is the warranty?\n\nSee the order terms.",
        )
        link = self.Item.create(
            {
                "name": "Video",
                "subject_id": self.institutional.id,
                "kind": "link",
                "url": "https://example.test/video",
            }
        )
        self.assertEqual(link._catalog_plaintext(), "Video\nhttps://example.test/video")
        with self.assertRaises(ValidationError), self.cr.savepoint():
            link.url = "javascript:alert(1)"
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.company_content.answer = "<p><br></p>"
        foreign = self.env["product.product"].create(
            {"name": "Foreign product", "company_id": self.other_company.id}
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.general.product_ids = foreign
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.parent_subject.product_ids = foreign
        with self.assertRaises(AccessError):
            self.Item.action_open_catalog(
                company_id=self.company.id, product_ids=foreign.ids
            )

    def test_library_action_is_removable_and_context_is_clean(self):
        action = self.Item.with_context(
            default_partner_id=42, search_default_customer=1
        ).action_open_catalog(
            company_id=self.company.id,
            product_ids=self.variant.ids,
        )
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "marketing_center_catalog.browser")
        self.assertNotEqual(action.get("target"), "new")
        self.assertEqual(
            action["params"],
            {"company_id": self.company.id, "product_ids": self.variant.ids},
        )
        self.assertNotIn("default_partner_id", action["context"])
        self.assertNotIn("search_default_customer", action["context"])
        self.assertEqual(action["context"]["catalog_product_ids"], self.variant.ids)
        items = self.Item.with_context(
            catalog_company_id=self.company.id, catalog_product_ids=self.sibling.ids
        )
        self.assertNotIn(
            self.variant_content, items.search([("catalog_matches_context", "=", True)])
        )
        self.assertIn(
            self.variant_content,
            items.search([("catalog_matches_context", "=", False)]),
        )
        self.assertIn(
            self.general,
            items.search([("catalog_matches_context", "!=", False)]),
        )
        self.assertIn(
            self.variant_content,
            items.search([("catalog_matches_context", "!=", True)]),
        )
        template_action = self.template.action_open_content_catalog()
        self.assertEqual(
            set(template_action["params"]["product_ids"]),
            set(self.template.product_variant_ids.ids),
        )
        variant_action = self.variant.action_open_content_catalog()
        self.assertEqual(variant_action["params"]["product_ids"], self.variant.ids)

    def test_create_subject_with_inline_text_and_faq(self):
        with Form(self.Subject.with_user(self.editor)) as subject:
            subject.name = "New editable subject"
            with subject.information_ids.new() as content:
                content.name = "Introduction"
                content.body = "<p>Saved with the subject.</p>"
            with subject.faq_ids.new() as faq:
                faq.question = "Where can it be used?"
                faq.answer = "<p>Check the specifications.</p>"
        record = subject.save()
        self.assertEqual(len(record.information_ids), 1)
        self.assertEqual(len(record.faq_ids), 1)
        self.assertEqual(len(record.item_ids), 2)

    def test_subject_has_one_information_page_and_no_freeform_summary(self):
        for field_name in ("summary", "summary_text"):
            self.assertNotIn(field_name, self.Subject._fields)
        payload = self.parent_subject._catalog_payload()
        self.assertNotIn("summary_text", payload)
        self.assertNotIn("summary_html", payload)

        view = self.Subject.with_user(self.editor).get_view(
            self.env.ref("marketing_center_catalog.view_catalog_subject_form").id,
            "form",
        )
        arch = etree.fromstring(view["arch"])
        self.assertEqual(
            arch.xpath(".//notebook/page/@name"),
            ["information", "materials", "products"],
        )
        information_page = arch.xpath(".//page[@name='information']")[0]
        self.assertEqual(
            information_page.xpath("./field/@name"),
            ["information_ids", "faq_ids"],
        )
        self.assertEqual(
            information_page.xpath("./separator/@string"),
            ["Structured Information", "Frequently Asked Questions"],
        )
        for field_name, default_view in (
            ("information_ids", "list"),
            ("faq_ids", "list"),
            ("material_ids", "kanban"),
        ):
            node = arch.xpath(".//field[@name='%s']" % field_name)[0]
            self.assertEqual(node.get("widget"), "catalog_x2many")
            self.assertEqual(
                literal_eval(node.get("options"))["default_view"], default_view
            )
            self.assertIsNotNone(node.find("tree"))
            self.assertIsNotNone(node.find("kanban"))

    def test_browser_and_native_search_find_structured_information_and_faq(self):
        subjects = self.Subject.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        items = self.Item.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        for query, subject, item, section in (
            (
                "General specifications",
                self.parent_subject,
                self.general,
                "information",
            ),
            ("order terms", self.institutional, self.company_content, "faq"),
        ):
            with self.subTest(query=query):
                page = subjects.catalog_search_subjects(self.company.id, query=query)
                self.assertEqual([row["id"] for row in page["subjects"]], subject.ids)
                page = items.catalog_search_items(
                    self.company.id, subject.id, query=query, section=section
                )
                self.assertEqual([row["id"] for row in page["items"]], item.ids)
                for model, view_name, expected in (
                    (subjects, "view_catalog_subject_search", subject),
                    (items, "view_catalog_item_search", item),
                ):
                    view = model.get_view(
                        self.env.ref("marketing_center_catalog." + view_name).id,
                        "search",
                    )
                    arch = etree.fromstring(view["arch"])
                    domain = safe_eval(
                        arch.xpath("./field[@name='name']/@filter_domain")[0],
                        {"self": query},
                    )
                    self.assertEqual(model.search(domain).ids, expected.ids)

    def test_browser_subjects_counts_and_sections_follow_exact_product_scope(self):
        subjects = self.Subject.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        items = self.Item.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id]
        )
        page = subjects.catalog_search_subjects(
            self.company.id, product_ids=self.sibling.ids
        )
        rows = {row["id"]: row for row in page["subjects"]}
        self.assertIn(self.parent_subject.id, rows)
        self.assertIn(self.institutional.id, rows)
        self.assertNotIn(self.variant_subject.id, rows)
        self.assertNotIn(self.other_subject.id, rows)
        self.assertEqual(
            rows[self.parent_subject.id]["counts"],
            {"information": 1, "materials": 1, "faq": 0},
        )
        page = subjects.catalog_search_subjects(self.company.id, query="guarantee")
        self.assertEqual(
            [row["id"] for row in page["subjects"]], self.institutional.ids
        )
        page = subjects.catalog_search_subjects(
            self.company.id,
            product_ids=self.sibling.ids,
            query="Finish A specifications",
        )
        self.assertFalse(page["subjects"])
        self.assertTrue(
            subjects.catalog_search_subjects(self.company.id, limit=1)["has_more"]
        )
        content = items.catalog_search_items(
            self.company.id,
            self.parent_subject.id,
            product_ids=self.sibling.ids,
            section="materials",
        )
        self.assertEqual([row["id"] for row in content["items"]], self.file.ids)
        self.assertEqual(content["items"][0]["preview_kind"], "pdf")
        self.assertTrue(content["items"][0]["preview_url"])
        self.assertTrue(content["items"][0]["shareable"])
        self.assertEqual(content["subject"]["id"], self.parent_subject.id)
        content = items.catalog_search_items(
            self.company.id, self.parent_subject.id, product_ids=self.sibling.ids
        )
        self.assertEqual([row["id"] for row in content["items"]], self.general.ids)
        self.assertIn("General specifications", content["items"][0]["text"])
        self.assertEqual(
            [
                row["id"]
                for row in items.catalog_search_products(
                    self.company.id, query="CATALOG-B"
                )
            ],
            self.sibling.ids,
        )

    def test_browser_api_rejects_other_company_archival_invalid_pages_and_outsiders(
        self,
    ):
        items = self.Item.with_user(self.reader).with_context(
            allowed_company_ids=[self.company.id, self.other_company.id]
        )
        with self.assertRaises(AccessError):
            items.catalog_search_items(self.company.id, self.other_subject.id)
        with self.assertRaises(AccessError):
            self.Subject.with_user(self.outsider).catalog_search_subjects(
                self.company.id
            )
        with self.assertRaises(AccessError):
            self.Item.with_user(self.outsider).catalog_search_items(
                self.company.id, self.parent_subject.id
            )
        with self.assertRaises(AccessError):
            items.catalog_search_products(None)
        with self.assertRaises(ValidationError):
            items.catalog_search_items(
                self.company.id, self.parent_subject.id, section="all"
            )
        with self.assertRaises(ValidationError):
            self.Subject.catalog_search_subjects(self.company.id, limit=10000)
        self.parent_subject.active = False
        with self.assertRaises(AccessError):
            items.catalog_search_items(self.company.id, self.parent_subject.id)

    def test_material_form_only_offers_file_or_link_and_saves_as_material(self):
        self.assertEqual(
            self.Item.fields_get(["material_kind"])["material_kind"]["selection"],
            [("file", "File"), ("link", "Link")],
        )
        with Form(self.Subject.with_user(self.editor)) as subject:
            subject.name = "Materials form fixture"
            with subject.material_ids.new() as material:
                material.name = "External instructions"
                material.material_kind = "link"
                material.url = "https://example.test/instructions"
        record = subject.save()
        self.assertEqual(record.material_ids.kind, "link")
        self.assertFalse(record.faq_ids)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.Item.create(
                {
                    "name": "Invalid material",
                    "subject_id": record.id,
                    "material_kind": "faq",
                    "question": "Question?",
                    "answer": "<p>Answer.</p>",
                }
            )

    def test_kanban_templates_load_every_record_value_including_photo_id(self):
        # A database id in the RPC result is not enough: the kanban parser only
        # builds record.<field> for fields explicitly present in its architecture.
        for model, view_name in (
            (self.Subject, "view_catalog_subject_kanban"),
            (self.Item, "view_catalog_item_kanban"),
        ):
            view = model.with_user(self.reader).get_view(
                self.env.ref("marketing_center_catalog." + view_name).id, "kanban"
            )
            arch = etree.fromstring(view["arch"])
            loaded_fields = set(arch.xpath(".//field/@name"))
            referenced_fields = set(
                re.findall(r"record\.(\w+)\.(?:raw_value|value)", view["arch"])
            )
            self.assertFalse(
                referenced_fields - loaded_fields,
                "Kanban references fields absent from its architecture: %s"
                % (referenced_fields - loaded_fields),
            )
            if model == self.Subject:
                self.assertTrue({"id", "image"} <= loaded_fields)

    def test_preview_type_detects_contents_and_keeps_active_formats_download_only(self):
        self.assertEqual(self.file.preview_kind, "pdf")
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
            "C0lEQVR42mP8/x8AAusB9Wl6P6sAAAAASUVORK5CYII="
        )
        self.file.file_data = base64.b64encode(png)
        self.assertEqual(self.file.preview_kind, "image")
        self.assertEqual(self.file.mimetype, "image/png")
        self.file.file_data = base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        )
        self.assertEqual(self.file.preview_kind, "file")
        self.assertFalse(self.file._catalog_payload()["preview_url"])
        self.assertTrue(self.file._catalog_payload()["download_url"])
        self.file.file_data = base64.b64encode(
            b"<html><script>alert(1)</script></html>"
        )
        self.assertEqual(self.file.preview_kind, "file")
