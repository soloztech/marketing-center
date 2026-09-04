from odoo import models

from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN


class MetaApiApp(models.Model):
    _inherit = "meta.api.app"

    def write(self, values):
        """Invalidate webhook observations after a material App change.

        Subscription reconciliation always locks Endpoint before App.  This
        bridge follows that same order: dependent endpoints are locked before
        the inherited App writer takes its revision lock.  It therefore keeps
        the technical observation honest without introducing App -> Endpoint
        lock inversion into the shared foundation.
        """

        revision_fields = {
            "active",
            "external_app_id",
            "graph_version",
            "credential_backend",
            "app_secret_ref",
        }
        if not revision_fields.intersection(values):
            return super().write(values)

        endpoints = (
            self.env["meta.webhook.endpoint"]
            .sudo()
            .with_context(active_test=False)
            .search([("app_id", "in", self.ids)], order="id")
        )
        endpoints.flush_recordset(["app_id"])
        if endpoints:
            self.env.cr.execute(
                "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(endpoints.ids)],
            )

        # A concurrent writer may have committed while the endpoints were being
        # found.  Invalidate only after their lock serializes this App's webhook
        # dependants, then use the monotonic App revision as the change oracle.
        self.invalidate_recordset(["revision"])
        previous_revisions = {app.id: app.revision for app in self}
        result = super().write(values)
        self.invalidate_recordset(["active", "revision"])
        changed_apps = self.filtered(
            lambda app: app.revision != previous_revisions[app.id]
        )
        if not changed_apps:
            return result

        affected = endpoints.filtered(lambda endpoint: endpoint.app_id in changed_apps)
        for endpoint in affected:
            app_paused = not endpoint.app_id.active
            endpoint.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
                {
                    "subscription_state": "error" if app_paused else "unknown",
                    "observed_subscriptions_json": False,
                    "verified_at": False,
                    "last_error_class": "AppPaused" if app_paused else False,
                    "last_error_message": (
                        "The shared Meta App is paused." if app_paused else False
                    ),
                }
            )
            active_pages = endpoint.page_ids.filtered("active")
            if active_pages:
                active_pages.with_context(
                    meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
                ).write(
                    {
                        "subscription_state": "error" if app_paused else "unknown",
                        "observed_fields_json": False,
                        "verified_at": False,
                        "last_error_class": "AppPaused" if app_paused else False,
                        "last_error_message": (
                            "The shared Meta App is paused." if app_paused else False
                        ),
                    }
                )
        return result
