{
    "name": "Marketing Center - Contact Center CRM Bridge",
    "summary": "Converge Contact Center CRM cases with marketing attribution",
    "version": "16.0.1.2.1",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "contact_center_crm",
        "marketing_center_contact_center",
        "marketing_center_crm",
        "queue_job",
    ],
    "data": ["data/queue_job.xml"],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
