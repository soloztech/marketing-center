import base64
import uuid
from io import BytesIO

from PIL import Image

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged


class CatalogBridgeSetup:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create(
            {"name": "Other Catalog Company"}
        )
        groups = cls.env.ref(
            "contact_center_base.group_contact_center_agent"
        ) | cls.env.ref("marketing_center_catalog.group_catalog_reader")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Catalog Agent",
                    "login": "catalog-" + str(uuid.uuid4()),
                    "password": "catalog-test-password",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, (cls.company | cls.other_company).ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Catalog Test Inbox",
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "company_id": cls.company.id,
                "access_user_ids": [(6, 0, cls.agent.ids)],
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Catalog Test Guest"})
        identity = cls.env["contact.center.identity"].create(
            {
                "company_id": cls.company.id,
                "name": "Catalog Test Customer",
                "mail_guest_id": guest.id,
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account, identity=identity, guest_ids=guest.ids
        )
        cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        image = BytesIO()
        Image.new("RGB", (4, 4), "white").save(image, format="PNG")
        cls.image_data = base64.b64encode(image.getvalue())
        cls.subject = cls.env["marketing.center.catalog.subject"].create(
            {
                "name": "Company Information",
                "image": cls.image_data,
                "kind": "company",
                "company_id": cls.company.id,
            }
        )
        cls.text = cls.env["marketing.center.catalog.item"].create(
            {
                "name": "Warranty",
                "subject_id": cls.subject.id,
                "kind": "faq",
                "question": "How does the warranty work?",
                "answer": "<p>Contact support.</p>",
                "visibility": "shareable",
            }
        )
        cls.file = cls.env["marketing.center.catalog.item"].create(
            {
                "name": "Datasheet",
                "subject_id": cls.subject.id,
                "kind": "file",
                "file_name": "datasheet.pdf",
                "file_data": base64.b64encode(b"%PDF-1.4\nCatalog test\n%%EOF"),
                "visibility": "shareable",
            }
        )
        cls.internal = cls.env["marketing.center.catalog.item"].create(
            {
                "name": "Internal instruction",
                "subject_id": cls.subject.id,
                "kind": "text",
                "body": "<p>For staff only.</p>",
                "visibility": "internal",
            }
        )
        cls.foreign_subject = cls.env["marketing.center.catalog.subject"].create(
            {
                "name": "Foreign subject",
                "image": cls.image_data,
                "kind": "company",
                "company_id": cls.other_company.id,
            }
        )
        cls.foreign = cls.env["marketing.center.catalog.item"].create(
            {
                "name": "Other company text",
                "subject_id": cls.foreign_subject.id,
                "kind": "text",
                "body": "<p>Other company.</p>",
                "visibility": "shareable",
            }
        )
        cls.api = (
            cls.env["contact.center.ui.api"]
            .with_user(cls.agent)
            .with_context(allowed_company_ids=(cls.company | cls.other_company).ids)
        )


@tagged("post_install", "-at_install")
class TestCatalogBridge(CatalogBridgeSetup, TransactionCase):
    def test_search_matches_conversation_company_with_two_active_companies(self):
        page = self.api.search_catalog_content(self.channel.id)
        self.assertEqual(
            {item["id"] for item in page["items"]},
            {self.text.id, self.file.id, self.internal.id},
        )
        self.assertNotIn(self.foreign.id, [item["id"] for item in page["items"]])
        internal = next(
            item for item in page["items"] if item["id"] == self.internal.id
        )
        self.assertFalse(internal["shareable"])
        self.assertIn("For staff only.", internal["text"])

    def test_subject_cards_and_sections_use_shared_browser_contract(self):
        page = self.api.search_catalog_subjects(self.channel.id)
        self.assertEqual(
            {subject["id"] for subject in page["subjects"]}, {self.subject.id}
        )
        self.assertEqual(
            page["subjects"][0]["image_url"],
            f"/contact_center/catalog/{self.channel.id}/subject/{self.subject.id}/image",
        )
        page = self.api.search_catalog_content(
            self.channel.id, subject_id=self.subject.id, section="materials"
        )
        self.assertEqual([item["id"] for item in page["items"]], [self.file.id])
        self.assertEqual(page["items"][0]["preview_kind"], "pdf")
        self.assertEqual(
            page["items"][0]["preview_url"],
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/preview",
        )
        self.assertEqual(
            page["items"][0]["download_url"],
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/download",
        )
        faq = self.api.search_catalog_content(
            self.channel.id, subject_id=self.subject.id, section="faq"
        )
        self.assertEqual([item["id"] for item in faq["items"]], [self.text.id])

    def test_subject_drilldown_and_image_require_exact_conversation_company(self):
        with self.assertRaises(AccessError):
            self.api.search_catalog_content(
                self.channel.id,
                subject_id=self.foreign_subject.id,
                section="information",
            )
        with self.assertRaises(AccessError):
            self.api._catalog_subject(self.channel.id, self.foreign_subject.id)
        self.subject.active = False
        with self.assertRaises(AccessError):
            self.api._catalog_subject(self.channel.id, self.subject.id)

    def test_prepare_text_and_file_does_not_send_or_change_master_attachment(self):
        before = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel"), ("res_id", "=", self.channel.id)]
        )
        attachment = self.env["ir.attachment"].search(
            [
                ("res_model", "=", self.file._name),
                ("res_id", "=", self.file.id),
                ("res_field", "=", "file_data"),
            ]
        )
        original = attachment.read(["res_model", "res_id", "checksum", "public"])
        result = self.api.prepare_catalog_content(
            self.channel.id, [self.text.id, self.file.id]
        )
        self.assertIn("Contact support.", result["items"][0]["text"])
        self.assertEqual(
            result["items"][1]["file"]["url"],
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/file",
        )
        self.assertEqual(
            attachment.read(["res_model", "res_id", "checksum", "public"]), original
        )
        self.assertEqual(
            self.env["mail.message"].search_count(
                [("model", "=", "mail.channel"), ("res_id", "=", self.channel.id)]
            ),
            before,
        )
        self.assertFalse(attachment.public)

    def test_internal_foreign_and_archived_content_cannot_be_shared(self):
        for item in (self.internal, self.foreign):
            with self.subTest(item=item.id), self.assertRaises(AccessError):
                self.api.prepare_catalog_content(self.channel.id, [item.id])
        self.text.active = False
        with self.assertRaises(AccessError):
            self.api.prepare_catalog_content(self.channel.id, [self.text.id])
        self.subject.active = False
        with self.assertRaises(AccessError):
            self.api._catalog_item(self.channel.id, self.file.id)

    def test_preview_allows_internal_but_share_route_requires_shareable(self):
        self.file.visibility = "internal"
        self.assertEqual(
            self.api._catalog_item(self.channel.id, self.file.id, shareable=False)[1],
            self.file,
        )
        with self.assertRaises(AccessError):
            self.api._catalog_item(self.channel.id, self.file.id)

    def test_catalog_permission_and_channel_authorization_are_required(self):
        self.agent.groups_id -= self.env.ref(
            "marketing_center_catalog.group_catalog_reader"
        )
        with self.assertRaises(AccessError):
            self.api.search_catalog_content(self.channel.id)
        with self.assertRaises(AccessError):
            self.api.prepare_catalog_content(self.channel.id, [self.file.id])

    def test_catalog_reader_without_channel_access_cannot_open_cards_or_files(self):
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Catalog outsider",
                    "login": str(uuid.uuid4()),
                    "company_id": self.company.id,
                    "company_ids": [(6, 0, self.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            (
                                self.env.ref(
                                    "contact_center_base.group_contact_center_agent"
                                )
                                | self.env.ref(
                                    "marketing_center_catalog.group_catalog_reader"
                                )
                            ).ids,
                        )
                    ],
                }
            )
        )
        api = self.api.with_user(outsider).with_context(
            allowed_company_ids=self.company.ids
        )
        for call in (
            lambda: api.search_catalog_subjects(self.channel.id),
            lambda: api._catalog_subject(self.channel.id, self.subject.id),
            lambda: api._catalog_item(self.channel.id, self.file.id, shareable=False),
        ):
            with self.assertRaises(AccessError):
                call()

    def test_bad_filter_and_selection_are_rejected(self):
        for ids in ([], [True], ["1"], [self.file.id] * 51):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                self.api.prepare_catalog_content(self.channel.id, ids)
        for kwargs in ({"query": ["name"]}, {"kind": "res.users"}, {"offset": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.api.search_catalog_content(self.channel.id, **kwargs)


@tagged("post_install", "-at_install")
class TestCatalogDownload(CatalogBridgeSetup, HttpCase):
    def test_authenticated_download_preserves_master_and_blocks_internal_sharing(self):
        self.authenticate(self.agent.login, "catalog-test-password")
        self.opener.cookies.set("cids", f"{self.company.id},{self.other_company.id}")
        original = self.file.with_context(bin_size=False).file_data
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/file"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, base64.b64decode(original))
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(self.file.with_context(bin_size=False).file_data, original)
        self.file.visibility = "internal"
        self.file.flush_recordset()
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/file"
        )
        self.assertEqual(response.status_code, 404)
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/preview"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline;"))
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/download"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["Content-Disposition"].startswith("attachment;")
        )
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.foreign.id}/file"
        )
        self.assertEqual(response.status_code, 404)

    def test_subject_image_is_private_and_exactly_scoped_to_channel(self):
        self.authenticate(self.agent.login, "catalog-test-password")
        self.opener.cookies.set("cids", f"{self.company.id},{self.other_company.id}")
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/subject/{self.subject.id}/image"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("image/"))
        self.assertIn("private", response.headers["Cache-Control"])
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/subject/{self.foreign_subject.id}/image"
        )
        self.assertEqual(response.status_code, 404)

    def test_unsupported_active_content_is_downloaded_instead_of_embedded(self):
        self.file.write(
            {
                "file_name": "document.html",
                "file_data": base64.b64encode(
                    b"<script>document.title='unsafe'</script>"
                ),
            }
        )
        self.file.flush_recordset()
        self.authenticate(self.agent.login, "catalog-test-password")
        response = self.url_open(
            f"/contact_center/catalog/{self.channel.id}/item/{self.file.id}/preview"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["Content-Disposition"].startswith("attachment;")
        )
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
