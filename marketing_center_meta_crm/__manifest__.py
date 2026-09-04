{
    "name": "Marketing Center - Meta Lead Ads CRM",
    "summary": "Optionally project authenticated Meta Lead Ads submissions to CRM",
    "version": "16.0.1.2.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "marketing_center_meta",
        "marketing_center_crm",
        "queue_job",
    ],
    "data": [
        "security/marketing_center_meta_crm_security.xml",
        "security/ir.model.access.csv",
        "data/queue_job.xml",
        "views/meta_lead_crm_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
