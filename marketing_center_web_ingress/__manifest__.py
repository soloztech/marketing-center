{
    "name": "Marketing Center - Web Ingress",
    "summary": "Provider-neutral first-party web attribution ingress",
    "version": "16.0.1.1.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
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
