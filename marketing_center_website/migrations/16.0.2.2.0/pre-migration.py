"""Retire notice UI; do not change campaign configuration or history."""
import hashlib

from lxml import etree

from odoo import SUPERUSER_ID, _, api
from odoo.exceptions import UserError

RETIRED = (
    "marketing_center_website.cookie_notice",
    "marketing_center_website.res_config_settings_view_form",
)


def retire_notice(env):
    views = env["ir.ui.view"].with_context(active_test=False)
    targets = views.browse()
    for xmlid in RETIRED:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            if record._name != "ir.ui.view":
                raise UserError(_("Unexpected retired cookie record: ") + xmlid)
            targets |= record
    # Settings XMLID is verified below by field references as well.
    unexpected = views.search(
        [("inherit_id", "in", targets.ids), ("id", "not in", targets.ids)]
    )
    if unexpected:
        raise UserError(
            _("Run Soloz preparation BEFORE -u; cookie descendants remain: ")
            + str(unexpected.ids)
        )
    copies = views.search([("key", "in", list(RETIRED)), ("id", "not in", targets.ids)])
    if copies:
        raise UserError(
            _("Unreviewed cookie/settings Website copy: ") + str(copies.ids)
        )
    # Read every stored language and Reset source, not only the current language.
    layout = env.ref(
        "marketing_center_website.measurement_layout", raise_if_not_found=False
    )
    tokens = (
        "marketing_cookie_",
        "_marketing_informational_notice",
        "_marketing_notice_storage_key",
        "data-marketing-tracking-notice",
        "marketing-cookie-notice",
        "cookie_proceed_label",
        "cookie_policy_url",
        "cookie_notice",
    )
    remaining = []
    for view in views.search(
        [("id", "not in", (targets | layout).ids if layout else targets.ids)]
    ):
        arches = list(
            (view._fields["arch_db"]._get_stored_translations(view) or {}).values()
        )
        arches.append(view.arch_prev or "")
        if any(token in arch for arch in arches for token in tokens):
            remaining.append(view.id)
    if remaining:
        raise UserError(
            _("Run Soloz preparation; retired notice references remain in CMS: ")
            + str(remaining)
        )
    expected = {
        "marketing_center_website.cookie_notice": (
            "58265a81ec0afcff76a3c208d293408514c2b28f59a4f436d753cc9721501db1"
        ),
        "marketing_center_website.res_config_settings_view_form": (
            "c60a249aaf3e5205db6ced71873f02c16e1d792d0463c5bf922b5e77cf4499ca"
        ),
    }
    for xmlid, digest in expected.items():
        record = env.ref(xmlid, raise_if_not_found=False)
        if not record:
            continue  # Fully retired re-entry after a committed native stage.
        for arch in (
            record._fields["arch_db"]._get_stored_translations(record) or {}
        ).values():
            tree = etree.fromstring(
                arch.encode(),
                etree.XMLParser(
                    remove_blank_text=True, resolve_entities=False, no_network=True
                ),
            )
            if (
                hashlib.sha256(etree.tostring(tree, method="c14n")).hexdigest()
                != digest
            ):
                raise UserError(
                    _(
                        "Unreviewed cookie/settings CMS override; preparation review required: "
                    )
                    + xmlid
                )
    ids = targets.ids
    targets.unlink()
    env["ir.model.data"].search(
        [
            ("module", "=", "marketing_center_website"),
            ("model", "=", "ir.ui.view"),
            ("res_id", "in", ids),
        ]
    ).unlink()
    return {"removed_views": ids}


def migrate(cr, version):
    retire_notice(
        api.Environment(cr, SUPERUSER_ID, {"active_test": False, "lang": "en_US"})
    )
