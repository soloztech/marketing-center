"""Real two-transaction races between WhatsApp claims and temporal guesses."""

import datetime
import hashlib
import uuid

from markupsafe import escape
from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
)

WHEN = datetime.datetime(2026, 9, 20, 12, 0)


@tagged("-at_install", "post_install")
class TestWebsiteWhatsappSupersedeConcurrency(TransactionCase):
    """A stale snapshot must retry instead of acting on associations it cannot see."""

    def _env(self, cr):
        return api.Environment(
            cr, SUPERUSER_ID, {"allowed_company_ids": [self.env.company.id]}
        )

    def _channel(self, env, fixture):
        guest = env["mail.guest"].create({"name": "WA race %s" % fixture["token"]})
        identity = env["contact.center.identity"].create(
            {
                "name": "WA race %s" % fixture["token"],
                "company_id": fixture["company_id"],
                "mail_guest_id": guest.id,
            }
        )
        account = env["contact.center.account"].browse(fixture["account_id"])
        channel = env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            conversation_type="direct",
            guest_ids=guest.ids,
        )
        return env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )

    def _message(self, env, binding_id, when):
        binding = env["contact.center.channel.binding"].browse(binding_id)
        message = binding.channel_id.with_context(
            guest=binding.identity_id.mail_guest_id
        )._contact_center_post(
            origin="inbound",
            body=escape("Olá"),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            date=when,
            partner_ids=[],
        )
        return (
            env["contact.center.message.binding"]
            .with_context(
                marketing_contact_center_skip_lifecycle_enqueue=True,
            )
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": str(uuid.uuid4()),
                    "delivery_state": "delivered",
                }
            )
        )

    def _fixture(self):
        token = uuid.uuid4().hex[:10]
        origin = "https://wa-race-%s.example" % token
        fixture = {"token": token, "company_id": self.env.company.id}
        with self.registry.cursor() as cr:
            env = self._env(cr)
            website = env["website"].create(
                {
                    "name": "WA race %s" % token,
                    "domain": origin,
                    "company_id": fixture["company_id"],
                }
            )
            endpoint = env["marketing.web.ingress.endpoint"].create(
                {
                    "name": "WA race %s" % token,
                    "company_id": fixture["company_id"],
                    "allowed_origins": origin,
                    "allowed_hosts": origin[8:],
                    "capture_enabled": True,
                    "capture_purpose": "website_attribution",
                    "website_tracking_policy": "informational_notice",
                    "privacy_policy_version": "race-v1",
                    "privacy_notice_version": "race-v1",
                    "privacy_policy_justification": "Synthetic test",
                    "identifier_retention_days": 30,
                }
            )
            ingress = env["marketing.website.ingress.binding"].create(
                {
                    "website_id": website.id,
                    "endpoint_id": endpoint.id,
                }
            )
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "WA race agent %s" % token,
                        "login": "wa-race-agent-%s" % token,
                        "company_id": fixture["company_id"],
                        "company_ids": [(6, 0, [fixture["company_id"]])],
                        "groups_id": [
                            (
                                6,
                                0,
                                (
                                    env.ref(
                                        "contact_center_base.group_contact_center_agent"
                                    )
                                    | env.ref("sales_team.group_sale_salesman")
                                ).ids,
                            )
                        ],
                    }
                )
            )
            fixture["agent_id"] = agent.id
            team = env["contact.center.team"].create(
                {
                    "name": "WA race %s" % token,
                    "company_id": fixture["company_id"],
                    "agent_ids": [(6, 0, agent.ids)],
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "WA race %s" % token,
                    "company_id": fixture["company_id"],
                    "platform": "whatsapp",
                    "own_external_identity": "5511999999999@s.whatsapp.net",
                    "access_team_ids": [(6, 0, team.ids)],
                }
            )
            action = env["marketing.website.action"].create(
                {
                    "name": "WA race %s" % token,
                    "binding_id": ingress.id,
                    "kind": "whatsapp_handoff",
                    "route_ref": "whatsapp.race.%s" % token,
                    "source_path": "/",
                    "whatsapp_destination": "5511999999999",
                    "handoff_enabled": True,
                    "handoff_account_id": account.id,
                    "handoff_reference_prefix": "CP",
                }
            )
            fixture.update(
                website_id=website.id,
                endpoint_id=endpoint.id,
                ingress_id=ingress.id,
                team_id=team.id,
                account_id=account.id,
                action_id=action.id,
            )
            event_id = str(uuid.uuid4())
            handoff = (
                env["marketing.website.whatsapp.handoff"]
                ._service()
                .create(
                    {
                        "reference": "CP-" + uuid.uuid4().hex[:12].upper(),
                        "action_id": action.id,
                        "website_id": website.id,
                        "company_id": fixture["company_id"],
                        "account_id": account.id,
                        "clicked_at": WHEN - datetime.timedelta(seconds=20),
                        "page_url": origin + "/",
                        "landing_url": origin + "/",
                        "acquisition_json": {"utm_source": "test"},
                        "event_id": event_id,
                        "session_key": hashlib.sha256(event_id.encode()).hexdigest(),
                    }
                )
            )
            first = self._channel(env, fixture)
            second = self._channel(env, fixture)
            first_message = self._message(env, first.id, WHEN)
            match = env["marketing.website.whatsapp.match"].search(
                [("message_binding_id", "=", first_message.id)]
            )
            fixture.update(
                handoff_id=handoff.id,
                first_binding_id=first.id,
                second_binding_id=second.id,
                first_match_id=match.id,
            )
            cr.commit()  # pylint: disable=invalid-commit
        return fixture

    def _cleanup(self, fixture):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            account_id = fixture.get("account_id")
            if account_id:
                cr.execute(
                    "DELETE FROM marketing_website_whatsapp_match WHERE account_id = %s",
                    [account_id],
                )
                cr.execute(
                    "DELETE FROM marketing_website_whatsapp_handoff WHERE account_id = %s",
                    [account_id],
                )
                channel_bindings = (
                    env["contact.center.channel.binding"]
                    .with_context(active_test=False)
                    .search([("account_id", "=", account_id)])
                )
                channels = channel_bindings.channel_id
                message_bindings = env["contact.center.message.binding"].search(
                    [("account_id", "=", account_id)]
                )
                models = {
                    "contact.center.channel.binding": set(channel_bindings.ids),
                    "contact.center.message.binding": set(message_bindings.ids),
                }
                env["queue.job"].search([("model_name", "in", list(models))]).filtered(
                    lambda job: bool(set(job.record_ids or []) & models[job.model_name])
                ).unlink()
                self._cleanup_bridge_evidence(env, channel_bindings)
                message_bindings.unlink()
                env["mail.message"].search(
                    [("model", "=", "mail.channel"), ("res_id", "in", channels.ids)]
                ).with_context(
                    contact_center_post_token=CONTACT_CENTER_POST_TOKEN
                ).unlink()
                identities = channel_bindings.identity_id
                guests = identities.mail_guest_id
                channel_bindings.unlink()
                channels.with_context(
                    contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN,
                    contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
                ).unlink()
                env["contact.center.identity.alias"].search(
                    [("account_id", "=", account_id)]
                ).unlink()
                identities.unlink()
                guests.with_context(
                    contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
                ).unlink()
            if fixture.get("action_id"):
                cr.execute(
                    "DELETE FROM marketing_website_redirect_grant WHERE action_id = %s",
                    [fixture["action_id"]],
                )
                cr.execute(
                    "DELETE FROM marketing_website_action WHERE id = %s",
                    [fixture["action_id"]],
                )
            if account_id:
                env["contact.center.account"].browse(account_id).unlink()
                env["contact.center.team"].browse(fixture["team_id"]).unlink()
            if fixture.get("agent_id"):
                agent = env["res.users"].browse(fixture["agent_id"])
                partner = agent.partner_id
                agent.unlink()
                partner.exists().unlink()
            if fixture.get("ingress_id"):
                cr.execute(
                    "DELETE FROM marketing_website_ingress_binding WHERE id = %s",
                    [fixture["ingress_id"]],
                )
                env["website"].browse(fixture["website_id"]).unlink()
                cr.execute(
                    "DELETE FROM marketing_web_ingress_endpoint WHERE id = %s",
                    [fixture["endpoint_id"]],
                )
            cr.commit()  # pylint: disable=invalid-commit

    def _cleanup_bridge_evidence(self, env, channel_bindings):
        """Remove optional Marketing bridge rows owned by this committed fixture.

        Those ledgers forbid runtime deletion; these scoped test-only statements
        clean child rows before the Contact Center parents.
        """
        if "marketing.contact.center.response.signal" not in env:
            return
        ids = [channel_bindings.ids]
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_cursor "
            "WHERE channel_binding_id = ANY(%s)",
            ids,
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response WHERE episode_id IN "
            "(SELECT id FROM marketing_contact_center_response_episode "
            "WHERE channel_binding_id = ANY(%s))",
            ids,
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_episode "
            "WHERE channel_binding_id = ANY(%s)",
            ids,
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_signal "
            "WHERE channel_binding_id = ANY(%s)",
            ids,
        )
        events = env["marketing.business.event"].search(
            [
                ("source_system", "=", "contact_center"),
                ("source_model", "=", "mail.channel"),
                ("source_res_id", "in", channel_bindings.channel_id.ids),
            ]
        )
        if events:
            if "marketing.business.event.crm.link" in env:
                env.cr.execute(
                    "DELETE FROM marketing_business_event_crm_link "
                    "WHERE event_id = ANY(%s)",
                    [events.ids],
                )
            env.cr.execute(
                "DELETE FROM marketing_business_event_observation "
                "WHERE event_id = ANY(%s)",
                [events.ids],
            )
            env.cr.execute(
                "DELETE FROM marketing_business_event WHERE id = ANY(%s)",
                [events.ids],
            )

    def _open_snapshot(self, fixture):
        cr = self.registry.cursor()
        # REPEATABLE READ fixes the snapshot at the first statement.
        cr.execute(
            "SELECT state FROM marketing_website_whatsapp_match WHERE id = %s",
            [fixture["first_match_id"]],
        )
        return cr

    def _confirm(self, env, fixture):
        # The real review path, as the conversation's authorized agent.
        env["marketing.website.whatsapp.match"].with_user(fixture["agent_id"]).browse(
            fixture["first_match_id"]
        ).action_confirm()

    def _states(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT message.channel_binding_id, match.state "
                "FROM marketing_website_whatsapp_match AS match "
                "JOIN contact_center_message_binding AS message "
                "ON message.id = match.message_binding_id "
                "WHERE match.handoff_id = %s",
                [fixture["handoff_id"]],
            )
            return dict(cr.fetchall())

    def test_stale_analysis_after_committed_confirmation_retries(self):
        fixture = {}
        try:
            fixture = self._fixture()
            stale = self._open_snapshot(fixture)
            try:
                with self.registry.cursor() as cr:
                    self._confirm(self._env(cr), fixture)
                    cr.commit()  # pylint: disable=invalid-commit
                with self.assertRaises(pg_errors.SerializationFailure):
                    self._message(
                        self._env(stale),
                        fixture["second_binding_id"],
                        WHEN + datetime.timedelta(seconds=5),
                    )
                stale.rollback()
            finally:
                stale.close()
            with self.registry.cursor() as cr:
                self._message(
                    self._env(cr),
                    fixture["second_binding_id"],
                    WHEN + datetime.timedelta(seconds=5),
                )
                cr.commit()  # pylint: disable=invalid-commit
            self.assertEqual(
                self._states(fixture), {fixture["first_binding_id"]: "confirmed"}
            )
        finally:
            self._cleanup(fixture)

    def test_stale_confirmation_after_committed_guess_retries(self):
        fixture = {}
        try:
            fixture = self._fixture()
            stale = self._open_snapshot(fixture)
            try:
                with self.registry.cursor() as cr:
                    self._message(
                        self._env(cr),
                        fixture["second_binding_id"],
                        WHEN + datetime.timedelta(seconds=5),
                    )
                    cr.commit()  # pylint: disable=invalid-commit
                self.assertEqual(
                    self._states(fixture),
                    {
                        fixture["first_binding_id"]: "suggested",
                        fixture["second_binding_id"]: "suggested",
                    },
                )
                with self.assertRaises(pg_errors.SerializationFailure):
                    self._confirm(self._env(stale), fixture)
                stale.rollback()
            finally:
                stale.close()
            with self.registry.cursor() as cr:
                self._confirm(self._env(cr), fixture)
                cr.commit()  # pylint: disable=invalid-commit
            self.assertEqual(
                self._states(fixture),
                {
                    fixture["first_binding_id"]: "confirmed",
                    fixture["second_binding_id"]: "superseded",
                },
            )
        finally:
            self._cleanup(fixture)
