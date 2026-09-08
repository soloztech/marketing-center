import datetime
import hashlib
import uuid
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.adapter import (
    ProviderAdapter,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.dto import AdapterResult
from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_ATTRIBUTION_TOKEN,
)
from odoo.addons.contact_center_crm.models.conversation_link import (
    conversation_graph_is_locked,
)
from odoo.addons.marketing_center_contact_center_crm.models import (
    crm_lead as bridge_crm_lead,
    service as bridge_service,
)
from odoo.addons.marketing_center_crm.models import (
    crm_lead as marketing_crm_lead,
    service as marketing_crm_service,
)
from odoo.addons.queue_job.tests.common import trap_jobs


@adapter_registry.register("test.marketing.cc.crm")
class MarketingContactCenterCrmTestAdapter(ProviderAdapter):
    display_name = "Marketing Contact Center CRM Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        raise NotImplementedError

    def execute_command(self, connection, command):
        return AdapterResult.success(external_message_id="unused")

    def prepare_request_snapshot(self, connection, command):
        return {"provider": self.key, "method": "POST", "endpoint": "/unused"}

    def get_capabilities(self, connection):
        return {}

    def get_health(self, connection):
        return {"state": "connected"}


class TestMarketingContactCenterCrmConvergence(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = str(uuid.uuid4())
        cls.cc_agent = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.crm_user = cls.env.ref("sales_team.group_sale_salesman")
        cls.user = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CC marketing CRM agent",
                    "login": "cc-marketing-crm-%s" % suffix,
                    "email": "cc-marketing-crm@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, (cls.cc_agent | cls.crm_user).ids)],
                }
            )
        )
        cls.crm_team = cls.env["crm.team"].create(
            {
                "name": "CC marketing CRM %s" % suffix,
                "company_id": cls.env.company.id,
                "user_id": cls.user.id,
            }
        )
        cls.crm_stage = cls.env["crm.stage"].create(
            {
                "name": "CC marketing initial %s" % suffix,
                "sequence": 401,
                "team_id": cls.crm_team.id,
            }
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "CC marketing team %s" % suffix,
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.user.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "CC marketing account %s" % suffix,
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "cc-marketing-account-%s" % suffix,
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "CC marketing provider %s" % suffix,
                "account_id": cls.account.id,
                "adapter_key": "test.marketing.cc.crm",
                "external_ref": "cc-marketing-provider-%s" % suffix,
                "provider_schema_version": "fixture-v1",
                "state": "connected",
            }
        )
        cls.convergence = cls.env["marketing.contact.center.crm.service"]

    def _channel(self, label):
        guest = self.env["mail.guest"].sudo().create({"name": label})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": label,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            guest_ids=guest.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": self.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": str(uuid.uuid4()),
                }
            )
        )
        return channel, binding

    def _lead(self, label):
        return self.env["crm.lead"].create(
            {
                "name": label,
                "company_id": self.env.company.id,
                "team_id": self.crm_team.id,
                "stage_id": self.crm_stage.id,
                "user_id": self.user.id,
            }
        )

    def _link_conversation(self, channel, lead, reconcile=True):
        link = (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.user)
            ._link(channel.with_user(self.user), lead.with_user(self.user))
        )
        if reconcile:
            link.company_id._job_marketing_contact_center_crm_conversation_link(link.id)
        return link

    def _source_and_projection(
        self,
        binding,
        label,
        bind_source=True,
        reconcile=True,
    ):
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "marketing-cc-crm:%s" % label,
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": label},
                }
            )
        )
        source_values = {
            "company_id": self.env.company.id,
            "account_id": self.account.id,
            "provider_connection_id": self.connection.id,
            "inbox_event_id": inbox.id,
            "evidence_inbox_event_ids": [(6, 0, inbox.ids)],
            "occurred_at": datetime.datetime(2026, 9, 1, 12, 0),
            "captured_at": datetime.datetime(2026, 9, 1, 12, 1),
            "conversation_ref": binding.conversation_ref,
            "conversation_address_fingerprint": digest,
            "source_key_kind": "event",
            "source_external_key": "event:%s" % label,
            "canonical_key": digest,
            "attribution_fingerprint": digest,
            "touchpoint_type": "paid_ad_click",
            "evidence_level": "provider_asserted",
            "network": "meta",
            "source_platform": "facebook",
        }
        if bind_source:
            source_values.update(
                {
                    "channel_binding_id": binding.id,
                    "identity_id": binding.identity_id.id,
                }
            )
        source = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .with_context(
                contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
                marketing_contact_center_skip_enqueue=True,
            )
            .create(source_values)
        )
        bridge_link = self.env[
            "marketing.contact.center.attribution.service"
        ]._sync_touchpoint(source)
        if reconcile and bridge_link:
            bridge_link.company_id._job_marketing_contact_center_crm_attribution_link(
                bridge_link.id
            )
        return source, bridge_link

    def test_conversation_first_then_attribution_converges(self):
        channel, binding = self._channel("conversation-first")
        lead = self._lead("Conversation first")
        self._link_conversation(channel, lead)

        _source, bridge_link = self._source_and_projection(
            binding, "conversation-first"
        )

        links = self.env["marketing.attribution.crm.link"].search(
            [
                ("touchpoint_id", "=", bridge_link.marketing_touchpoint_id.id),
                ("lead_id", "=", lead.id),
            ]
        )
        self.assertEqual(len(links), 1)
        self.assertIn("contact_center:conversation:", links.source_ref)

    def test_prebaseline_assertion_cannot_replace_current_conversation_link_identity(
        self,
    ):
        channel, binding = self._channel("current-assertion-identity")
        lead = self._lead("Current assertion identity")
        _source, bridge = self._source_and_projection(
            binding, "current-assertion-identity"
        )
        touchpoint = bridge.marketing_touchpoint_id
        prebaseline = self.env["marketing.crm.service"]._link_touchpoint_lead(
            touchpoint,
            lead,
            "contact_center:conversation:%s:canonical:%s"
            % (str(channel.id), touchpoint.canonical_key),
            authority_key="contact_center.conversation",
            authority_ref=str(channel.id),
            assertion_ref="prebaseline-contact-center:%s" % uuid.uuid4(),
        )

        conversation_link = self._link_conversation(channel, lead)
        assertions = self.env["marketing.attribution.crm.link"].search(
            [
                ("touchpoint_id", "=", touchpoint.id),
                ("lead_id", "=", lead.id),
                ("authority_key", "=", "contact_center.conversation"),
                ("authority_ref", "=", str(channel.id)),
            ]
        )

        self.assertIn(prebaseline, assertions)
        self.assertEqual(len(assertions), 2)
        current = assertions - prebaseline
        self.assertEqual(
            current.assertion_ref,
            "conversation-link:%s:canonical:%s:lead:%s"
            % (conversation_link.id, touchpoint.canonical_key, lead.id),
        )
        self.assertEqual(self.convergence._reconcile_channel(channel), current)

    def test_attribution_first_then_conversation_converges(self):
        channel, binding = self._channel("attribution-first")
        _source, bridge_link = self._source_and_projection(binding, "attribution-first")
        self.assertFalse(bridge_link.marketing_touchpoint_id.crm_link_ids)

        lead = self._lead("Attribution first")
        self._link_conversation(channel, lead)

        self.assertEqual(
            bridge_link.marketing_touchpoint_id.crm_link_ids.mapped("lead_id"), lead
        )

    def test_many_links_many_leads_and_replay_are_idempotent(self):
        channel, binding = self._channel("many-links")
        leads = self._lead("Proposal A") | self._lead("Proposal B")
        self._link_conversation(channel, leads[0])
        self._link_conversation(channel, leads[1])
        _source_a, bridge_a = self._source_and_projection(binding, "many-links-a")
        _source_b, bridge_b = self._source_and_projection(binding, "many-links-b")

        touchpoints = (
            bridge_a.marketing_touchpoint_id | bridge_b.marketing_touchpoint_id
        )
        links = self.env["marketing.attribution.crm.link"].search(
            [
                ("touchpoint_id", "in", touchpoints.ids),
                ("lead_id", "in", leads.ids),
            ]
        )
        self.assertEqual(len(links), 4)

        first_replay = self.convergence._reconcile_channel(channel)
        second_replay = self.convergence._reconcile_channel(channel)
        self.assertEqual(set(first_replay.ids), set(links.ids))
        self.assertEqual(set(second_replay.ids), set(links.ids))
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search_count(
                [
                    ("touchpoint_id", "in", touchpoints.ids),
                    ("lead_id", "in", leads.ids),
                ]
            ),
            4,
        )

    def test_one_lead_in_two_conversations_receives_both_touchpoints(self):
        lead = self._lead("One lead, two conversations")
        touchpoints = self.env["marketing.attribution.touchpoint"]
        for label in ("conversation-a", "conversation-b"):
            channel, binding = self._channel(label)
            self._link_conversation(channel, lead)
            _source, bridge = self._source_and_projection(binding, label)
            touchpoints |= bridge.marketing_touchpoint_id

        links = self.env["marketing.attribution.crm.link"].search(
            [("touchpoint_id", "in", touchpoints.ids), ("lead_id", "=", lead.id)]
        )
        self.assertEqual(len(links), 2)

    def test_conversation_unlink_revokes_only_its_authority_and_relink_recovers(self):
        channel, binding = self._channel("authority-revocation")
        lead = self._lead("Authority revocation")
        conversation_link = self._link_conversation(channel, lead)
        _source, bridge = self._source_and_projection(binding, "authority-revocation")
        touchpoint = bridge.marketing_touchpoint_id
        conversation_assertion = self.env["marketing.attribution.crm.link"].search(
            [
                ("touchpoint_id", "=", touchpoint.id),
                ("lead_id", "=", lead.id),
                ("authority_key", "=", "contact_center.conversation"),
                ("authority_ref", "=", str(channel.id)),
            ]
        )
        self.assertEqual(len(conversation_assertion), 1)

        manual_assertion = self.env["marketing.crm.service"]._link_touchpoint_lead(
            touchpoint,
            lead,
            "manual:independent",
            authority_key="test.manual",
            authority_ref="manual:independent",
            assertion_ref="manual:independent",
        )
        effective_domain = [
            ("touchpoint_id", "=", touchpoint.id),
            ("lead_id", "=", lead.id),
        ]
        effective = self.env["marketing.attribution.crm.effective.link"].search(
            effective_domain
        )
        self.assertEqual(effective.assertion_count, 2)

        conversation_link._tombstone()
        conversation_link.invalidate_recordset(["lead_id", "state"])
        self.assertFalse(conversation_link.lead_id)
        self.assertEqual(conversation_link.state, "unlinked")
        self.assertEqual(len(conversation_assertion.revocation_ids), 1)
        self.assertFalse(manual_assertion.revocation_ids)
        effective = self.env["marketing.attribution.crm.effective.link"].search(
            effective_domain
        )
        self.assertEqual(effective.assertion_count, 1)

        new_conversation_link = self._link_conversation(channel, lead)
        self.assertNotEqual(new_conversation_link.id, conversation_link.id)
        active_conversation_assertions = self.env[
            "marketing.attribution.crm.link"
        ].search(
            [
                ("touchpoint_id", "=", touchpoint.id),
                ("lead_id", "=", lead.id),
                ("authority_key", "=", "contact_center.conversation"),
                ("authority_ref", "=", str(channel.id)),
                ("revocation_ids", "=", False),
            ]
        )
        self.assertEqual(len(active_conversation_assertions), 1)
        effective = self.env["marketing.attribution.crm.effective.link"].search(
            effective_domain
        )
        effective.invalidate_recordset(["assertion_count"])
        self.assertEqual(effective.assertion_count, 2)

    def test_conversation_unlink_removes_projection_without_other_authority(self):
        channel, binding = self._channel("sole-authority-revocation")
        lead = self._lead("Sole authority revocation")
        conversation_link = self._link_conversation(channel, lead)
        _source, bridge = self._source_and_projection(
            binding, "sole-authority-revocation"
        )
        touchpoint = bridge.marketing_touchpoint_id
        domain = [
            ("touchpoint_id", "=", touchpoint.id),
            ("lead_id", "=", lead.id),
        ]
        self.assertTrue(
            self.env["marketing.attribution.crm.effective.link"].search(domain)
        )

        conversation_link._tombstone()
        conversation_link.invalidate_recordset(["lead_id", "state"])
        self.assertFalse(conversation_link.lead_id)
        self.assertEqual(conversation_link.state, "unlinked")

        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(domain)
        )

    def test_tombstoned_conversation_link_is_not_replayed(self):
        channel, binding = self._channel("tombstone-replay")
        lead = self._lead("Tombstone replay")
        conversation_link = self._link_conversation(channel, lead)
        lead.unlink()
        conversation_link.invalidate_recordset(["lead_id", "state"])
        self.assertFalse(conversation_link.lead_id)
        self.assertEqual(conversation_link.state, "unlinked")

        _source, bridge = self._source_and_projection(binding, "after-tombstone")

        self.assertFalse(bridge.marketing_touchpoint_id.crm_link_ids)
        self.assertFalse(self.convergence._reconcile_channel(channel))

    def test_late_conversation_resolution_converges_existing_projection(self):
        channel, binding = self._channel("late-resolution")
        lead = self._lead("Late conversation resolution")
        self._link_conversation(channel, lead)
        source, bridge = self._source_and_projection(
            binding,
            "late-resolution",
            bind_source=False,
        )
        self.assertFalse(bridge.marketing_touchpoint_id.crm_link_ids)

        source.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).write(
            {
                "channel_binding_id": binding.id,
                "identity_id": binding.identity_id.id,
            }
        )
        bridge.company_id._job_marketing_contact_center_crm_attribution_link(bridge.id)

        bridge.marketing_touchpoint_id.invalidate_recordset(["crm_link_ids"])
        self.assertEqual(
            bridge.marketing_touchpoint_id.crm_link_ids.mapped("lead_id"),
            lead,
        )

    def test_bounded_replay_rejects_records_from_another_conversation(self):
        channel_a, _binding_a = self._channel("scope-a")
        channel_b, _binding_b = self._channel("scope-b")
        lead = self._lead("Scoped link")
        conversation_link = self._link_conversation(channel_a, lead)

        with self.assertRaises(ValidationError):
            self.convergence._reconcile_channel(
                channel_b,
                conversation_links=conversation_link,
            )

    def test_non_contact_center_channel_is_rejected(self):
        channel = self.env["mail.channel"].create(
            {"name": "Outside convergence", "channel_type": "channel"}
        )
        with self.assertRaises(ValidationError):
            self.convergence._reconcile_channel(channel)

    def test_conversation_create_enqueues_durable_convergence_instead_of_linking_inline(
        self,
    ):
        channel, binding = self._channel("queued-link-hook")
        _source, bridge = self._source_and_projection(
            binding,
            "queued-link-hook",
            reconcile=False,
        )
        lead = self._lead("Queued link hook")

        with trap_jobs() as trap:
            conversation_link = self._link_conversation(
                channel,
                lead,
                reconcile=False,
            )

            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_conversation_link,
                args=(conversation_link.id, 0, 100),
                properties={
                    "identity_key": (
                        "marketing_contact_center_crm:conversation:%s:after_attribution:0"
                        % conversation_link.id
                    ),
                    "priority": 40,
                },
            )
        self.assertFalse(bridge.marketing_touchpoint_id.crm_link_ids)

        self.env.company._job_marketing_contact_center_crm_conversation_link(
            conversation_link.id
        )
        self.assertEqual(
            bridge.marketing_touchpoint_id.crm_link_ids.mapped("lead_id"),
            lead,
        )

    def test_conversation_job_pages_cross_product_and_chains_monotonic_cursor(self):
        channel, binding = self._channel("bounded-link-job")
        lead = self._lead("Bounded link job")
        conversation_link = self._link_conversation(
            channel,
            lead,
            reconcile=False,
        )
        bridges = self.env["marketing.attribution.contact.center.link"]
        for index in range(3):
            _source, bridge = self._source_and_projection(
                binding,
                "bounded-link-job-%s" % index,
                reconcile=False,
            )
            bridges |= bridge

        with trap_jobs() as trap:
            first = (
                self.env.company._job_marketing_contact_center_crm_conversation_link(
                    conversation_link.id,
                    after_attribution_link_id=0,
                    limit=2,
                )
            )

            self.assertFalse(first["done"])
            self.assertEqual(first["processed"], 2)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_conversation_link,
                args=(conversation_link.id, first["last_attribution_link_id"], 2),
            )
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search_count(
                [
                    ("touchpoint_id", "in", bridges.marketing_touchpoint_id.ids),
                    ("lead_id", "=", lead.id),
                ]
            ),
            2,
        )

        second = self.env.company._job_marketing_contact_center_crm_conversation_link(
            conversation_link.id,
            after_attribution_link_id=first["last_attribution_link_id"],
            limit=2,
        )
        self.assertTrue(second["done"])
        self.assertEqual(second["processed"], 1)
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search_count(
                [
                    ("touchpoint_id", "in", bridges.marketing_touchpoint_id.ids),
                    ("lead_id", "=", lead.id),
                ]
            ),
            3,
        )

    def test_attribution_job_pages_conversation_links_and_eventually_converges(self):
        channel, binding = self._channel("bounded-attribution-job")
        leads = self.env["crm.lead"]
        for index in range(3):
            lead = self._lead("Bounded attribution lead %s" % index)
            leads |= lead
            self._link_conversation(channel, lead, reconcile=False)
        _source, bridge = self._source_and_projection(
            binding,
            "bounded-attribution-job",
            reconcile=False,
        )

        with trap_jobs() as trap:
            first = (
                bridge.company_id._job_marketing_contact_center_crm_attribution_link(
                    bridge.id,
                    after_conversation_link_id=0,
                    limit=2,
                )
            )

            self.assertFalse(first["done"])
            self.assertEqual(first["processed"], 2)
            trap.assert_enqueued_job(
                bridge.company_id._job_marketing_contact_center_crm_attribution_link,
                args=(bridge.id, first["last_conversation_link_id"], 2),
            )
        self.assertCountEqual(
            bridge.marketing_touchpoint_id.crm_link_ids.mapped("lead_id").ids,
            leads[:2].ids,
        )

        second = bridge.company_id._job_marketing_contact_center_crm_attribution_link(
            bridge.id,
            after_conversation_link_id=first["last_conversation_link_id"],
            limit=2,
        )
        self.assertTrue(second["done"])
        self.assertEqual(second["processed"], 1)
        bridge.marketing_touchpoint_id.invalidate_recordset(["crm_link_ids"])
        self.assertCountEqual(
            bridge.marketing_touchpoint_id.crm_link_ids.mapped("lead_id").ids,
            leads.ids,
        )

    def test_bridge_fences_contact_graph_before_marketing_stage_lock(self):
        channel, _binding = self._channel("bridge-lock-order")
        lead = self._lead("Bridge lock order")
        self._link_conversation(channel, lead)
        next_stage = self.env["crm.stage"].create(
            {
                "name": "CC marketing next %s" % uuid.uuid4(),
                "sequence": 402,
                "team_id": self.crm_team.id,
            }
        )
        order = []
        original_graph = bridge_crm_lead.CrmLead._marketing_contact_center_lock_graph
        original_marketing = marketing_crm_lead.CrmLead._marketing_lock_event_state

        def record_graph(records, **kwargs):
            order.append("contact_graph")
            return original_graph(records, **kwargs)

        def record_marketing(records):
            self.assertTrue(conversation_graph_is_locked(records.env))
            order.append("marketing_lead")
            return original_marketing(records)

        with patch.object(
            bridge_crm_lead.CrmLead,
            "_marketing_contact_center_lock_graph",
            record_graph,
        ), patch.object(
            marketing_crm_lead.CrmLead,
            "_marketing_lock_event_state",
            record_marketing,
        ):
            lead.with_user(self.user).write({"stage_id": next_stage.id})

        self.assertEqual(order[:2], ["contact_graph", "marketing_lead"])
        self.assertEqual(lead.stage_id, next_stage)
        self.assertEqual(
            self.env["contact.center.crm.conversation.link"]
            .search([("channel_id", "=", channel.id), ("state", "=", "active")])
            .mapped("lead_id"),
            lead,
        )

    def test_bridge_fences_contact_graph_before_marketing_merge_snapshot(self):
        channel, binding = self._channel("bridge-merge-lock-order")
        leads = self._lead("Merge source") | self._lead("Merge survivor")
        conversation_links = self._link_conversation(channel, leads[0])
        conversation_links |= self._link_conversation(channel, leads[1])
        _source, bridge = self._source_and_projection(
            binding,
            "bridge-merge-lock-order",
        )
        order = []
        original_graph = bridge_crm_lead.CrmLead._marketing_contact_center_lock_graph
        original_prepare = marketing_crm_service.MarketingCrmService._prepare_lead_merge

        def record_graph(records, **kwargs):
            order.append("contact_graph")
            return original_graph(records, **kwargs)

        def record_prepare(service, merge_leads):
            self.assertTrue(conversation_graph_is_locked(merge_leads.env))
            order.append("marketing_merge")
            return original_prepare(service, merge_leads)

        with patch.object(
            bridge_crm_lead.CrmLead,
            "_marketing_contact_center_lock_graph",
            record_graph,
        ), patch.object(
            marketing_crm_service.MarketingCrmService,
            "_prepare_lead_merge",
            record_prepare,
        ):
            survivor = leads._merge_opportunity()

        self.assertEqual(order[:2], ["contact_graph", "marketing_merge"])
        conversation_links.invalidate_recordset(["lead_id", "state"])
        active_links = conversation_links.filtered(lambda link: link.state == "active")
        self.assertEqual(len(active_links), 1)
        self.assertEqual(active_links.lead_id, survivor)
        bridge.marketing_touchpoint_id.invalidate_recordset(["crm_link_ids"])
        self.assertEqual(
            bridge.marketing_touchpoint_id.crm_link_ids.mapped("lead_id"),
            survivor,
        )

    def test_merge_moves_link_and_convergence_identity_then_unlink_revokes_all(self):
        channel_a, binding_a = self._channel("merge-distinct-a")
        channel_b, binding_b = self._channel("merge-distinct-b")
        leads = self._lead("Distinct merge A") | self._lead("Distinct merge B")
        links = self._link_conversation(channel_a, leads[0])
        links |= self._link_conversation(channel_b, leads[1])
        _source_a, bridge_a = self._source_and_projection(binding_a, "merge-distinct-a")
        _source_b, bridge_b = self._source_and_projection(binding_b, "merge-distinct-b")
        original_ids = links.ids
        with trap_jobs() as trap:
            survivor = leads._merge_opportunity()
            self.assertTrue(trap.enqueued_jobs)
        links.invalidate_recordset(["lead_id", "state"])
        self.assertEqual(links.ids, original_ids)
        self.assertTrue(all(link.state == "active" for link in links))
        self.assertEqual(links.mapped("lead_id"), survivor)
        for link in links:
            link.company_id._job_marketing_contact_center_crm_conversation_link(link.id)
        for link, bridge in ((links[0], bridge_a), (links[1], bridge_b)):
            expected = self.convergence._assertion_reference(
                link, bridge.marketing_touchpoint_id
            )
            self.assertTrue(
                self.env["marketing.attribution.crm.link"].search(
                    [
                        ("assertion_ref", "=", expected),
                        ("lead_id", "=", survivor.id),
                    ]
                )
            )
        links[0]._tombstone()
        self.assertFalse(
            self.env["marketing.attribution.crm.effective.link"].search(
                [
                    ("touchpoint_id", "=", bridge_a.marketing_touchpoint_id.id),
                    ("lead_id", "=", survivor.id),
                ]
            )
        )
        self.assertTrue(
            self.env["marketing.attribution.crm.effective.link"].search(
                [
                    ("touchpoint_id", "=", bridge_b.marketing_touchpoint_id.id),
                    ("lead_id", "=", survivor.id),
                ]
            )
        )

    def test_attribution_create_enqueues_inverse_bounded_job(self):
        _channel, binding = self._channel("queued-attribution-hook")

        with trap_jobs() as trap:
            _source, bridge = self._source_and_projection(
                binding,
                "queued-attribution-hook",
                reconcile=False,
            )

            trap.assert_enqueued_job(
                bridge.company_id._job_marketing_contact_center_crm_attribution_link,
                args=(bridge.id, 0, 100),
                properties={
                    "identity_key": (
                        "marketing_contact_center_crm:attribution:%s:after_conversation:0"
                        % bridge.id
                    ),
                    "priority": 40,
                },
            )

    def test_convergence_job_cannot_cross_company_boundary(self):
        channel, _binding = self._channel("job-company-boundary")
        lead = self._lead("Job company boundary")
        conversation_link = self._link_conversation(
            channel,
            lead,
            reconcile=False,
        )
        other_company = self.env["res.company"].create(
            {"name": "Other convergence company %s" % uuid.uuid4()}
        )

        result = other_company._job_marketing_contact_center_crm_conversation_link(
            conversation_link.id
        )

        self.assertTrue(result["done"])
        self.assertEqual(result["processed"], 0)

    def test_backfill_page_only_enqueues_bounded_conversation_jobs(self):
        channel_a, _binding_a = self._channel("bounded-backfill-a")
        channel_b, _binding_b = self._channel("bounded-backfill-b")
        with trap_jobs():
            first = self._link_conversation(
                channel_a,
                self._lead("Bounded backfill A"),
                reconcile=False,
            )
            self._link_conversation(
                channel_b,
                self._lead("Bounded backfill B"),
                reconcile=False,
            )

        with trap_jobs() as trap:
            result = self.convergence._reconcile_existing(
                company=self.env.company,
                after_conversation_link_id=0,
                limit=1,
            )

            self.assertEqual(result["processed_conversation_links"], 1)
            self.assertEqual(result["enqueued_conversation_links"], 1)
            self.assertTrue(result["has_more"])
            self.assertEqual(result["last_conversation_link_id"], first.id)
            trap.assert_enqueued_job(
                self.env.company._job_marketing_contact_center_crm_conversation_link,
                args=(first.id, 0, 100),
            )

    def test_convergence_locks_contact_graph_before_channel_serialization(self):
        channel, binding = self._channel("convergence-lock-order")
        self._link_conversation(
            channel,
            self._lead("Convergence lock order"),
        )
        self._source_and_projection(binding, "convergence-lock-order")
        order = []
        original_graph = (
            bridge_service.MarketingContactCenterCrmService._lock_conversation_link_graph
        )
        original_channel = bridge_service.MarketingContactCenterCrmService._lock_channel

        def record_graph(service, conversation_links):
            order.append("contact_graph")
            return original_graph(service, conversation_links)

        def record_channel(service, locked_channel):
            order.append("channel")
            return original_channel(service, locked_channel)

        with patch.object(
            bridge_service.MarketingContactCenterCrmService,
            "_lock_conversation_link_graph",
            record_graph,
        ), patch.object(
            bridge_service.MarketingContactCenterCrmService,
            "_lock_channel",
            record_channel,
        ):
            self.convergence._reconcile_channel(channel)

        self.assertEqual(order[:2], ["contact_graph", "channel"])
