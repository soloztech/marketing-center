{
    "name": "Marketing Center - Sales",
    "summary": "Optional sales event history, disabled by default",
    "version": "16.0.1.1.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "sale_management",
        "sale_crm",
        "marketing_center_base",
        "marketing_center_crm",
    ],
    "data": [
        "security/marketing_center_sale_security.xml",
        "security/ir.model.access.csv",
        "views/sale_views.xml",
        "views/link_views.xml",
    ],
    "installable": True,
    "application": False,
}
