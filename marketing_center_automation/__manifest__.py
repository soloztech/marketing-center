{
    "name": "Marketing Center - Communication Automation",
    "summary": "OCA journeys with controlled Contact Center messages",
    "version": "16.0.1.0.0",
    "category": "Marketing",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": [
        "automation_oca",
        "base_automation",
        "marketing_center_crm",
        "contact_center_crm",
        "queue_job",
    ],
    "data": [
        "security/automation_security.xml",
        "data/automation.xml",
        "views/automation_views.xml",
    ],
    "installable": True,
    "application": False,
}
