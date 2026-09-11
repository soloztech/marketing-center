import base64

from odoo import http
from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.http import request
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.marketing_center_catalog.models.catalog_item import PREVIEW_MIMETYPES


class CatalogController(http.Controller):
    def _catalog_response(self, channel_id, item_id, shareable, inline=False):
        try:
            _channel, item = request.env["contact.center.ui.api"]._catalog_item(
                channel_id, item_id, shareable=shareable
            )
            if item.kind != "file":
                raise request.not_found()
            stream = request.env["ir.binary"]._get_stream_from(
                item, "file_data", filename_field="file_name"
            )
            if stream.type == "path":
                with open(stream.path, "rb") as source:
                    header = source.read(65536)
            elif stream.type == "data":
                header = stream.data[:65536]
            else:
                raise request.not_found()
            stream.mimetype = item._catalog_detect_mimetype(header, item.file_name)
            stream.public = False
            response = stream.get_response(
                as_attachment=not (inline and stream.mimetype in PREVIEW_MIMETYPES),
                content_security_policy="default-src 'none'; sandbox",
            )
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            return response
        except (AccessError, MissingError, ValidationError, FileNotFoundError):
            raise request.not_found() from None

    @http.route(
        "/contact_center/catalog/<int:channel_id>/item/<int:item_id>/file",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def catalog_file(self, channel_id, item_id, **_kwargs):
        return self._catalog_response(channel_id, item_id, shareable=True)

    @http.route(
        "/contact_center/catalog/<int:channel_id>/item/<int:item_id>/preview",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def catalog_preview(self, channel_id, item_id, **_kwargs):
        return self._catalog_response(channel_id, item_id, shareable=False, inline=True)

    @http.route(
        "/contact_center/catalog/<int:channel_id>/item/<int:item_id>/download",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def catalog_download(self, channel_id, item_id, **_kwargs):
        return self._catalog_response(channel_id, item_id, shareable=False)

    @http.route(
        "/contact_center/catalog/<int:channel_id>/subject/<int:subject_id>/image",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def catalog_subject_image(self, channel_id, subject_id, **_kwargs):
        try:
            _channel, subject = request.env["contact.center.ui.api"]._catalog_subject(
                channel_id, subject_id
            )
            data = subject.with_context(bin_size=False).image
            if not data:
                raise request.not_found()
            raw = base64.b64decode(data)
            mimetype = guess_mimetype(raw)
            if mimetype not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/avif",
            }:
                raise request.not_found()
            return request.make_response(
                raw,
                headers=[
                    ("Content-Type", mimetype),
                    ("Cache-Control", "private, no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                    ("Content-Security-Policy", "default-src 'none'; sandbox"),
                ],
            )
        except (AccessError, MissingError, ValidationError):
            raise request.not_found() from None
