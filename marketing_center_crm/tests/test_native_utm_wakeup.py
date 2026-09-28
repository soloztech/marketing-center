"""Real two-transaction wake-ups for native UTM reconciliation jobs."""

import uuid

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.native_utm import _fingerprint


@tagged("-at_install", "post_install")
class TestNativeUtmWakeupAcrossTransactions(TransactionCase):
    """A job finished after a producer snapshot must not swallow its wake-up."""

    def _env(self, cr):
        return api.Environment(
            cr, SUPERUSER_ID, {"allowed_company_ids": [self.env.company.id]}
        )

    def _states(self, prefix):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT state FROM queue_job WHERE identity_key LIKE %s ORDER BY id",
                [prefix + "%"],
            )
            return [row[0] for row in cr.fetchall()]

    def _finish_jobs(self, prefix):
        # Stands in for a worker that ran and committed the older job.
        with self.registry.cursor() as cr:
            cr.execute(
                "UPDATE queue_job SET state = 'done', date_done = now() "
                "WHERE identity_key LIKE %s",
                [prefix + "%"],
            )

    def _wake_after_stale_snapshot(self, prefix, wake):
        with self.registry.cursor() as cr:
            wake(self._env(cr))
            wake(self._env(cr))  # repeated calls in one transaction coalesce
        self.assertEqual(self._states(prefix), ["pending"])
        stale = self.registry.cursor()
        try:
            # REPEATABLE READ fixes the snapshot at the first statement.
            stale.execute("SELECT count(*) FROM queue_job")
            self._finish_jobs(prefix)
            wake(self._env(stale))
            stale.commit()
        finally:
            stale.close()
        self.assertEqual(self._states(prefix), ["done", "pending"])

    @staticmethod
    def _delete_lead_events(cr, lead_id):
        cr.execute(
            "SELECT id FROM marketing_business_event "
            "WHERE source_model = 'crm.lead' AND source_res_id = %s",
            [lead_id],
        )
        event_ids = [row[0] for row in cr.fetchall()]
        if not event_ids:
            return
        for table in (
            "marketing_attribution_result",
            "marketing_business_event_crm_link",
            "marketing_business_event_observation",
        ):
            cr.execute(
                "SELECT to_regclass(%s)",
                [table],
            )
            if cr.fetchone()[0]:
                cr.execute(
                    "DELETE FROM %s WHERE %s = ANY(%%s)"
                    % (
                        table,
                        "business_event_id"
                        if table == "marketing_attribution_result"
                        else "event_id",
                    ),
                    [event_ids],
                )
        # Newest first: later facts reference earlier ones as root/reversal.
        for event_id in sorted(event_ids, reverse=True):
            cr.execute("DELETE FROM marketing_business_event WHERE id = %s", [event_id])

    def _delete_jobs(self, prefix):
        with self.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM queue_job WHERE identity_key LIKE %s", [prefix + "%"]
            )

    def test_scope_wakeup_is_not_swallowed_by_job_finished_after_snapshot(self):
        entity_id = 900000000 + uuid.uuid4().int % 90000000
        scope = {"source_ids": [], "entity_ids": [entity_id], "touchpoint_ids": []}
        prefix = "marketing_native_utm:scope:%s:%s:" % (
            self.env.company.id,
            _fingerprint(scope),
        )
        try:
            self._wake_after_stale_snapshot(
                prefix,
                lambda env: env[
                    "marketing.native.utm.service"
                ]._after_native_utm_change(entity_ids=[entity_id]),
            )
        finally:
            self._delete_jobs(prefix)

    def test_lead_wakeup_is_not_swallowed_by_job_finished_after_snapshot(self):
        token = uuid.uuid4().hex[:10]
        ids = {}
        try:
            with self.registry.cursor() as cr:
                env = self._env(cr)
                utm_source = env["utm.source"].create({"name": "Wake %s" % token})
                utm_medium = env["utm.medium"].create({"name": "Wake %s" % token})
                source = env["marketing.center.source"].create(
                    {
                        "name": "Wake %s" % token,
                        "service": "test.ads",
                        "state": "active",
                        "external_account_ref": "wake-%s" % token,
                        "native_utm_mode": "simulate",
                        "native_utm_source_id": utm_source.id,
                        "native_utm_medium_id": utm_medium.id,
                    }
                )
                lead = env["crm.lead"].create(
                    {"name": "Wake %s" % token, "company_id": self.env.company.id}
                )
                ids.update(
                    utm_source=utm_source.id,
                    utm_medium=utm_medium.id,
                    source=source.id,
                    lead=lead.id,
                )
            prefix = "marketing_native_utm:lead:%s:" % ids["lead"]
            self._delete_jobs(prefix)
            self._wake_after_stale_snapshot(
                prefix,
                lambda env: env["crm.lead"].browse(ids["lead"])._enqueue_native_utm(),
            )
        finally:
            with self.registry.cursor() as cr:
                env = self._env(cr)
                if ids.get("lead"):
                    cr.execute(
                        "DELETE FROM queue_job WHERE identity_key LIKE %s",
                        ["marketing_native_utm:lead:%s:%%" % ids["lead"]],
                    )
                    env["crm.lead"].browse(ids["lead"]).unlink()
                    # The committed lead also produced immutable ledger facts;
                    # remove them so later suites in this database see no residue.
                    self._delete_lead_events(cr, ids["lead"])
                if ids.get("source"):
                    cr.execute(
                        "DELETE FROM marketing_center_source_access_user_rel "
                        "WHERE source_id = %s",
                        [ids["source"]],
                    )
                    cr.execute(
                        "DELETE FROM marketing_center_source WHERE id = %s",
                        [ids["source"]],
                    )
                    env["utm.source"].browse(ids["utm_source"]).unlink()
                    env["utm.medium"].browse(ids["utm_medium"]).unlink()
