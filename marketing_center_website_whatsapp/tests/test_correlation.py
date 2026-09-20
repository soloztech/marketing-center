import datetime
import hashlib
import uuid
from unittest.mock import patch

from markupsafe import escape
from psycopg2 import OperationalError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
)


class TestWebsiteWhatsappCorrelation(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.when = datetime.datetime(2026, 9, 20, 12, 0)
        cls.agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.crm_group = cls.env.ref("sales_team.group_sale_salesman")
        cls.agent = cls._user("WhatsApp attribution agent")
        cls.outsider = cls._user("Unassigned attribution agent")
        cls.account = cls._account(cls.env.company)
        cls.origin = "https://whatsapp.example.test"
        cls.website = cls.env["website"].create({
            "name": "WhatsApp correlation test", "domain": cls.origin,
            "company_id": cls.env.company.id,
        })
        endpoint = cls.env["marketing.web.ingress.endpoint"].create({
            "name": "WhatsApp test", "company_id": cls.env.company.id,
            "allowed_origins": cls.origin, "allowed_hosts": "whatsapp.example.test",
            "capture_enabled": True, "capture_purpose": "website_attribution",
            "website_tracking_policy": "informational_notice",
            "privacy_policy_version": "whatsapp-v1", "privacy_notice_version": "whatsapp-v1",
            "privacy_policy_justification": "Synthetic test", "identifier_retention_days": 30,
        })
        ingress = cls.env["marketing.website.ingress.binding"].create({
            "website_id": cls.website.id, "endpoint_id": endpoint.id,
        })
        cls.action = cls.env["marketing.website.action"].create({
            "name": "WhatsApp test", "binding_id": ingress.id,
            "kind": "whatsapp_handoff", "route_ref": "whatsapp.test", "source_path": "/",
            "whatsapp_destination": "5511999999999", "handoff_enabled": True,
            "handoff_account_id": cls.account.id, "handoff_reference_prefix": "CP",
        })
        cls.matches = cls.env["marketing.website.whatsapp.match"]
        cls.service = cls.env["marketing.website.whatsapp.correlation"]

    @classmethod
    def _user(cls, name):
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": name, "login": "whatsapp-correlation-%s" % uuid.uuid4(),
            "company_id": cls.env.company.id, "company_ids": [(6, 0, cls.env.company.ids)],
            "groups_id": [(6, 0, (cls.agent_group | cls.crm_group).ids)],
        })

    @classmethod
    def _account(cls, company):
        team = cls.env["contact.center.team"].create({
            "name": "WhatsApp correlation %s" % uuid.uuid4(),
            "company_id": company.id, "agent_ids": [(6, 0, cls.agent.ids)],
        })
        return cls.env["contact.center.account"].create({
            "name": "WhatsApp inbox %s" % uuid.uuid4(), "company_id": company.id,
            "platform": "whatsapp", "own_external_identity": "5511999999999@s.whatsapp.net",
            "access_team_ids": [(6, 0, team.ids)],
        })

    def _channel(self, account=None, conversation_type="direct"):
        account = account or self.account
        guest = self.env["mail.guest"].sudo().create({"name": "Synthetic WhatsApp visitor"})
        identity = self.env["contact.center.identity"].sudo().create({
            "name": "Synthetic visitor", "company_id": account.company_id.id,
            "mail_guest_id": guest.id,
        })
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=account, identity=identity if conversation_type == "direct" else None,
            conversation_type=conversation_type, guest_ids=guest.ids,
        )
        return self.env["contact.center.channel.binding"].sudo().create({
            "channel_id": channel.id, "account_id": account.id,
            "identity_id": identity.id if conversation_type == "direct" else False,
            "conversation_type": conversation_type, "conversation_ref": str(uuid.uuid4()),
        })

    def _handoff(self, when=None, **values):
        event_id = str(uuid.uuid4())
        defaults = {
            "reference": "CP-" + uuid.uuid4().hex[:12].upper(),
            "action_id": self.action.id, "website_id": self.website.id,
            "company_id": self.env.company.id, "account_id": self.account.id,
            "clicked_at": when or self.when - datetime.timedelta(seconds=20),
            "page_url": self.origin + "/", "landing_url": self.origin + "/",
            "acquisition_json": {"utm_source": "test"}, "event_id": event_id,
            "session_key": hashlib.sha256(event_id.encode()).hexdigest(),
        }
        defaults.update(values)
        return self.env["marketing.website.whatsapp.handoff"]._service().create(defaults)

    def _message(self, text="Olá", when=None, binding=None, **values):
        binding = binding or self._channel()
        guest = binding.identity_id.mail_guest_id or binding.channel_id.channel_member_ids.guest_id[:1]
        message = binding.channel_id.sudo().with_context(guest=guest)._contact_center_post(
            origin="inbound", body=escape(text), message_type="comment",
            subtype_xmlid="mail.mt_comment", date=when or self.when, partner_ids=[],
        )
        defaults = {
            "message_id": message.id, "channel_binding_id": binding.id,
            "direction": "inbound", "origin": "provider", "content_type": "text",
            "external_message_id": str(uuid.uuid4()), "delivery_state": "delivered",
        }
        defaults.update(values)
        return self.env["contact.center.message.binding"].sudo().with_context(
            marketing_contact_center_skip_lifecycle_enqueue=True,
        ).create(defaults)

    def _results(self, message):
        return self.matches.search([("message_binding_id", "=", message.id)])

    def test_exact_reference_and_repeat_are_idempotent(self):
        handoff = self._handoff()
        before_leads = self.env["crm.lead"].search_count([])
        before_outbox = self.env["contact.center.outbox.command"].search_count([])
        message = self._message("Olá. Referência: " + handoff.reference)
        match = self._results(message)
        self.assertEqual(match.state, "reference")
        self.assertEqual(match.handoff_id, handoff)
        self.assertEqual(match.delta_seconds, 20)
        self.assertEqual(self.service._analyze_inbound(message), match)
        repeated = self._message(handoff.reference, binding=message.channel_binding_id)
        self.assertFalse(self._results(repeated))
        self.assertEqual(self.service._analyze_inbound(repeated), match)
        self.assertEqual(self.env["crm.lead"].search_count([]), before_leads)
        self.assertEqual(self.env["contact.center.outbox.command"].search_count([]), before_outbox)

    def test_removed_reference_is_suggestion_not_claim(self):
        handoff = self._handoff()
        message = self._message("Quero informações")
        match = self._results(message)
        self.assertEqual(match.handoff_id, handoff)
        self.assertEqual(match.state, "suggested")
        self.assertEqual(match.candidate_count, 1)
        self.assertTrue(0 <= match.score <= 100)

    def test_unmatched_click_is_eligible_while_claimed_click_is_excluded(self):
        claimed = self._handoff(self.when - datetime.timedelta(seconds=10))
        self.assertEqual(self._results(self._message(claimed.reference)).state, "reference")
        available = self._handoff(self.when - datetime.timedelta(seconds=40))
        message = self._message()
        self.assertTrue(self.service._is_entry_message(message))
        matches = self._results(message)
        self.assertEqual(matches.handoff_id, available)
        self.assertEqual(matches.state, "suggested")
        self.assertEqual(matches.candidate_count, 1)

    def test_multiple_candidates_remain_visible_and_ranked(self):
        old = self._handoff(self.when - datetime.timedelta(seconds=100))
        recent = self._handoff(self.when - datetime.timedelta(seconds=10))
        matches = self._results(self._message())
        self.assertEqual(set(matches.mapped("handoff_id").ids), {old.id, recent.id})
        self.assertEqual(set(matches.mapped("state")), {"suggested"})
        self.assertEqual(set(matches.mapped("candidate_count")), {2})
        self.assertGreater(
            matches.filtered(lambda item: item.handoff_id == recent).score,
            matches.filtered(lambda item: item.handoff_id == old).score,
        )

    def test_candidate_limit_marks_ambiguity(self):
        for offset in range(7):
            self._handoff(self.when - datetime.timedelta(seconds=offset + 10))
        matches = self._results(self._message())
        self.assertEqual(len(matches), 5)
        self.assertTrue(all(matches.mapped("candidates_truncated")))
        self.assertEqual(set(matches.mapped("candidate_count")), {6})
        self.assertEqual(set(matches.mapped("state")), {"suggested"})

    def test_unknown_malformed_and_multiple_refs_do_not_fall_back(self):
        handoff = self._handoff()
        for text in (
            "Referência: CP-ZZZZZZZZZZZZ",
            "Referência: " + handoff.reference[:-1],
            "Referência: " + handoff.reference.lower(),
            handoff.reference + " " + handoff.reference,
            handoff.reference + " CP-ZZZZZZZZZZZ",
        ):
            self.assertFalse(self._results(self._message(text)))

    def test_cross_account_and_cross_company_reference_rejected(self):
        handoff = self._handoff()
        other_account = self._account(self.env.company)
        message = self._message(handoff.reference, binding=self._channel(other_account))
        self.assertFalse(self._results(message))
        company = self.env["res.company"].create({"name": "Other WhatsApp company"})
        self.agent.write({"company_ids": [(4, company.id)]})
        account = self._account(company)
        message = self._message(handoff.reference, binding=self._channel(account))
        self.assertFalse(self._results(message))

    def test_provider_time_controls_window_not_processing_time(self):
        handoff = self._handoff(self.when - datetime.timedelta(days=1))
        message = self._message("Olá", when=handoff.clicked_at + datetime.timedelta(seconds=30))
        match = self._results(message)
        self.assertEqual(match.handoff_id, handoff)
        self.assertEqual(match.delta_seconds, 30)

    def test_reference_rounding_and_expiry(self):
        future = self._handoff(self.when + datetime.timedelta(seconds=2))
        self.assertEqual(self._results(self._message(future.reference)).state, "reference")
        too_future = self._handoff(self.when + datetime.timedelta(seconds=3))
        self.assertFalse(self._results(self._message(too_future.reference)))
        old = self._handoff(self.when - datetime.timedelta(days=30, seconds=1))
        self.assertFalse(self._results(self._message(old.reference)))

    def test_claimed_reference_cannot_move_to_another_conversation(self):
        handoff = self._handoff()
        original = self._results(self._message(handoff.reference))
        other = self._message(handoff.reference)
        self.assertFalse(self._results(other))
        self.assertEqual(self.matches.search_count([("handoff_id", "=", handoff.id)]), 1)
        self.assertEqual(original.state, "reference")

    def test_only_first_or_return_after_gap_gets_temporal_candidates(self):
        binding = self._channel()
        self._message(when=self.when - datetime.timedelta(minutes=30), binding=binding)
        self._handoff()
        self.assertFalse(self._results(self._message(binding=binding)))
        next_day = self.when + datetime.timedelta(hours=25)
        new = self._handoff(next_day - datetime.timedelta(seconds=30))
        matches = self._results(self._message(when=next_day, binding=binding))
        self.assertEqual(matches.handoff_id, new)
        self.assertEqual(matches.state, "suggested")

    def test_same_second_prior_message_prevents_duplicate_episode(self):
        binding = self._channel()
        self._message(binding=binding)
        self._handoff()
        self.assertFalse(self._results(self._message(binding=binding)))

    def test_forwarded_quoted_outbound_and_group_messages_do_not_match(self):
        handoff = self._handoff()
        forwarded = self._message(handoff.reference, is_forwarded=True)
        self.assertFalse(self._results(forwarded))
        quoted = self._message(handoff.reference, binding=forwarded.channel_binding_id,
                               reply_to_binding_id=forwarded.id)
        self.assertFalse(self._results(quoted))
        own = self._message(handoff.reference, direction="outbound", origin="external_device")
        self.assertFalse(self._results(own))
        group = self._message(handoff.reference, binding=self._channel(conversation_type="group"))
        self.assertFalse(self._results(group))
        self.assertFalse(self.service._eligible(group))

    def test_confirm_reject_and_rerun_preserve_review(self):
        handoff = self._handoff()
        message = self._message()
        match = self._results(message)
        match.with_user(self.agent).action_confirm()
        self.assertEqual(match.state, "confirmed")
        self.assertEqual(match.reviewer_id, self.agent)
        match.with_user(self.agent).action_reject()
        self.assertEqual(match.state, "rejected")
        self.assertEqual(self.service._analyze_inbound(message).state, "rejected")
        self.assertEqual(match.handoff_id, handoff)

    def test_competing_confirmation_refused(self):
        self._handoff()
        first = self._results(self._message())
        second = self._results(self._message())
        first.with_user(self.agent).action_confirm()
        with self.assertRaises(ValidationError):
            second.with_user(self.agent).action_confirm()
        self.assertEqual(second.state, "suggested")

    def test_nonmember_cannot_review_or_forge(self):
        handoff = self._handoff()
        match = self._results(self._message())
        with self.assertRaises(AccessError):
            match.with_user(self.outsider).action_confirm()
        with self.assertRaises(AccessError):
            match.with_user(self.agent).write({"state": "confirmed"})
        with self.assertRaises(AccessError):
            self.matches.create({"handoff_id": handoff.id})

    def test_only_explicit_lead_links_expose_confirmed_sources(self):
        self._handoff()
        message = self._message()
        match = self._results(message)
        lead = self.env["crm.lead"].create({
            "name": "Synthetic WhatsApp lead", "company_id": self.env.company.id,
            "user_id": self.agent.id,
        })
        channel = message.channel_binding_id.channel_id
        self.env["contact.center.crm.conversation.link"].with_user(self.agent)._link(
            channel.with_user(self.agent), lead.with_user(self.agent),
        )
        self.assertFalse(lead.with_user(self.agent).marketing_whatsapp_match_ids)
        match.with_user(self.agent).action_confirm()
        lead.invalidate_recordset(["marketing_whatsapp_match_ids"])
        self.assertEqual(lead.with_user(self.agent).marketing_whatsapp_match_ids, match)
        self.assertFalse(lead.with_user(self.outsider).marketing_whatsapp_match_ids)

    def test_correlation_failure_preserves_message_but_retries_concurrency(self):
        service_type = type(self.service)
        with patch.object(service_type, "_analyze_inbound", side_effect=ValueError("synthetic")):
            message = self._message()
        self.assertTrue(message.exists())
        for error in (OperationalError("synthetic"), MarketingSerializationFailure("synthetic")):
            with self.assertRaises(type(error)), self.env.cr.savepoint():
                with patch.object(service_type, "_analyze_inbound", side_effect=error):
                    self._message()
