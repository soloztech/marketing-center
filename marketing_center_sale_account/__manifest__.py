{
    "name": "Marketing Center - Sales Accounting Bridge",
    "summary": "Optional sales/accounting history links, disabled by default",
    "version": "16.0.1.1.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "marketing_center_account",
        "marketing_center_sale",
    ],
    "data": [
        "security/marketing_center_sale_account_security.xml",
        "security/ir.model.access.csv",
        "views/account_sale_views.xml",
    ],
    "installable": True,
    "application": False,
}
