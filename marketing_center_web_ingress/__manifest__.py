{
    "name": "Marketing Center - Web Ingress",
    "summary": "Provider-neutral first-party web attribution ingress",
    "version": "16.0.2.1.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/marketing-center",
    "license": "AGPL-3",
    "depends": ["marketing_center_base"],
    "data": [
        "security/marketing_center_web_ingress_security.xml",
        "security/ir.model.access.csv",
        "data/web_ingress_cron.xml",
        "views/web_ingress_views.xml",
    ],
    "installable": True,
    "application": False,
}
