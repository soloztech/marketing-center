from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"]["content_catalog"] = self.env[
            "marketing.center.catalog.item"
        ].check_access_rights("read", raise_exception=False)
        return result

    def _catalog_channel(self, channel_id):
        channel, _member = self._authorized_channel(channel_id)
        self.env["marketing.center.catalog.subject"].check_access_rights("read")
        self.env["marketing.center.catalog.item"].check_access_rights("read")
        return channel

    def _catalog_ids(self, values, limit=50):
        if not isinstance(values, list) or len(values) > limit:
            raise ValidationError(_("Select up to %s records.") % limit)
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValidationError(_("Invalid catalog selection."))
        return list(dict.fromkeys(values))

    def _catalog_subject(self, channel_id, subject_id):
        channel = self._catalog_channel(channel_id)
        subject_id = self._positive_id(subject_id, _("subject ID"))
        subject = self.env["marketing.center.catalog.subject"].search(
            [
                ("id", "=", subject_id),
                ("active", "=", True),
                ("company_id", "=", channel.contact_center_company_id.id),
            ],
            limit=1,
        )
        if not subject:
            raise AccessError(_("This subject is unavailable for this conversation."))
        return channel, subject

    def _catalog_channel_subject_card(self, subject, channel):
        if subject.get("image_url"):
            subject["image_url"] = "/contact_center/catalog/%s/subject/%s/image" % (
                channel.id,
                subject["id"],
            )
        return subject

    @api.model
    def search_catalog_subjects(
        self, channel_id, query="", product_ids=None, offset=0, limit=24
    ):
        channel = self._catalog_channel(channel_id)
        page = self.env["marketing.center.catalog.subject"].catalog_search_subjects(
            company_id=channel.contact_center_company_id.id,
            product_ids=product_ids,
            query=query,
            offset=offset,
            limit=limit,
        )
        page["channel_id"] = channel.id
        page["subjects"] = [
            self._catalog_channel_subject_card(subject, channel)
            for subject in page["subjects"]
        ]
        return page

    def _catalog_item(self, channel_id, item_id, *, shareable=True):
        channel = self._catalog_channel(channel_id)
        item_id = self._positive_id(item_id, _("content ID"))
        model = self.env["marketing.center.catalog.item"]
        item = model.search(
            expression.AND(
                [
                    model._catalog_domain(
                        channel.contact_center_company_id.id,
                        shareable_only=shareable,
                    ),
                    [("id", "=", item_id)],
                ]
            ),
            limit=1,
        )
        if not item:
            raise AccessError(_("This content is unavailable for this conversation."))
        item.subject_id.check_access_rule("read")
        return channel, item

    def _catalog_serialize_item(self, item, channel):
        return {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "subject": item.subject_id.name,
            "shareable": item.visibility == "shareable",
            "text": item._catalog_plaintext(),
            "scope": [item.applicability],
            "filename": item.file_name or item.name,
            "mimetype": item.mimetype or "application/octet-stream",
            "preview_url": (
                "/contact_center/catalog/%s/item/%s/preview" % (channel.id, item.id)
                if item.kind == "file"
                else False
            ),
        }

    @api.model
    def search_catalog_content(
        self,
        channel_id,
        query="",
        product_ids=None,
        kind="",
        subject_kind="",
        offset=0,
        subject_id=None,
        section="",
        limit=24,
    ):
        channel = self._catalog_channel(channel_id)
        if subject_id is not None:
            _channel, subject = self._catalog_subject(channel_id, subject_id)
            page = self.env["marketing.center.catalog.item"].catalog_search_items(
                company_id=channel.contact_center_company_id.id,
                subject_id=subject.id,
                product_ids=product_ids,
                query=query,
                section=section or "information",
                offset=offset,
                limit=limit,
            )
            page["channel_id"] = channel.id
            if page.get("subject"):
                self._catalog_channel_subject_card(page["subject"], channel)
            for item in page["items"]:
                if item["kind"] == "file":
                    base_url = "/contact_center/catalog/%s/item/%s" % (
                        channel.id,
                        item["id"],
                    )
                    item["preview_url"] = base_url + "/preview"
                    item["download_url"] = base_url + "/download"
            return page
        if not isinstance(query, str) or len(query) > 256:
            raise ValidationError(_("Enter a search of up to 256 characters."))
        if kind not in ("", "text", "faq", "file", "link"):
            raise ValidationError(_("Invalid content type."))
        if subject_kind not in ("", "company", "solution"):
            raise ValidationError(_("Invalid subject type."))
        if type(offset) is not int or offset < 0 or offset > 10000:
            raise ValidationError(_("Invalid result page."))
        product_ids = self._catalog_ids(product_ids or [], limit=100)
        model = self.env["marketing.center.catalog.item"]
        domain = model._catalog_domain(
            channel.contact_center_company_id.id, product_ids=product_ids, search=query
        )
        if kind:
            domain = expression.AND([domain, [("kind", "=", kind)]])
        if subject_kind:
            domain = expression.AND([domain, [("subject_id.kind", "=", subject_kind)]])
        items = model.search(domain, offset=offset, limit=41, order="name, id")
        items.subject_id.check_access_rule("read")
        return {
            "channel_id": channel.id,
            "items": [
                self._catalog_serialize_item(item, channel) for item in items[:40]
            ],
            "has_more": len(items) > 40,
        }

    @api.model
    def search_catalog_products(self, channel_id, query=""):
        channel = self._catalog_channel(channel_id)
        if not isinstance(query, str) or len(query) > 256:
            raise ValidationError(_("Enter a search of up to 256 characters."))
        products = self.env["product.product"].name_search(
            name=query,
            args=[("company_id", "in", [False, channel.contact_center_company_id.id])],
            operator="ilike",
            limit=40,
        )
        return [{"id": record_id, "name": name} for record_id, name in products]

    @api.model
    def prepare_catalog_content(self, channel_id, item_ids):
        channel = self._catalog_channel(channel_id)
        ids = self._catalog_ids(item_ids)
        if not ids:
            raise ValidationError(_("Select some content first."))
        model = self.env["marketing.center.catalog.item"]
        items = model.search(
            expression.AND(
                [
                    model._catalog_domain(
                        channel.contact_center_company_id.id, shareable_only=True
                    ),
                    [("id", "in", ids)],
                ]
            )
        )
        if set(items.ids) != set(ids):
            raise AccessError(
                _("Some selected content is no longer available to share.")
            )
        items.subject_id.check_access_rule("read")
        result = []
        for item in model.browse(ids):
            result.append(
                {
                    "id": item.id,
                    "text": "" if item.kind == "file" else item._catalog_plaintext(),
                    "file": (
                        {
                            "url": "/contact_center/catalog/%s/item/%s/file"
                            % (channel.id, item.id),
                            "name": item.file_name or item.name,
                            "mimetype": item.mimetype or "application/octet-stream",
                        }
                        if item.kind == "file"
                        else False
                    ),
                }
            )
        return {"channel_id": channel.id, "items": result}
