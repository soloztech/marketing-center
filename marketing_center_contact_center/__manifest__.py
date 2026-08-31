{
    "name": "Marketing Center - Contact Center Bridge",
    "summary": "Project Contact Center acquisition evidence into Marketing Center",
    "version": "16.0.1.0.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/marketing-center",
    "license": "AGPL-3",
    "depends": ["contact_center_base", "marketing_center_base", "queue_job"],
    "data": [
        "security/marketing_center_contact_center_security.xml",
        "security/ir.model.access.csv",
        "data/queue_job.xml",
        "views/attribution_link_views.xml",
    ],
    "installable": True,
    "application": False,
}
