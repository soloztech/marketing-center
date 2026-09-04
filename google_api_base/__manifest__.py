{
    "name": "Google API Base",
    "summary": "Bounded Google Ads REST transport and external credentials",
    "version": "16.0.1.1.2",
    "category": "Technical",
    "author": "Soloz Technologies",
    "website": "https://github.com/lcsztl/integration-core",
    "license": "AGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests", "google.auth"]},
    "data": [
        "security/google_api_security.xml",
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
}
