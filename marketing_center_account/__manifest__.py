{
    "name": "Marketing Center - Accounting",
    "summary": "Project invoicing and realized receipts into the marketing ledger",
    "version": "16.0.1.0.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "account",
        "marketing_center_base",
    ],
    "data": [
        "security/marketing_center_account_security.xml",
        "security/ir.model.access.csv",
        "views/account_views.xml",
        "views/link_views.xml",
    ],
    "installable": True,
    "application": False,
}
