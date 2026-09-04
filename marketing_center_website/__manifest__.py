{
    "name": "Marketing Center - Website",
    "summary": "First-party Odoo Website adapter for the marketing ingress",
    "version": "16.0.2.2.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/marketing-center",
    "license": "AGPL-3",
    "depends": ["marketing_center_web_ingress", "website"],
    "data": [
        "security/marketing_center_website_security.xml",
        "security/ir.model.access.csv",
        "data/website_action_cron.xml",
        "views/website_ingress_binding_views.xml",
        "views/website_action_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "marketing_center_website/static/src/js/landing_capture.esm.js",
            "marketing_center_website/static/src/js/landing_bootstrap.esm.js",
            "marketing_center_website/static/src/js/action_capture.esm.js",
            "marketing_center_website/static/src/js/action_bootstrap.esm.js",
        ],
        "web.qunit_suite_tests": [
            "marketing_center_website/static/src/js/landing_capture.esm.js",
            "marketing_center_website/static/src/js/action_capture.esm.js",
            "marketing_center_website/static/tests/landing_capture_tests.esm.js",
            "marketing_center_website/static/tests/action_capture_tests.esm.js",
        ],
    },
    "installable": True,
    "application": False,
}
