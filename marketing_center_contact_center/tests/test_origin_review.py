import datetime
import hashlib

from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_crm.models.auto_origin import ORIGIN_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from .test_convergence import MarketingContactCenterCrmFixture


class TestAutomaticOriginReview(MarketingContactCenterCrmFixture):
    def _automatic(self, label, end=False):
        channel, binding = self._channel(label)
        lead = self._lead(label)
        with trap_jobs():
            row = (
                self.env["contact.center.crm.conversation.link"]
                .with_user(self.user)
                ._link(
                    channel.with_user(self.user),
                    lead.with_user(self.user),
                    writer="intake",
                    origin="created",
                )
            )
            row = (
                row.with_user(self.user)
                .with_context(
                    crm_origin_service=ORIGIN_TOKEN,
                    crm_origin_anchor_source=1,
                    crm_origin_policy_revision=1,
                )
                ._confirm_scope("2026-09-01 11:00:00", end)
            )
            self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        return lead, channel, binding, row

    def _point(self, binding, label, at):
        with trap_jobs():
            _source, bridge = self._source_and_projection(
                binding, label, occurred_at=at
            )
        return bridge.marketing_touchpoint_id

    def _review(self, lead, channel, point, decision, start=None, end=False):
        row = lead._conversation_links()
        wizard = (
            self.env["contact.center.crm.origin.review"]
            .with_user(self.user)
            .create(
                {
                    "lead_id": lead.id,
                    "channel_id": channel.id,
                    "evidence_key": point.canonical_key,
                    "decision": decision,
                    "scope_start": start or row.scope_start,
                    "scope_end": end or row.scope_end,
                }
            )
        )
        with trap_jobs():
            wizard.action_confirm()
        self.env.company._job_marketing_contact_center_crm_conversation_link(
            lead._conversation_links().id
        )
        return lead._conversation_links()

    def test_outside_origin_visible_without_marketing_and_exclusion_is_idempotent(self):
        lead, channel, binding, row = self._automatic(
            "origin-outside", "2026-09-02 00:00:00"
        )
        point = self._point(binding, "outside-point", datetime.datetime(2026, 9, 3))
        self.assertEqual(
            row._crm_origin_evidence_scope(point.occurred_at, point.canonical_key),
            "pending",
        )
        page = lead.with_user(self.user).get_contact_center_origin_page(channel.id)
        self.assertEqual(page["items"][0]["evidence_key"], point.canonical_key)
        self.assertEqual(page["items"][0]["scope"], "pending")
        self.assertFalse(
            self.env["marketing.attribution.touchpoint"]
            .with_user(self.user)
            .check_access_rights("read", raise_exception=False)
        )
        self.assertLessEqual(
            set(page["items"][0]),
            {
                "type",
                "at",
                "scope",
                "review_reason",
                "evidence_key",
                "can_review",
                "campaign_name",
                "ad_name",
                "source_name",
                "medium_name",
            },
        )
        row = self._review(lead, channel, point, "exclude")
        self.assertEqual(
            row._crm_origin_evidence_scope(point.occurred_at, point.canonical_key),
            "ineligible",
        )
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        self.assertEqual(
            row._crm_origin_evidence_scope(point.occurred_at, point.canonical_key),
            "ineligible",
        )
        self.assertEqual(
            lead.with_user(self.user).get_contact_center_origin_page(channel.id)[
                "items"
            ][0]["scope"],
            "ineligible",
        )

    def test_human_window_successor_does_not_inherit_other_decisions(self):
        lead, channel, binding, row = self._automatic(
            "origin-window", "2026-09-02 00:00:00"
        )
        first = self._point(binding, "origin-first", datetime.datetime(2026, 9, 3))
        second = self._point(binding, "origin-second", datetime.datetime(2026, 9, 4))
        self._review(lead, channel, second, "exclude")
        successor = self._review(
            lead, channel, first, "include", end=datetime.datetime(2026, 9, 3, 0, 0, 1)
        )
        self.assertNotEqual(successor, row)
        self.assertEqual(successor.scope_decision_mode, "human")
        self.assertTrue(successor.automatic_lineage)
        self.assertEqual(
            successor._crm_origin_evidence_scope(
                first.occurred_at, first.canonical_key
            ),
            "eligible",
        )
        self.assertEqual(
            successor._crm_origin_evidence_scope(
                second.occurred_at, second.canonical_key
            ),
            "ineligible",
        )

    def test_closed_return_requires_reopen_and_accepts_only_decided_evidence(self):
        lead, channel, binding, row = self._automatic("origin-return")
        lead.write({"probability": 100})
        self.assertTrue(row.origin_first_closed_at)
        closed_at = row.origin_first_closed_at
        before = self._point(
            binding, "late-delivery", closed_at - datetime.timedelta(seconds=1)
        )
        first = self._point(
            binding, "return-first", closed_at + datetime.timedelta(seconds=1)
        )
        second = self._point(
            binding, "return-second", closed_at + datetime.timedelta(seconds=2)
        )
        self.assertEqual(
            row._crm_origin_evidence_scope(before.occurred_at, before.canonical_key),
            "eligible",
        )
        self.assertEqual(
            row._crm_origin_evidence_scope(first.occurred_at, first.canonical_key),
            "pending",
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._review(lead, channel, first, "include")
        lead.write({"probability": 20})
        row = self._review(lead, channel, first, "include")
        self.assertEqual(row.origin_first_closed_at, closed_at)
        self.assertEqual(
            row._crm_origin_evidence_scope(first.occurred_at, first.canonical_key),
            "eligible",
        )
        self.assertEqual(
            row._crm_origin_evidence_scope(second.occurred_at, second.canonical_key),
            "pending",
        )

    def test_archival_and_stage_won_record_first_closure(self):
        for label, values in (
            ("archived-origin", {"active": False}),
            (
                "won-origin",
                {
                    "stage_id": self.env["crm.stage"]
                    .create({"name": "Won origin", "is_won": True})
                    .id
                },
            ),
        ):
            lead, _channel, _binding, row = self._automatic(label)
            lead.write(values)
            self.assertTrue(row.origin_first_closed_at)
        lead, _channel, _binding, row = self._automatic("stage-toggle-origin")
        lead.stage_id.write({"is_won": True})
        self.assertTrue(row.origin_first_closed_at)

    def test_unmapped_key_and_forged_wizard_target_are_rejected(self):
        lead, channel, _binding, _row = self._automatic("origin-key")
        unknown = hashlib.sha256(b"not-captured").hexdigest()
        with self.assertRaises(AccessError):
            lead.with_user(self.user).action_contact_center_origin_review(
                channel.id, unknown
            )
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env["contact.center.crm.origin.review"].with_user(self.user).create(
                {
                    "lead_id": lead.id,
                    "channel_id": channel.id,
                    "evidence_key": unknown,
                    "scope_start": "2026-09-01",
                }
            )

    def _catalog_authority(self, lead):
        from odoo.addons.marketing_center_base.tests.test_crm_native_utm import (
            TestCrmNativeUtm,
        )

        self.lead = lead
        self.crm = self.env["marketing.crm.service"]
        self.utm_source = self.env["utm.source"].create({"name": "Origin test source"})
        self.utm_medium = self.env["utm.medium"].create({"name": "Origin test medium"})
        self.source = self.env["marketing.center.source"].create(
            {
                "name": "Origin test catalog",
                "service": "test.ads",
                "state": "active",
                "external_account_ref": "origin-catalog",
                "native_utm_mode": "apply",
                "native_utm_source_id": self.utm_source.id,
                "native_utm_medium_id": self.utm_medium.id,
            }
        )
        with trap_jobs():
            entity, point, assertion = TestCrmNativeUtm._point(self)
        classifier = self.env["marketing.crm.native.utm.service"]
        self.assertEqual(classifier._classify(lead, apply=True)["state"], "applied")
        return classifier, entity, point, assertion

    def test_pending_new_origin_preserves_only_still_valid_owned_campaign(self):
        lead, _channel, binding, _row = self._automatic(
            "origin-owned", "2026-09-02 00:00:00"
        )
        classifier, entity, _point, assertion = self._catalog_authority(lead)
        self._point(binding, "owned-pending", datetime.datetime(2026, 9, 3))
        classifier._classify(lead, apply=True)
        self.assertEqual(lead.campaign_id, entity.native_utm_campaign_id)
        self.assertTrue(lead.marketing_origin_review_pending)
        with trap_jobs():
            self.crm._revoke_attribution_assertions(
                assertion, "test", "test", "withdraw"
            )
        classifier._classify(lead, apply=True)
        self.assertFalse(any(lead._native_utm_values().values()))
        self.assertTrue(lead.marketing_origin_review_pending)

    def test_manual_native_campaign_survives_origin_revocation_and_review(self):
        lead, _channel, binding, _row = self._automatic(
            "origin-manual", "2026-09-02 00:00:00"
        )
        classifier, _entity, _point, assertion = self._catalog_authority(lead)
        manual = self.env["utm.campaign"].create({"name": "Manual business campaign"})
        lead.write({"campaign_id": manual.id})
        self._point(binding, "manual-pending", datetime.datetime(2026, 9, 3))
        with trap_jobs():
            self.crm._revoke_attribution_assertions(
                assertion, "test", "test", "withdraw"
            )
        self.assertEqual(classifier._classify(lead, apply=True)["state"], "manual")
        self.assertEqual(lead.campaign_id, manual)

    def test_split_records_the_review_on_successor_and_stops_future_holds(self):
        lead, channel, binding, old = self._automatic("origin-split")
        point = self._point(
            binding, "origin-split-reviewed", datetime.datetime(2026, 9, 3)
        )
        wizard = (
            self.env["contact.center.crm.origin.review"]
            .with_user(self.user)
            .create(
                {
                    "lead_id": lead.id,
                    "channel_id": channel.id,
                    "evidence_key": point.canonical_key,
                    "scope_start": old.scope_start,
                }
            )
        )
        action = wizard.action_split_period()
        with trap_jobs():
            scope = (
                self.env["contact.center.crm.scope"]
                .with_user(self.user)
                .with_context(**action["context"])
                .create({})
            )
            scope.action_confirm()
        current = lead._conversation_links()
        self.assertNotEqual(current, old)
        self.assertEqual(current.scope_decision_mode, "human")
        decision = (
            self.env["contact.center.crm.review.decision"]
            .sudo()
            .search(
                [
                    ("link_ref", "=", current.id),
                    ("evidence_key", "=", point.canonical_key),
                ]
            )
        )
        self.assertEqual(decision.decision, "exclude")
        self.assertEqual(decision.actor_ref, self.user.id)
        self.assertEqual(
            current._crm_origin_evidence_scope(point.occurred_at, point.canonical_key),
            "ineligible",
        )
        later = self._point(
            binding, "origin-other-business", datetime.datetime(2026, 9, 4)
        )
        self.assertEqual(
            current._crm_origin_evidence_scope(later.occurred_at, later.canonical_key),
            "ineligible",
        )
        with trap_jobs():
            next_lead = self._lead("Subsequent business")
            next_link = (
                self.env["contact.center.crm.conversation.link"]
                .with_user(self.user)
                ._link(
                    channel.with_user(self.user),
                    next_lead.with_user(self.user),
                    writer="manual",
                    origin="linked",
                )
                ._confirm_scope(point.occurred_at)
            )
        self.assertEqual(
            next_link._crm_origin_evidence_scope(
                later.occurred_at, later.canonical_key
            ),
            "eligible",
        )

    def test_origin_wizard_is_private_to_creator_in_same_or_other_company(self):
        lead, channel, binding, row = self._automatic("private-origin-review")
        point = self._point(binding, "private-point", datetime.datetime(2026, 9, 3))
        wizard = (
            self.env["contact.center.crm.origin.review"]
            .with_user(self.user)
            .create(
                {
                    "lead_id": lead.id,
                    "channel_id": channel.id,
                    "evidence_key": point.canonical_key,
                    "scope_start": row.scope_start,
                }
            )
        )
        company = self.env["res.company"].create(
            {"name": "Origin wizard foreign company"}
        )
        for allowed in [self.env.company, company]:
            other = (
                self.env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Other origin agent",
                        "login": "other-origin-%s-%s" % (wizard.id, allowed.id),
                        "company_id": allowed.id,
                        "company_ids": [(6, 0, allowed.ids)],
                        "groups_id": [(6, 0, (self.cc_agent | self.crm_user).ids)],
                    }
                )
            )
            target = wizard.with_user(other).with_context(
                allowed_company_ids=allowed.ids
            )
            self.assertFalse(target.search([("id", "=", wizard.id)]))
            for operation in [
                lambda: target.read(),
                lambda: target.write({"decision": "include"}),
                lambda: target.unlink(),
            ]:
                with self.assertRaises(AccessError):
                    operation()

    def test_review_marker_keeps_other_pending_then_clears_after_all_decisions(self):
        lead, channel, binding, row = self._automatic(
            "marker-cohort", "2026-09-02 00:00:00"
        )
        first = self._point(binding, "marker-first", datetime.datetime(2026, 9, 3))
        second = self._point(binding, "marker-second", datetime.datetime(2026, 9, 4))
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        self.assertEqual(row.origin_review_reason, "outside_window")
        row = self._review(lead, channel, first, "exclude")
        self.assertEqual(row.origin_review_reason, "outside_window")
        row = self._review(lead, channel, second, "exclude")
        self.assertFalse(row.origin_review_reason)
        self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
        self.assertFalse(row.origin_review_reason)

    def test_split_defaults_before_anchor_open_valid_period(self):
        lead, channel, binding, old = self._automatic("split-before-anchor")
        point = self._point(binding, "split-before", datetime.datetime(2026, 8, 31))
        wizard = (
            self.env["contact.center.crm.origin.review"]
            .with_user(self.user)
            .create(
                {
                    "lead_id": lead.id,
                    "channel_id": channel.id,
                    "evidence_key": point.canonical_key,
                    "scope_start": old.scope_start,
                }
            )
        )
        action = wizard.action_split_period()
        self.assertEqual(action["context"]["default_scope_start"], point.occurred_at)
        self.assertFalse(action["context"]["default_scope_end"])
        with trap_jobs():
            scope = (
                self.env["contact.center.crm.scope"]
                .with_user(self.user)
                .with_context(**action["context"])
                .create({})
            )
            scope.action_confirm()
        current = lead._conversation_links()
        self.assertEqual(current.scope_decision_mode, "human")
        self.assertEqual(
            current._crm_origin_evidence_scope(point.occurred_at, point.canonical_key),
            "eligible",
        )
