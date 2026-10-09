{
    "name": "Marketing Center - Website WhatsApp",
    "summary": "Link native Website visits to WhatsApp conversations by reference",
    "version": "16.0.1.6.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "marketing_center_website",
        "contact_center_crm",
        "marketing_center_contact_center",
        "queue_job",
    ],
    "data": [
        "security/handoff_security.xml",
        "data/journey_queue.xml",
        "security/ir.model.access.csv",
        "views/handoff_views.xml",
        "views/conversation_views.xml",
        "views/visitor_journey_views.xml",
    ],
    "assets": {
        "web.qunit_suite_tests": [
            "marketing_center_website_whatsapp/static/tests/visitor_journey_tests.esm.js",
        ],
        "web.assets_backend": [
            "marketing_center_website_whatsapp/static/src/js/visitor_journey.esm.js",
            "marketing_center_website_whatsapp/static/src/xml/visitor_journey.xml",
        ],
        "web.assets_frontend": [
            "marketing_center_website_whatsapp/static/src/js/whatsapp_handoff.esm.js",
        ],
    },
    "installable": True,
    "application": False,
}
