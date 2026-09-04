{
    "name": "Marketing Center - Sales",
    "summary": "Project sales lifecycle and value into the marketing ledger",
    "version": "16.0.1.1.1",
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
