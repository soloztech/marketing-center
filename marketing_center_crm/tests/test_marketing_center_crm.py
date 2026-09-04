import datetime
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services import MarketingTouchpointDTO


class TestMarketingCenterCrm(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env["marketing.crm.service"]
        suffix = str(uuid.uuid4())
        cls.stage_a = cls.env["crm.stage"].create(
            {"name": "CRM initial %s" % suffix, "sequence": 301}
        )
        cls.stage_b = cls.env["crm.stage"].create(
            {
                "name": "CRM qualified %s" % suffix,
                "sequence": 302,
                "marketing_semantic": "qualified",
            }
        )
        cls.stage_won = cls.env["crm.stage"].create(
            {"name": "CRM won %s" % suffix, "sequence": 303, "is_won": True}
        )
        cls.lead = cls.env["crm.lead"].create(
            {
                "name": "Marketing CRM %s" % suffix,
                "company_id": cls.env.company.id,
                "stage_id": cls.stage_a.id,
            }
        )
        touchpoint_result = cls.env["marketing.attribution.service"]._ingest_touchpoint(
            cls.env.company,
            MarketingTouchpointDTO(
                source_system="test.crm",
                source_scope_ref="crm-suite",
                source_occurrence_ref="touchpoint:%s" % suffix,
                occurred_at=datetime.datetime(2026, 9, 1, 12, 0),
                platform="web",
                channel="website",
                touchpoint_type="entry_point",
                evidence_level="first_party",
            ),
        )
        cls.touchpoint = cls.env["marketing.attribution.touchpoint"].browse(
            touchpoint_result.touchpoint_id
        )

    def _events(self, lead=None, event_type=None):
        lead = lead or self.lead
        events = lead.marketing_business_event_link_ids.mapped("event_id")
        return events.filtered(
            lambda event: not event_type or event.event_type == event_type
        )

    def test_assertion_identity_is_a_required_runtime_contract(self):
        fields_by_name = self.env["marketing.attribution.crm.link"]._fields
        for field_name in (
            "canonical_key",
            "authority_key",
            "authority_ref",
            "assertion_ref",
        ):
            self.assertTrue(fields_by_name[field_name].required, field_name)

    def test_create_is_projected_once_and_links_are_immutable(self):
        created = self._events(event_type="lead_created")
        self.assertEqual(len(created), 1)
        self.assertEqual(self.lead.marketing_event_sequence, 1)
        link = self.lead.marketing_business_event_link_ids.filtered(
            lambda item: item.event_id == created
        )
        self.assertEqual(len(link), 1)
        with self.assertRaises(AccessError):
            link.sudo().write({"lead_id": self.lead.id})
        with self.assertRaises(AccessError):
            link.sudo().unlink()
        with self.assertRaises(AccessError):
            self.lead.sudo().write({"marketing_event_sequence": 999})
        with self.assertRaises(AccessError):
            self.lead.sudo().write({"marketing_event_company_id": self.env.company.id})

    def test_noop_and_a_b_a_are_distinct_monotonic_occurrences(self):
        initial_count = len(self._events(event_type="lead_stage_changed"))
        initial_sequence = self.lead.marketing_event_sequence
        self.lead.write({"stage_id": self.stage_a.id})
        self.assertEqual(
            len(self._events(event_type="lead_stage_changed")), initial_count
        )
        self.assertEqual(self.lead.marketing_event_sequence, initial_sequence)

        self.lead.write({"stage_id": self.stage_b.id})
        self.lead.write({"stage_id": self.stage_a.id})
        transitions = self._events(event_type="lead_stage_changed")
        self.assertEqual(len(transitions), initial_count + 2)
        self.assertEqual(
            len(set(transitions.mapped("business_event_key"))), len(transitions)
        )
        self.assertEqual(self.lead.marketing_event_sequence, initial_sequence + 2)
        self.assertEqual(len(self._events(event_type="qualified")), 1)

    def test_won_and_lost_are_explicit(self):
        self.lead.write({"stage_id": self.stage_won.id})
        self.assertEqual(len(self._events(event_type="won")), 1)
        reason = self.env["crm.lost.reason"].create({"name": "Not now"})
        self.lead.action_set_lost(lost_reason_id=reason.id)
        lost = self._events(event_type="lost")
        self.assertEqual(len(lost), 1)
        self.assertEqual(
            lost.snapshot_json["extensions"]["crm.lost_reason_id"], reason.id
        )

    def test_event_replay_and_attribution_link_are_idempotent_many_to_many(self):
        first_event = self.service._ingest_lead_event(
            self.lead,
            "lead_stage_changed",
            "manual-replay",
            occurred_at=datetime.datetime(2026, 9, 1, 13, 0),
            extensions={"crm.old_stage_id": 1, "crm.new_stage_id": 2},
        )
        replayed_event = self.service._ingest_lead_event(
            self.lead,
            "lead_stage_changed",
            "manual-replay",
            occurred_at=datetime.datetime(2026, 9, 1, 13, 0),
            extensions={"crm.old_stage_id": 1, "crm.new_stage_id": 2},
        )
        self.assertEqual(first_event, replayed_event)

        first_link = self.service._link_touchpoint_lead(
            self.touchpoint, self.lead, "test"
        )
        replayed_link = self.service._link_touchpoint_lead(
            self.touchpoint, self.lead, "test"
        )
        other_lead = self.env["crm.lead"].create(
            {
                "name": "Second attribution target",
                "company_id": self.env.company.id,
                "stage_id": self.stage_a.id,
            }
        )
        other_link = self.service._link_touchpoint_lead(
            self.touchpoint, other_lead, "test"
        )
        self.assertEqual(first_link, replayed_link)
        self.assertNotEqual(first_link, other_link)
        self.assertEqual(self.touchpoint.crm_lead_count, 2)
        with self.assertRaises(AccessError):
            first_link.sudo().unlink()
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.crm.link"].sudo().create(
                {
                    "company_id": self.env.company.id,
                    "touchpoint_id": self.touchpoint.id,
                    "lead_id": self.lead.id,
                    "source_ref": "bypass",
                }
            )

    def test_authority_revocations_are_append_only_and_project_independently(self):
        first = self.service._link_touchpoint_lead(
            self.touchpoint,
            self.lead,
            "source:first",
            authority_key="test.first",
            authority_ref="record:one",
            assertion_ref="assertion:one",
        )
        second = self.service._link_touchpoint_lead(
            self.touchpoint,
            self.lead,
            "source:second",
            authority_key="test.second",
            authority_ref="record:two",
            assertion_ref="assertion:two",
        )
        self.assertNotEqual(first, second)
        effective = self.env["marketing.attribution.crm.effective.link"].search(
            [
                ("touchpoint_id", "=", self.touchpoint.id),
                ("lead_id", "=", self.lead.id),
            ]
        )
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective.assertion_count, 2)

        with self.assertRaises(ValidationError):
            self.service._revoke_attribution_assertions(
                first,
                "test.second",
                "record:two",
                "remove:foreign-authority",
            )
        first_revocation = self.service._revoke_attribution_assertions(
            first,
            "test.first",
            "record:one",
            "remove:first",
            reason="test_source_removed",
        )
        replayed_revocation = self.service._revoke_attribution_assertions(
            first,
            "test.first",
            "record:one",
            "remove:first",
            reason="test_source_removed",
        )
        self.assertEqual(first_revocation, replayed_revocation)
        with self.assertRaises(ValidationError):
            self.service._revoke_attribution_assertions(
                first,
                "test.first",
                "record:one",
                "remove:first:conflict",
                reason="test_source_removed",
            )
        effective = self.env["marketing.attribution.crm.effective.link"].search(
            [
                ("touchpoint_id", "=", self.touchpoint.id),
                ("lead_id", "=", self.lead.id),
            ]
        )
        self.assertEqual(effective.assertion_count, 1)

        self.service._revoke_attribution_assertions(
            second,
            "test.second",
            "record:two",
            "remove:second",
            reason="test_source_removed",
        )
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                [
                    ("touchpoint_id", "=", self.touchpoint.id),
                    ("lead_id", "=", self.lead.id),
                ]
            )
        )
        self.lead.invalidate_recordset(["marketing_touchpoint_count"])
        self.assertEqual(self.lead.marketing_touchpoint_count, 0)
        with self.assertRaises(AccessError):
            first_revocation.sudo().write({"reason": "tampered"})
        with self.assertRaises(AccessError):
            first_revocation.sudo().unlink()
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.crm.revocation"].sudo().create(
                {
                    "company_id": self.env.company.id,
                    "assertion_id": first.id,
                    "authority_key": "test.first",
                    "authority_ref": "record:one",
                    "revocation_ref": "bypass",
                    "reason": "bypass",
                }
            )

    def test_assertion_idempotency_reference_fails_closed_on_target_reuse(self):
        self.service._link_touchpoint_lead(
            self.touchpoint,
            self.lead,
            "source:collision",
            authority_key="test.collision",
            authority_ref="record:collision",
            assertion_ref="same-reference",
        )
        with self.assertRaises(ValidationError):
            self.service._link_touchpoint_lead(
                self.touchpoint,
                self.lead,
                "source:changed",
                authority_key="test.collision",
                authority_ref="record:collision",
                assertion_ref="same-reference",
            )
        other_lead = self.env["crm.lead"].create(
            {
                "name": "Assertion collision target",
                "company_id": self.env.company.id,
                "stage_id": self.stage_a.id,
            }
        )
        with self.assertRaises(ValidationError):
            self.service._link_touchpoint_lead(
                self.touchpoint,
                other_lead,
                "source:collision",
                authority_key="test.collision",
                authority_ref="record:collision",
                assertion_ref="same-reference",
            )

    def test_crm_count_and_action_follow_the_effective_canonical_revision(self):
        self.service._link_touchpoint_lead(self.touchpoint, self.lead, "effective")
        base_values = {
            "source_system": self.touchpoint.source_system,
            "source_scope_ref": self.touchpoint.source_scope_ref,
            "source_occurrence_ref": self.touchpoint.source_occurrence_ref,
            "occurred_at": self.touchpoint.occurred_at,
            "platform": self.touchpoint.platform,
            "channel": self.touchpoint.channel,
            "touchpoint_type": self.touchpoint.touchpoint_type,
            "evidence_level": self.touchpoint.evidence_level,
        }
        conflict = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.env.company,
            MarketingTouchpointDTO(
                **base_values,
                source_evidence_ref="crm-conflict:%s" % uuid.uuid4(),
                utm={"campaign": "unaccepted-conflict"},
            ),
        )
        conflict_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            conflict.touchpoint_id
        )
        self.service._link_touchpoint_lead(
            conflict_touchpoint, self.lead, "conflicting-revision"
        )
        self.lead.invalidate_recordset(["marketing_touchpoint_count"])
        self.assertEqual(self.lead.marketing_touchpoint_count, 1)
        self.assertEqual(conflict_touchpoint.crm_lead_count, 1)
        action = self.lead.action_view_marketing_touchpoints()
        self.assertEqual(
            action["res_model"], "marketing.attribution.effective.touchpoint"
        )
        self.assertEqual(action["res_id"], self.touchpoint.id)

        correction = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.env.company,
            MarketingTouchpointDTO(
                **base_values,
                source_evidence_ref="crm-correction:%s" % uuid.uuid4(),
                revision_kind="correction",
                utm={"campaign": "accepted-correction"},
            ),
        )
        self.lead.invalidate_recordset(["marketing_touchpoint_count"])
        self.assertEqual(self.lead.marketing_touchpoint_count, 1)
        action = self.lead.action_view_marketing_touchpoints()
        self.assertEqual(action["res_id"], correction.touchpoint_id)
        effective = self.env["marketing.attribution.effective.touchpoint"].browse(
            correction.touchpoint_id
        )
        self.assertEqual(effective.crm_lead_count, 1)

    def test_cross_company_links_are_rejected(self):
        other_company = self.env["res.company"].create({"name": "CRM Other Company"})
        other_lead = (
            self.env["crm.lead"]
            .with_context(allowed_company_ids=[self.env.company.id, other_company.id])
            .create(
                {
                    "name": "Other-company lead",
                    "company_id": other_company.id,
                    "stage_id": self.stage_a.id,
                }
            )
        )
        with self.assertRaises(ValidationError):
            self.service.with_context(
                allowed_company_ids=[self.env.company.id, other_company.id]
            )._link_touchpoint_lead(self.touchpoint, other_lead, "cross-company")

    def test_global_lead_keeps_a_stable_marketing_company(self):
        global_lead = self.env["crm.lead"].create(
            {
                "name": "Global lead with stable marketing scope",
                "company_id": False,
                "stage_id": self.stage_a.id,
            }
        )
        self.assertEqual(global_lead.marketing_event_company_id, self.env.company)
        other_company = self.env["res.company"].create({"name": "Later Company"})
        with self.assertRaises(ValidationError):
            global_lead.with_context(
                allowed_company_ids=[self.env.company.id, other_company.id]
            ).write({"company_id": other_company.id})
        global_lead.write({"company_id": self.env.company.id})
        self.assertEqual(global_lead.marketing_event_company_id, self.env.company)

    def test_backfill_is_paginated_and_does_not_duplicate_live_tracking(self):
        self.lead.write({"stage_id": self.stage_b.id})
        before = self._events()
        first_page = self.service._backfill_lead_events(
            self.lead, after_tracking_id=0, limit=1
        )
        self.assertLessEqual(first_page["processed"], 1)
        cursor = first_page["last_tracking_id"]
        while first_page["has_more"]:
            first_page = self.service._backfill_lead_events(
                self.lead, after_tracking_id=cursor, limit=1
            )
            self.assertGreaterEqual(first_page["last_tracking_id"], cursor)
            cursor = first_page["last_tracking_id"]
        self.assertEqual(self._events(), before)

        manager_group = self.env.ref(
            "marketing_center_base.group_marketing_center_manager"
        )
        manager = self._create_salesperson(
            "manager",
            manager_group | self.env.ref("sales_team.group_sale_salesman_all_leads"),
        )
        action = self.lead.with_user(manager).action_backfill_marketing_events()
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(self._events(), before)

    def test_link_rules_follow_crm_ownership(self):
        sales_group = self.env.ref("sales_team.group_sale_salesman")
        owner = self._create_salesperson("owner", sales_group)
        outsider = self._create_salesperson("outsider", sales_group)
        self.lead.write({"user_id": owner.id})
        attribution_link = self.service._link_touchpoint_lead(
            self.touchpoint, self.lead, "security-test"
        )
        effective_link = self.env["marketing.attribution.crm.effective.link"].search(
            [
                ("touchpoint_id", "=", self.touchpoint.id),
                ("lead_id", "=", self.lead.id),
            ]
        )
        event_link = self.lead.marketing_business_event_link_ids[:1]

        attribution_link.with_user(owner).read(["id"])
        effective_link.with_user(owner).read(["id"])
        event_link.with_user(owner).read(["id"])
        with self.assertRaises(AccessError):
            self.touchpoint.with_user(owner).read(["public_ref"])
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.effective.touchpoint"].with_user(
                owner
            ).browse(self.touchpoint.id).read(["public_ref"])
        with self.assertRaises(AccessError):
            attribution_link.with_user(outsider).read(["id"])
        with self.assertRaises(AccessError):
            effective_link.with_user(outsider).read(["id"])
        with self.assertRaises(AccessError):
            event_link.with_user(outsider).read(["id"])

    def test_lead_unlink_preserves_auditable_evidence_and_hides_live_actions(self):
        owner = self._create_salesperson(
            "tombstone-owner", self.env.ref("sales_team.group_sale_salesman")
        )
        all_leads_user = self._create_salesperson(
            "tombstone-auditor",
            self.env.ref("sales_team.group_sale_salesman_all_leads"),
        )
        self.lead.write({"user_id": owner.id})
        assertion = self.service._link_touchpoint_lead(
            self.touchpoint,
            self.lead,
            "unlink-regression",
            authority_key="test.unlink",
            authority_ref="lead:%s" % self.lead.id,
            assertion_ref="unlink:%s" % self.lead.id,
        )
        event_link = self.lead.marketing_business_event_link_ids[:1]
        event = event_link.event_id
        revoked_assertion = self.service._link_touchpoint_lead(
            self.touchpoint,
            self.lead,
            "unlink-revoked",
            authority_key="test.unlink.revoked",
            authority_ref="lead:%s" % self.lead.id,
            assertion_ref="unlink-revoked:%s" % self.lead.id,
        )
        revocation = self.service._revoke_attribution_assertions(
            revoked_assertion,
            "test.unlink.revoked",
            "lead:%s" % self.lead.id,
            "unlink-revocation:%s" % self.lead.id,
        )
        lead_id = self.lead.id
        lead_name = self.lead.name

        self.assertTrue(self.lead.unlink())
        self.assertFalse(self.env["crm.lead"].browse(lead_id).exists())
        assertion.invalidate_recordset(["lead_id"])
        event_link.invalidate_recordset(["lead_id"])
        revocation.invalidate_recordset(["lead_id"])
        self.assertFalse(assertion.lead_id)
        self.assertEqual(assertion.lead_model, "crm.lead")
        self.assertEqual(assertion.lead_res_id, lead_id)
        self.assertEqual(assertion.lead_display_ref, lead_name)
        self.assertFalse(revocation.lead_id)
        self.assertEqual(revocation.lead_res_id, lead_id)
        self.assertFalse(event_link.lead_id)
        self.assertEqual(event_link.lead_res_id, lead_id)
        with self.assertRaises(AccessError):
            assertion.with_user(owner).read(["id"])
        self.assertEqual(
            assertion.with_user(all_leads_user).read(["id"])[0]["id"], assertion.id
        )
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                [("canonical_key", "=", self.touchpoint.canonical_key)]
            )
        )
        event.invalidate_recordset(["crm_lead_count"])
        self.assertEqual(event.crm_lead_count, 0)
        self.assertEqual(event.action_view_crm_leads()["domain"], [("id", "in", [])])

    def test_native_merge_appends_equivalence_and_reprojects_active_evidence(self):
        source = self.env["crm.lead"].create(
            {
                "name": "Merge source",
                "company_id": self.env.company.id,
                "stage_id": self.stage_a.id,
                "probability": 10,
            }
        )
        target = self.env["crm.lead"].create(
            {
                "name": "Merge target",
                "company_id": self.env.company.id,
                "stage_id": self.stage_a.id,
                "probability": 90,
            }
        )
        source_assertion = self.service._link_touchpoint_lead(
            self.touchpoint,
            source,
            "merge-source",
            authority_key="test.merge.source",
            authority_ref="lead:%s" % source.id,
            assertion_ref="merge-source:%s" % source.id,
        )
        source_event_links = source.marketing_business_event_link_ids
        source_event_ids = set(source_event_links.mapped("event_id").ids)
        source_id = source.id

        merged = (source | target).merge_opportunity()
        self.assertEqual(merged, target)
        self.assertFalse(self.env["crm.lead"].browse(source_id).exists())
        source_assertion.invalidate_recordset(["lead_id"])
        source_event_links.invalidate_recordset(["lead_id"])
        self.assertFalse(source_assertion.lead_id)
        self.assertTrue(all(not link.lead_id for link in source_event_links))

        equivalence = self.env["marketing.crm.lead.equivalence"].search(
            [("source_lead_res_id", "=", source_id)]
        )
        self.assertEqual(len(equivalence), 1)
        self.assertEqual(equivalence.source_lead_display_ref, "Merge source")
        self.assertEqual(equivalence.target_lead_id, target)
        self.assertEqual(equivalence.target_lead_res_id, target.id)
        projected = self.env["marketing.attribution.crm.effective.link"].search(
            [
                ("canonical_key", "=", self.touchpoint.canonical_key),
                ("lead_id", "=", target.id),
            ]
        )
        self.assertEqual(len(projected), 1)
        merge_assertion = self.env["marketing.attribution.crm.link"].search(
            [
                ("lead_id", "=", target.id),
                ("authority_key", "=", "test.merge.source"),
            ]
        )
        self.assertEqual(len(merge_assertion), 1)
        self.assertEqual(merge_assertion.derived_from_assertion_id, source_assertion)
        target_event_ids = set(
            target.marketing_business_event_link_ids.mapped("event_id").ids
        )
        self.assertTrue(source_event_ids.issubset(target_event_ids))

        self.service._revoke_attribution_assertions(
            source_assertion,
            "test.merge.source",
            "lead:%s" % source_id,
            "merge-source-revoked:%s" % source_id,
        )
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                [
                    ("canonical_key", "=", self.touchpoint.canonical_key),
                    ("lead_id", "=", target.id),
                ]
            )
        )

        target_id = target.id
        self.assertTrue(target.unlink())
        equivalence.invalidate_recordset(["target_lead_id"])
        self.assertFalse(equivalence.target_lead_id)
        self.assertEqual(equivalence.target_lead_res_id, target_id)
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                [("canonical_key", "=", self.touchpoint.canonical_key)]
            )
        )

    def test_native_merge_rejects_cross_company_marketing_transfer(self):
        other_company = self.env["res.company"].create({"name": "Merge other company"})
        leads = self.env["crm.lead"].with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]
        )
        local = leads.with_company(self.env.company).create(
            {"name": "Local merge", "company_id": self.env.company.id}
        )
        foreign = leads.with_company(other_company).create(
            {"name": "Foreign merge", "company_id": other_company.id}
        )
        with self.assertRaises(ValidationError):
            (local | foreign).merge_opportunity()
        self.assertTrue(local.exists())
        self.assertTrue(foreign.exists())

    @classmethod
    def _create_salesperson(cls, label, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing CRM %s" % label,
                    "login": "marketing-crm-%s-%s" % (label, uuid.uuid4()),
                    "email": "%s@example.invalid" % label,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )
