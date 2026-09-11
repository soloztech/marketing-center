{
    "name": "Marketing Center - Content Catalog",
    "summary": "Company and product content, files and FAQs for sales and support",
    "version": "16.0.1.2.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["product", "mail", "web", "web_editor"],
    "data": [
        "security/catalog_security.xml",
        "security/ir.model.access.csv",
        "views/catalog_item_views.xml",
        "views/catalog_subject_views.xml",
        "views/product_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "marketing_center_catalog/static/src/js/*.js",
            "marketing_center_catalog/static/src/xml/*.xml",
            "marketing_center_catalog/static/src/scss/*.scss",
        ],
        "web.qunit_suite_tests": [
            "marketing_center_catalog/static/tests/*.js",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
