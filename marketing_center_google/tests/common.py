import re

from ..services.adapter import GoogleDiscoveredCustomer


def create_google_profile(env, *, name="Google Ads laboratory", company=None):
    company = company or env.company
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    identity = (
        env["google.api.identity"]
        .sudo()
        .with_company(company)
        .create(
            {
                "name": "%s identity" % name,
                "company_id": company.id,
                "api_version": "v25",
                "auth_mode": "authorized_user",
                "credential_backend": "environment",
                "oauth_client_id_ref": "GOOGLE_%s_CLIENT_ID" % suffix,
                "oauth_client_secret_ref": "GOOGLE_%s_CLIENT_SECRET" % suffix,
                "refresh_token_ref": "GOOGLE_%s_REFRESH_TOKEN" % suffix,
                "developer_token_ref": "GOOGLE_%s_DEVELOPER_TOKEN" % suffix,
            }
        )
    )
    profile = (
        env["marketing.center.google.profile"]
        .sudo()
        .with_company(company)
        .create(
            {
                "name": name,
                "company_id": company.id,
                "google_identity_id": identity.id,
            }
        )
    )
    return identity, profile


def project_google_source(
    env,
    profile,
    *,
    customer_id="1234567890",
    name="Google Ads laboratory",
    timezone="UTC",
    status="enabled",
    login_customer_id="",
):
    customer = GoogleDiscoveredCustomer(
        resource_name="customers/%s" % customer_id,
        customer_id=customer_id,
        name=name,
        currency=profile.company_id.currency_id.name,
        timezone=timezone,
        status=status,
        manager=False,
        test_account=False,
        hidden=False,
        depth=1,
        access_login_customer_id=login_customer_id,
    )
    source = env["marketing.center.google.service"]._project_customer(profile, customer)
    connection = (
        env["marketing.center.connection"]
        .sudo()
        .search(
            [
                ("source_id", "=", source.id),
                ("adapter_key", "=", "google.ads.rest.v25"),
                ("purpose", "=", "reader"),
            ],
            limit=1,
        )
    )
    return customer, source, connection
