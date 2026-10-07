import datetime
from unittest.mock import patch

from odoo.api import call_kw
from odoo.exceptions import ValidationError

from odoo.addons.queue_job.tests.common import trap_jobs

from .test_convergence import MarketingContactCenterCrmFixture


class TestMarketingBusinessScope(MarketingContactCenterCrmFixture):
    def _context(self, channel, lead, writer="manual"):
        return (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.user)
            ._link(
                channel.with_user(self.user), lead.with_user(self.user), writer=writer
            )
        )

    def _confirm(self, row, start="2026-09-01 12:00:00", end=False):
        return row.with_env(self.env).with_user(self.user)._confirm_scope(start, end)

    def _effective(self, lead):
        return self.env["marketing.attribution.crm.effective.link"].search(
            [("lead_id", "=", lead.id)]
        )

    def _assert_point_crm_leads(self, point, leads):
        # Nonstored raw and effective counters cache separately. Model a fresh
        # UI RPC after the preceding mutation, then verify both count and action.
        point.invalidate_recordset(["crm_lead_count"])
        self.env["marketing.attribution.effective.touchpoint"].invalidate_model(
            ["crm_lead_count"]
        )
        self.assertEqual(point.crm_lead_count, len(leads))
        self.assertEqual(
            point.action_view_crm_leads()["domain"],
            [("id", "in", sorted(leads.ids))],
        )

    def test_context_has_no_credit_then_confirm_converges_and_unlink_revokes(self):
        channel, binding = self._channel("business-flow")
        lead = self._lead("Business flow")
        row = self._context(channel, lead, "automation")
        _source, bridge = self._source_and_projection(binding, "business-flow")
        self.assertFalse(self._effective(lead))
        confirmed = self._confirm(row)
        self.assertTrue(confirmed.marketing_scope_pending)
        self.assertEqual(lead._job_resolve_native_utm()["state"], "scope_review")
        self.env.company._job_marketing_contact_center_crm_conversation_link(
            confirmed.id
        )
        self.assertFalse(confirmed.marketing_scope_pending)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertEqual(lead.marketing_touchpoint_count, 1)
        self._assert_point_crm_leads(bridge.marketing_touchpoint_id, lead)
        confirmed._tombstone()
        self.assertFalse(self._effective(lead))
        self._assert_point_crm_leads(bridge.marketing_touchpoint_id, lead.browse())

    def test_context_convergence_does_not_enqueue_redundant_utm_job(self):
        channel, _binding = self._channel("context-no-hold")
        lead = self._lead("Context without convergence hold")
        row = self._context(channel, lead)
        self.env["marketing.center.source"].create(
            {
                "name": "Context classification",
                "service": "test.ads",
                "external_account_ref": "context-no-hold",
                "native_utm_mode": "apply",
                "native_utm_source_id": self.env["utm.source"]
                .create({"name": "Context source"})
                .id,
                "native_utm_medium_id": self.env["utm.medium"]
                .create({"name": "Context medium"})
                .id,
            }
        )
        with trap_jobs() as trap:
            self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
            trap.assert_jobs_count(0, only=lead._job_resolve_native_utm)
        self.assertFalse(row.marketing_scope_pending)

    def test_bounded_confirmation_holds_until_last_page_and_failure_keeps_marker(self):
        channel, binding = self._channel("paged-scope")
        lead = self._lead("Paged scope")
        row = self._context(channel, lead)
        for i in range(3):
            self._source_and_projection(binding, "paged-%s" % i, reconcile=False)
        confirmed = self._confirm(row)
        page = self.env.company._job_marketing_contact_center_crm_conversation_link(
            confirmed.id, limit=2
        )
        self.assertFalse(page["done"])
        self.assertTrue(confirmed.marketing_scope_pending)
        self.assertEqual(lead._job_resolve_native_utm()["state"], "scope_review")
        with patch.object(
            type(self.convergence),
            "_reconcile_channel",
            side_effect=ValidationError("synthetic"),
        ):
            with self.assertRaises(ValidationError):
                self.env.company._job_marketing_contact_center_crm_conversation_link(
                    confirmed.id, page["last_attribution_link_id"], 2
                )
        self.assertTrue(confirmed.marketing_scope_pending)
        self.env.company._job_marketing_contact_center_crm_conversation_link(
            confirmed.id, page["last_attribution_link_id"], 2
        )
        self.assertFalse(confirmed.marketing_scope_pending)
        self.assertEqual(len(self._effective(lead)._scope_partition()["eligible"]), 3)

    def test_failed_confirmation_can_be_revised_or_unlinked_without_stuck_hold(self):
        channel, _binding = self._channel("failed-scope")
        lead = self._lead("Failed scope")
        first = self._confirm(self._context(channel, lead))
        revised = self._confirm(first, "2026-09-02 00:00:00")
        self.assertFalse(first.marketing_scope_pending)
        self.assertTrue(revised.marketing_scope_pending)
        revised._tombstone()
        self.assertFalse(revised.marketing_scope_pending)
        self.assertFalse(lead._marketing_scope_review_pending())

    def test_legacy_pending_and_independent_authority_supports_same_projection(self):
        channel, binding = self._channel("legacy-scope")
        lead = self._lead("Legacy scope")
        row = self._context(channel, lead)
        _source, bridge = self._source_and_projection(binding, "legacy-scope")
        row._service().write({"scope_state": "legacy", "writer": "unknown"})
        service = self.env["marketing.crm.service"]
        assertion = service._link_touchpoint_lead(
            bridge.marketing_touchpoint_id,
            lead,
            authority_key="contact_center.conversation",
            authority_ref=str(channel.id),
        )
        self.assertTrue(self._effective(lead)._scope_partition()["pending"])
        self.assertEqual(lead._job_resolve_native_utm()["state"], "scope_review")
        self.assertEqual(lead.marketing_touchpoint_count, 0)
        self._assert_point_crm_leads(bridge.marketing_touchpoint_id, lead.browse())
        native = service._link_touchpoint_lead(
            bridge.marketing_touchpoint_id,
            lead,
            authority_key="website.form",
            authority_ref="independent",
        )
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertFalse(self._effective(lead)._scope_partition()["pending"])
        self._assert_point_crm_leads(bridge.marketing_touchpoint_id, lead)
        row._tombstone()
        self.assertTrue(assertion.revocation_ids)
        self.assertFalse(native.revocation_ids)

    def test_confirmation_excludes_older_origin_and_end_boundary(self):
        channel, binding = self._channel("old-origin")
        lead = self._lead("New business")
        _source, _bridge = self._source_and_projection(binding, "old-origin")
        confirmed = self._confirm(self._context(channel, lead), "2026-09-02 00:00:00")
        self.env.company._job_marketing_contact_center_crm_conversation_link(
            confirmed.id
        )
        self.assertFalse(self._effective(lead))

    def test_review_without_assertion_and_deleted_pending_origin_keep_native_values(
        self,
    ):
        channel, _binding = self._channel("orphan-pending")
        lead = self._lead("Orphan pending")
        campaign = self.env["utm.campaign"].create(
            {"name": "Preserved synthetic campaign"}
        )
        lead._native_utm_write({"campaign_id": campaign.id})
        confirmed = self._confirm(self._context(channel, lead))
        remaining, _other_binding = self._channel("orphan-pending-remaining")
        context = self._context(remaining, lead)
        self.user.groups_id |= self.env.ref(
            "contact_center_base.group_contact_center_admin"
        )
        call_kw(
            self.env["contact.center.ui.api"].with_user(self.user),
            "delete_conversation",
            [channel.id],
            {},
        )
        self.assertFalse(channel.exists())
        self.assertTrue(lead.marketing_cc_orphan_review)
        self.assertFalse(confirmed.exists())
        self.assertEqual(lead._job_resolve_native_utm()["state"], "scope_review")
        self.assertEqual(
            lead._journey_origins(remaining, context)["status"], "scope_review"
        )
        self.assertEqual(lead.campaign_id, campaign)
        lead.write({"campaign_id": campaign.id})
        self.assertEqual(lead._job_resolve_native_utm()["state"], "manual")
        self.assertEqual(lead.campaign_id, campaign)
        self.assertEqual(lead._journey_origins(remaining, context)["status"], "ready")
        self.assertTrue(lead.marketing_cc_orphan_review)

    def test_scope_pause_preserves_applied_tuple_and_receipt_then_manual_resolution(
        self,
    ):
        from odoo.addons.marketing_center_base.tests.test_crm_native_utm import (
            TestCrmNativeUtm,
        )

        channel, _binding = self._channel("owned-tuple")
        lead = self._lead("Owned tuple")
        self.lead = lead
        self.crm = self.env["marketing.crm.service"]
        source = self.env["utm.source"].create({"name": "Scope source"})
        medium = self.env["utm.medium"].create({"name": "Scope medium"})
        self.source = self.env["marketing.center.source"].create(
            {
                "name": "Scope campaign source",
                "service": "test.ads",
                "state": "active",
                "external_account_ref": "scope-test",
                "native_utm_mode": "apply",
                "native_utm_source_id": source.id,
                "native_utm_medium_id": medium.id,
            }
        )
        _entity, point, native = TestCrmNativeUtm._point(self)
        classifier = self.env["marketing.crm.native.utm.service"]
        self.assertEqual(classifier._classify(lead, apply=True)["state"], "applied")
        values = lead._native_utm_values()
        receipt = lead.marketing_utm_receipt_id
        row = self._context(channel, lead)
        self.crm._link_touchpoint_lead(
            point,
            lead,
            authority_key="contact_center.conversation",
            authority_ref=str(channel.id),
        )
        self.crm._revoke_attribution_assertions(
            native,
            native.authority_key,
            native.authority_ref,
            "scope-test:retire-native",
        )
        self.assertEqual(
            classifier._classify(lead, apply=True)["state"], "scope_review"
        )
        self.assertEqual(lead._native_utm_values(), values)
        self.assertEqual(lead.marketing_utm_receipt_id, receipt)
        confirmed = self._confirm(row)
        self.assertTrue(confirmed.marketing_scope_pending)
        self.assertEqual(
            classifier._classify(lead, apply=True)["state"], "scope_review"
        )
        self.assertEqual(lead._native_utm_values(), values)
        # Existing explicit undo remains available; it acknowledges manual ownership.
        self.assertEqual(
            lead._journey_origins(channel, confirmed)["status"], "scope_review"
        )
        lead.action_revert_native_utm()
        self.assertEqual(classifier._classify(lead, apply=True)["state"], "manual")
        # Undo resolves UTM ownership; this live convergence still needs to finish.
        self.assertEqual(
            lead._journey_origins(channel, confirmed)["status"], "scope_review"
        )
        self.env.company._job_marketing_contact_center_crm_conversation_link(
            confirmed.id
        )
        self.assertEqual(lead._journey_origins(channel, confirmed)["status"], "ready")

    def test_manual_utm_does_not_hide_live_period_or_convergence_review(self):
        for state in ("context", "legacy", "review", "confirmed"):
            with self.subTest(state=state):
                channel, binding = self._channel("manual-live-" + state)
                lead = self._lead("Manual live " + state)
                row = self._context(channel, lead)
                _source, bridge = self._source_and_projection(
                    binding, "manual-live-" + state
                )
                self.env["marketing.crm.service"]._link_touchpoint_lead(
                    bridge.marketing_touchpoint_id,
                    lead,
                    authority_key="contact_center.conversation",
                    authority_ref=str(channel.id),
                )
                if state == "confirmed":
                    row = self._confirm(row)
                    self.assertTrue(row.marketing_scope_pending)
                elif state != "context":
                    values = {"scope_state": state}
                    if state == "legacy":
                        values["writer"] = "unknown"
                    row._service().write(values)
                lead.write({"campaign_id": False})
                self.assertTrue(lead.marketing_utm_manual)
                self.assertEqual(lead._job_resolve_native_utm()["state"], "manual")
                self.assertEqual(
                    lead._journey_origins(channel, row)["status"], "scope_review"
                )
                if state != "confirmed":
                    self.assertFalse(lead._journey_origins(channel, row)["items"])

    def test_review_without_assertions_still_holds_native_classification(self):
        channel, _binding = self._channel("review-no-assertion")
        lead = self._lead("Review without assertions")
        campaign = self.env["utm.campaign"].create({"name": "Review campaign"})
        lead._native_utm_write({"campaign_id": campaign.id})
        row = self._context(channel, lead)
        row._service().write({"scope_state": "review"})
        self.assertFalse(self._effective(lead))
        self.assertEqual(lead._job_resolve_native_utm()["state"], "scope_review")
        self.assertEqual(lead.campaign_id, campaign)

    def test_transferred_confirmation_holds_nonmanual_business_for_review(self):
        channel, binding = self._channel("transfer-review")
        source_lead = self._lead("Transfer source")
        target = self._lead("Transfer target")
        row = self._confirm(self._context(channel, source_lead))
        self._source_and_projection(binding, "transfer-review")
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        self.assertFalse(target.marketing_utm_manual)
        (source_lead | target)._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        row._transfer_to_lead(target)
        self.assertEqual(target._conversation_links().scope_state, "review")
        self.assertFalse(target.marketing_utm_manual)
        self.assertEqual(target._job_resolve_native_utm()["state"], "scope_review")

    def test_deleted_legacy_and_completed_confirmation_preserve_orphan_hold(self):
        self.env["marketing.center.source"].create(
            {
                "name": "Orphan classification",
                "service": "test.ads",
                "external_account_ref": "orphan-review",
                "native_utm_mode": "apply",
                "native_utm_source_id": self.env["utm.source"]
                .create({"name": "Orphan source"})
                .id,
                "native_utm_medium_id": self.env["utm.medium"]
                .create({"name": "Orphan medium"})
                .id,
            }
        )
        for state in ("legacy", "confirmed"):
            with self.subTest(state=state):
                channel, binding = self._channel("deleted-" + state)
                lead = self._lead("Deleted " + state)
                row = self._context(channel, lead)
                remaining, _other_binding = self._channel("remaining-" + state)
                context = self._context(remaining, lead)
                _source, bridge = self._source_and_projection(
                    binding, "deleted-" + state
                )
                self.crm = self.env["marketing.crm.service"]
                if state == "confirmed":
                    row = self._confirm(row)
                    self.env.company._job_marketing_contact_center_crm_conversation_link(
                        row.id
                    )
                    self.assertFalse(row.marketing_scope_pending)
                else:
                    row._service().write({"scope_state": "legacy", "writer": "unknown"})
                    self.crm._link_touchpoint_lead(
                        bridge.marketing_touchpoint_id,
                        lead,
                        authority_key="contact_center.conversation",
                        authority_ref=str(channel.id),
                    )
                campaign = self.env["utm.campaign"].create({"name": "Orphan " + state})
                lead._native_utm_write({"campaign_id": campaign.id})
                self.user.groups_id |= self.env.ref(
                    "contact_center_base.group_contact_center_admin"
                )
                with trap_jobs() as trap:
                    call_kw(
                        self.env["contact.center.ui.api"].with_user(self.user),
                        "delete_conversation",
                        [channel.id],
                        {},
                    )
                    trap.assert_jobs_count(1, only=lead._job_resolve_native_utm)
                    trap.perform_enqueued_jobs()
                self.assertFalse(channel.exists())
                self.assertEqual(lead.marketing_utm_state, "scope_review")
                self.assertEqual(lead.campaign_id, campaign)
                self.assertEqual(
                    lead._journey_origins(remaining, context)["status"], "scope_review"
                )
                self.crm._link_touchpoint_lead(
                    bridge.marketing_touchpoint_id,
                    lead,
                    authority_key="website.form",
                    authority_ref="independent-deleted-" + state,
                )
                self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
                lead.write({"campaign_id": campaign.id})
                self.assertEqual(lead._job_resolve_native_utm()["state"], "manual")
                self.assertEqual(
                    lead._journey_origins(remaining, context)["status"], "ready"
                )

    def test_historical_backfill_aborts_without_changing_assertions_or_utm(self):
        import runpy
        from pathlib import Path

        from odoo.addons.contact_center_crm import __file__ as crm_file

        channel, binding = self._channel("blocked-backfill")
        lead = self._lead("Blocked backfill")
        row = self._confirm(self._context(channel, lead))
        self._source_and_projection(binding, "blocked-backfill")
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        assertions = self.env["marketing.attribution.crm.link"].search([])
        links = self.env["contact.center.crm.conversation.link"].search([])
        values = lead._native_utm_values()
        migrate = runpy.run_path(
            str(
                Path(crm_file).resolve().parents[1]
                / "release_migrations/crm_conversation_backfill.py"
            )
        )["migrate"]
        with self.assertRaisesRegex(RuntimeError, "Historical extraction is blocked"):
            migrate(self.env)
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search([]), assertions
        )
        self.assertEqual(
            self.env["contact.center.crm.conversation.link"].search([]), links
        )
        self.assertEqual(lead._native_utm_values(), values)
        self.assertFalse(assertions.mapped("revocation_ids"))

    def test_effective_touchpoint_revision_rechecks_the_window(self):
        from odoo.addons.marketing_center_base.services import MarketingTouchpointDTO

        channel, binding = self._channel("revision-window")
        lead = self._lead("Revision window")
        _source, bridge = self._source_and_projection(binding, "revision-window")
        row = self._confirm(self._context(channel, lead), end="2026-09-02 00:00:00")
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        self.assertEqual(lead.marketing_touchpoint_count, 1)
        point = bridge.marketing_touchpoint_id
        correction = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.env.company,
            MarketingTouchpointDTO(
                source_system=point.source_system,
                source_scope_ref=point.source_scope_ref,
                source_occurrence_ref=point.source_occurrence_ref,
                source_evidence_ref="corrected-scope-time",
                occurred_at=datetime.datetime(2026, 9, 2),
                platform=point.platform,
                channel=point.channel,
                touchpoint_type=point.touchpoint_type,
                evidence_level=point.evidence_level,
                revision_kind="correction",
            ),
        )
        lead.invalidate_recordset(["marketing_touchpoint_count"])
        self.assertEqual(lead.marketing_touchpoint_count, 0)
        self._assert_point_crm_leads(point, lead.browse())
        self.assertTrue(self._effective(lead)._scope_partition()["ineligible"])
        corrected = self.env["marketing.attribution.touchpoint"].browse(
            correction.touchpoint_id
        )
        self.env["marketing.crm.service"]._link_touchpoint_lead(
            corrected,
            lead,
            authority_key="website.form",
            authority_ref="independent-revision",
        )
        lead.invalidate_recordset(["marketing_touchpoint_count"])
        self.assertEqual(lead.marketing_touchpoint_count, 1)
        self._assert_point_crm_leads(point, lead)
