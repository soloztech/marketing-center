def create_meta_app(
    env,
    *,
    name,
    external_app_id,
    app_secret_ref,
    graph_version="v26.0",
):
    return (
        env["meta.api.app"]
        .sudo()
        .create(
            {
                "name": name,
                "company_id": env.company.id,
                "external_app_id": external_app_id,
                "graph_version": graph_version,
                "credential_backend": "environment",
                "app_secret_ref": app_secret_ref,
            }
        )
    )


def create_meta_profile(
    env,
    *,
    name,
    external_app_id,
    app_secret_ref,
    access_token_ref,
    required_scopes=None,
    reader_kind="ads_reader",
):
    app = create_meta_app(
        env,
        name="%s App" % name,
        external_app_id=external_app_id,
        app_secret_ref=app_secret_ref,
    )
    values = {
        "name": name,
        "company_id": env.company.id,
        "meta_app_id": app.id,
        "credential_backend": "environment",
        "access_token_ref": access_token_ref,
        "reader_kind": reader_kind,
    }
    if required_scopes is not None:
        values["required_scopes"] = required_scopes
    profile = env["marketing.center.meta.profile"].create(values)
    return app, profile
