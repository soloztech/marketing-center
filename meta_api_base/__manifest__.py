{
    "name": "Meta API Base",
    "summary": "Provider-neutral Meta Graph transport and webhook authentication",
    "version": "16.0.1.1.1",
    "category": "Technical",
    "author": "Soloz Technologies",
    "website": "https://github.com/soloztech/marketing-center",
    "license": "AGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/meta_api_security.xml",
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
}
