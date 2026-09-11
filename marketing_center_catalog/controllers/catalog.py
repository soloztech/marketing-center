from odoo import http
from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.http import request

from ..models.catalog_item import PREVIEW_MIMETYPES


class CatalogMediaController(http.Controller):
    def _catalog_media_response(self, item_id, inline=False):
        try:
            item = request.env["ir.binary"]._find_record(
                res_model="marketing.center.catalog.item",
                res_id=item_id,
            )
            if item.kind != "file" or not item.active or not item.subject_id.active:
                raise request.not_found()
            stream = request.env["ir.binary"]._get_stream_from(
                item,
                "file_data",
                filename_field="file_name",
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
        "/marketing_center/catalog/item/<int:item_id>/preview",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def preview(self, item_id, **_kwargs):
        return self._catalog_media_response(item_id, inline=True)

    @http.route(
        "/marketing_center/catalog/item/<int:item_id>/file",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def download(self, item_id, **_kwargs):
        return self._catalog_media_response(item_id)
