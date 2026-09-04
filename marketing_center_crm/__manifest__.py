{
    "name": "Marketing Center - CRM",
    "summary": "Link marketing evidence to CRM and record CRM lifecycle events",
    "version": "16.0.1.3.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["crm", "marketing_center_base"],
    "data": [
        "security/marketing_center_crm_security.xml",
        "security/ir.model.access.csv",
        "views/crm_views.xml",
        "views/link_views.xml",
    ],
    "installable": True,
    "application": False,
}
