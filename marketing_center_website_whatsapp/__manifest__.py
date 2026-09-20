{
    "name": "Marketing Center - Website WhatsApp",
    "summary": "Link native Website visits to WhatsApp conversations by reference",
    "version": "16.0.1.1.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["marketing_center_website_crm", "contact_center_crm"],
    "data": [
        "security/handoff_security.xml",
        "security/ir.model.access.csv",
        "views/handoff_views.xml",
        "views/conversation_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "marketing_center_website_whatsapp/static/src/js/whatsapp_handoff.esm.js",
        ],
    },
    "installable": True,
    "application": False,
}
