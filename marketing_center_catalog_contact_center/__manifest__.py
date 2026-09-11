{
    "name": "Marketing Center Catalog - Contact Center",
    "summary": "Find company and product content and add it to conversation drafts",
    "version": "16.0.1.1.0",
    "category": "Sales/CRM",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["marketing_center_catalog", "contact_center_ui"],
    "assets": {
        "web.assets_backend": [
            "marketing_center_catalog_contact_center/static/src/js/catalog_dialog.esm.js",
            "marketing_center_catalog_contact_center/static/src/js/message_composer.esm.js",
            "marketing_center_catalog_contact_center/static/src/xml/*.xml",
            "marketing_center_catalog_contact_center/static/src/scss/catalog_dialog.scss",
        ],
        "web.qunit_suite_tests": [
            "marketing_center_catalog_contact_center/static/tests/*.esm.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
