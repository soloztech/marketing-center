{
    "name": "Marketing Center - Dashboard",
    "summary": "Read-only operational coverage and funnel overview",
    "version": "16.0.1.2.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/marketing-center",
    "license": "AGPL-3",
    "depends": ["marketing_center_base"],
    "data": [
        "security/marketing_center_dashboard_security.xml",
        "security/ir.model.access.csv",
        "views/dashboard_views.xml",
    ],
    "installable": True,
    "application": False,
}
