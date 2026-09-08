{
    "name": "Meta Webhook Base",
    "summary": "Shared bounded ingress and delivery ledger for Meta webhooks",
    "version": "16.0.1.0.1",
    "category": "Technical",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["meta_api_base", "queue_job"],
    "data": [
        "security/meta_webhook_security.xml",
        "security/ir.model.access.csv",
        "data/queue_job.xml",
        "data/recovery_cron.xml",
        "views/meta_webhook_views.xml",
    ],
    "installable": True,
    "application": False,
}
