import base64

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestCatalogDownload(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.reader = users.create(
            {
                "name": "Catalog Download Reader",
                "login": "catalog-download-reader",
                "password": "catalog-download-reader",
                "company_id": cls.env.company.id,
                "company_ids": [(6, 0, cls.env.company.ids)],
                "groups_id": [
                    (
                        6,
                        0,
                        cls.env.ref(
                            "marketing_center_catalog.group_catalog_reader"
                        ).ids,
                    )
                ],
            }
        )
        cls.outsider = users.create(
            {
                "name": "Catalog Download Outsider",
                "login": "catalog-download-outsider",
                "password": "catalog-download-outsider",
                "company_id": cls.env.company.id,
                "company_ids": [(6, 0, cls.env.company.ids)],
                "groups_id": [(6, 0, cls.env.ref("base.group_user").ids)],
            }
        )
        subject = cls.env["marketing.center.catalog.subject"].create(
            {"name": "Download Subject"}
        )
        cls.file = cls.env["marketing.center.catalog.item"].create(
            {
                "name": "Private reference",
                "subject_id": subject.id,
                "kind": "file",
                "file_data": base64.b64encode(b"Catalog private bytes"),
                "file_name": "reference.txt",
            }
        )

    def test_native_download_requires_catalog_read_access(self):
        url = (
            "/web/content/marketing.center.catalog.item/%s/file_data"
            "?download=true&filename_field=file_name" % self.file.id
        )
        self.authenticate(self.reader.login, "catalog-download-reader")
        response = self.url_open(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Catalog private bytes")
        self.assertIn("reference.txt", response.headers["Content-Disposition"])
        self.authenticate(self.outsider.login, "catalog-download-outsider")
        self.assertEqual(self.url_open(url).status_code, 404)
        self.authenticate(None, None)
        self.assertEqual(self.url_open(url).status_code, 404)

    def test_catalog_preview_pdf_is_inline_but_svg_is_download_only(self):
        self.file.write(
            {
                "file_name": "reference.pdf",
                "file_data": base64.b64encode(b"%PDF-1.4\nPreview fixture\n%%EOF"),
            }
        )
        self.authenticate(self.reader.login, "catalog-download-reader")
        preview = "/marketing_center/catalog/item/%s/preview" % self.file.id
        download = "/marketing_center/catalog/item/%s/file" % self.file.id
        response = self.url_open(preview)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")
        self.assertIn("inline", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertIn(
            "attachment", self.url_open(download).headers["Content-Disposition"]
        )
        self.file.write(
            {
                "file_data": base64.b64encode(
                    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
                )
            }
        )
        # A stale/forged computed MIME must never authorize active content inline.
        self.file.write({"mimetype": "application/pdf", "preview_kind": "pdf"})
        response = self.url_open(preview)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.authenticate(self.outsider.login, "catalog-download-outsider")
        self.assertEqual(self.url_open(preview).status_code, 404)
        self.assertEqual(self.url_open(download).status_code, 404)

    def test_catalog_routes_reject_archived_and_other_company_content(self):
        self.authenticate(self.reader.login, "catalog-download-reader")
        url = "/marketing_center/catalog/item/%s/file" % self.file.id
        self.file.active = False
        self.assertEqual(self.url_open(url).status_code, 404)
        self.file.active = True
        other_company = self.env["res.company"].create(
            {"name": "Other preview company"}
        )
        self.file.subject_id.company_id = other_company
        self.assertEqual(self.url_open(url).status_code, 404)
