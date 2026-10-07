"""The acquisition clock and business authority are independent evidence."""

import datetime
import json
import uuid
from unittest.mock import patch

from lxml import etree
from psycopg2 import OperationalError

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_website.models.consent import CONSENT_CONTEXT_TOKEN
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from .test_correlation import TestWebsiteWhatsappCorrelation


class TestWebsiteWhatsappJourney(TestWebsiteWhatsappCorrelation):
    def _google_catalog(self):
        if "marketing.center.google.profile" not in self.env:
            self.skipTest("Optional Google catalog is not installed")
        from odoo.addons.marketing_center_base.services.catalog_dto import (
            ExternalEntityDTO,
        )
        from odoo.addons.marketing_center_google.tests.common import (
            create_google_profile,
            project_google_source,
        )

        _identity, profile = create_google_profile(
            self.env, name="Website journey synthetic"
        )
        _customer, source, _connection = project_google_source(self.env, profile)
        source.write(
            {
                "native_utm_mode": "apply",
                "native_utm_source_id": self.env["utm.source"]
                .create({"name": "Synthetic Google"})
                .id,
                "native_utm_medium_id": self.env["utm.medium"]
                .create({"name": "Synthetic paid"})
                .id,
            }
        )
        result = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref="customers/1234567890/campaigns/23172129564",
                external_id="23172129564",
                name="Synthetic website campaign",
                observed_at=fields.Datetime.now(),
            ),
        )
        return source, self.env["marketing.center.external.entity"].browse(
            result.entity_id
        )

    def test_journey_catalog_resolution_classifies_crm_without_google_io(self):
        source, entity = self._google_catalog()
        handoff, match = self._claim(
            acquisition_json={"gad_campaignid": "23172129564", "gclid": "synthetic"}
        )
        lead, row = self._business(match)
        self._run(match, row)
        lead._job_resolve_native_utm()
        self.assertEqual(lead.campaign_id, entity.native_utm_campaign_id)
        self.assertTrue(lead.campaign_id)
        item = lead._journey_origins(match.channel_id, row)["items"][-1]
        self.assertEqual(item["campaign"]["name"], entity.name)
        self.assertEqual(item["campaign"]["evidence"], "url_and_local_catalog")
        # Native classification records its normal applied -> present replay
        # once. Measure policy changes only after that existing stabilization.
        lead._job_resolve_native_utm()
        before = self.env["marketing.crm.utm.application"].search_count(
            [("lead_id", "=", lead.id)]
        )
        endpoint = self.action.binding_id.endpoint_id
        endpoint.capture_enabled = False
        lead._job_resolve_native_utm()
        endpoint.capture_enabled = True
        lead._job_resolve_native_utm()
        self.assertEqual(
            self.env["marketing.crm.utm.application"].search_count(
                [("lead_id", "=", lead.id)]
            ),
            before,
        )
        other = source.copy(
            {
                "external_account_ref": "customers/9876543210",
                "external_account_id": "9876543210",
            }
        )
        other.write({"state": "active", "read_enabled": True, "active": True})
        self.assertEqual(
            self.env["marketing.website.whatsapp.journey.api"]._campaign(
                handoff.acquisition_json, self.env.company
            )["status"],
            "ambiguous",
        )
        self.assertTrue(other)

    def test_journey_hashed_gclid_without_campaign_never_schedules_lookup(self):
        self._google_catalog()
        handoff, match = self._claim(
            acquisition_json={"gclid": "synthetic-without-campaign"}
        )
        _lead, row = self._business(match)
        self._run(match, row)
        before = self.env["marketing.center.sync.run"].search_count([])
        with trap_jobs() as jobs:
            result = self.env[
                "marketing.center.google.click.service"
            ]._schedule_touchpoint(handoff.journey_touchpoint_id, explicit=True)
            self.assertFalse(result)
            jobs.assert_jobs_count(0)
        self.assertEqual(self.env["marketing.center.sync.run"].search_count([]), before)
        key = handoff.journey_touchpoint_id.canonical_key
        with trap_jobs() as jobs:
            result = self.env[
                "marketing.center.google.click.service"
            ]._cron_enqueue_linked_clicks()
            self.assertEqual(result["scheduled"], 0)
            jobs.assert_jobs_count(0)
        self.assertFalse(
            self.env["marketing.center.google.click.lookup"].search(
                [("canonical_key", "=", key)]
            )
        )
        self.assertEqual(self.env["marketing.center.sync.run"].search_count([]), before)

    def test_journey_native_retention_keeps_return_contract_and_manual_policy(self):
        expired = self._handoff()
        endpoint = self.action.binding_id.endpoint_id
        endpoint.retention_mode = "manual"
        manual = self._handoff()
        future = fields.Datetime.now() + datetime.timedelta(days=45)
        with patch.object(fields.Datetime, "now", return_value=future):
            result = self.env[
                "marketing.web.ingress.event"
            ]._cron_expire_retained_values()
        self.assertEqual(result, 0)
        self.assertTrue(expired.erased_at)
        self.assertFalse(manual.erased_at)
        self.assertFalse(manual.retain_until)

    def test_journey_native_visitor_merge_moves_handoff_and_track_only_same_site(self):
        source, target = self.env["website.visitor"].create(
            [
                {"website_id": self.website.id, "access_token": uuid.uuid4().hex},
                {
                    "website_id": self.website.id,
                    "access_token": str(self.agent.partner_id.id),
                },
            ]
        )
        track = self.env["website.track"].create(
            {
                "visitor_id": source.id,
                "url": self.origin + "/",
                "visit_datetime": self.when,
            }
        )
        handoff = self._handoff(visitor_id=source.id, track_id=track.id)
        source._merge_visitor(target)
        self.assertFalse(source.exists())
        self.assertEqual(track.visitor_id, target)
        self.assertEqual(handoff.visitor_id, target)
        self.assertEqual(handoff.track_id, track)

    def test_journey_cross_website_merge_detaches_both_references(self):
        foreign = self.website.copy(
            {"name": "Synthetic other Website", "domain": "https://other.invalid"}
        )
        source, target = self.env["website.visitor"].create(
            [
                {"website_id": self.website.id, "access_token": uuid.uuid4().hex},
                {
                    "website_id": foreign.id,
                    "access_token": str(self.agent.partner_id.id),
                },
            ]
        )
        track = self.env["website.track"].create(
            {"visitor_id": source.id, "url": self.origin + "/"}
        )
        handoff = self._handoff(visitor_id=source.id, track_id=track.id)
        snapshot = handoff.acquisition_json
        source._merge_visitor(target)
        self.assertFalse(source.exists())
        self.assertEqual(track.visitor_id, target)
        self.assertFalse(handoff.visitor_id or handoff.track_id)
        self.assertEqual(handoff.acquisition_json, snapshot)
        handoff._check_scope()

    def test_journey_more_than_100_links_hold_pending_until_last_page(self):
        _handoff, match = self._claim()
        leads = self.env["crm.lead"].create(
            [
                {
                    "name": "Synthetic paged %s" % index,
                    "company_id": self.env.company.id,
                }
                for index in range(101)
            ]
        )
        rows = (
            self.env["contact.center.crm.conversation.link"]
            ._service()
            .create(
                [
                    {
                        "channel_id": match.channel_id.id,
                        "lead_id": lead.id,
                        "lead_record_id_snapshot": lead.id,
                        "scope_state": "context",
                        "writer": "automation",
                    }
                    for lead in leads
                ]
            )
        )
        self.env.company._job_whatsapp_match(match.id, match.journey_revision)
        self.assertTrue(match.journey_pending)
        self.env.company._job_whatsapp_match(
            match.id, match.journey_revision, rows[99].id
        )
        self.assertFalse(match.journey_pending)
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search_count(
                [
                    ("authority_key", "=", "website.whatsapp"),
                    ("authority_ref", "=", str(match.id)),
                ]
            ),
            101,
        )

    def test_journey_native_partner_merge_and_removed_visitor_preserve_snapshot(self):
        source, target = self.env["res.partner"].create(
            [{"name": "Synthetic source"}, {"name": "Synthetic survivor"}]
        )
        visitors = self.env["website.visitor"].create(
            [
                {"website_id": self.website.id, "access_token": str(partner.id)}
                for partner in (source, target)
            ]
        )
        track = self.env["website.track"].create(
            {
                "visitor_id": visitors[0].id,
                "url": self.origin + "/?utm_campaign=Frozen",
            }
        )
        handoff, match = self._claim(
            visitor_id=visitors[0].id,
            track_id=track.id,
            acquisition_json={"utm_campaign": "Frozen"},
        )
        self._run(match)
        point = handoff.journey_touchpoint_id
        self.env["base.partner.merge.automatic.wizard"]._merge(
            (source | target).ids, dst_partner=target
        )
        self.assertEqual(handoff.visitor_id, visitors[1])
        self.assertEqual(handoff.track_id.visitor_id, visitors[1])
        visitors[1].unlink()
        self.assertFalse(handoff.visitor_id or handoff.track_id)
        self._run(match)
        self.assertEqual(handoff.journey_touchpoint_id, point)
        self.assertEqual(handoff.acquisition_json["utm_campaign"], "Frozen")

    def test_journey_conversion_transfer_review_and_reconfirmation(self):
        _handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        partner = self.env["res.partner"].create({"name": "Synthetic conversion"})
        lead.type = "lead"
        lead.with_user(self.agent).convert_opportunity(partner)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        target = lead.copy({"name": "Synthetic transfer", "campaign_id": False})
        (lead | target)._contact_center_lock_conversation_graph(
            channel_ids=match.channel_id.ids, touch_leads=True, touch_channels=True
        )
        row._transfer_to_lead(target)
        active = target._conversation_links()
        self.assertEqual(active.scope_state, "review")
        self._run(match, row)
        self._run(match, active)
        self.assertFalse(self._effective(lead))
        self.assertTrue(self._effective(target)._scope_partition()["pending"])
        confirmed = active.with_user(self.agent)._confirm_scope(self.when)
        self._run(match, confirmed)
        self.assertTrue(self._effective(target)._scope_partition()["eligible"])

    def test_journey_retention_isolates_optional_item_failure(self):
        first, second = self._handoff(), self._handoff()
        original = type(first)._erase_journey_values

        def erase(record, *args, **kwargs):
            if record.id == first.id:
                raise RuntimeError("Synthetic one-item retention failure")
            return original(record, *args, **kwargs)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + datetime.timedelta(days=45),
        ), patch.object(type(first), "_erase_journey_values", new=erase):
            self.env["marketing.web.ingress.event"]._cron_expire_retained_values()
        self.assertFalse(first.erased_at)
        self.assertTrue(second.erased_at)

    def _move_website_company(self):
        self.website.handoff_default_action_id = False
        self.action.write({"handoff_enabled": False, "handoff_account_id": False})
        self.action.binding_id.active = False
        company = self.env["res.company"].create({"name": "Synthetic Website move"})
        self.website.company_id = company
        return company

    def test_journey_company_move_does_not_block_retention(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        point = handoff.journey_touchpoint_id
        company = handoff.company_id
        self._move_website_company()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + datetime.timedelta(days=45),
        ):
            self.env["marketing.web.ingress.event"]._cron_expire_retained_values()
        self.assertTrue(handoff.erased_at)
        self.assertFalse(handoff.acquisition_json)
        self.assertTrue(point.privacy_erased_at)
        company._job_whatsapp_erasure(handoff.id)
        self.assertFalse(self._effective(lead)._scope_partition()["eligible"])
        self._run(match, row)
        self.assertFalse(self._effective(lead))

    def test_journey_company_move_does_not_block_refusal(self):
        handoff, match, decision = self._individual_capture()
        self._run(match)
        company = handoff.company_id
        self._move_website_company()
        decision.with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN).write(
            {"revoked_at": fields.Datetime.now()}
        )
        decision._explicit_refusal()
        company._job_whatsapp_withdrawal(decision.id)
        self.assertTrue(handoff.erased_at)
        self.assertFalse(handoff.capture_consent_id or handoff.acquisition_json)
        self.assertTrue(handoff.journey_touchpoint_id.privacy_erased_at)

    def test_journey_company_move_and_optional_detach_failure_allow_native_unlink(self):
        visitor = self.env["website.visitor"].create(
            {"website_id": self.website.id, "access_token": uuid.uuid4().hex}
        )
        track = self.env["website.track"].create(
            {"visitor_id": visitor.id, "url": self.origin + "/"}
        )
        handoff = self._handoff(visitor_id=visitor.id, track_id=track.id)
        snapshot = handoff.acquisition_json
        self._move_website_company()
        original = type(handoff).write

        def write(records, values):
            if values == {"visitor_id": False, "track_id": False}:
                raise RuntimeError("Synthetic optional detach failure")
            return original(records, values)

        with patch.object(type(handoff), "write", new=write):
            visitor.unlink()
        self.assertFalse(visitor.exists() or track.exists())
        self.assertFalse(handoff.visitor_id or handoff.track_id)
        self.assertEqual(handoff.acquisition_json, snapshot)

    def test_journey_visitor_detach_reraises_concurrency_errors(self):
        handoff = self._handoff()
        with patch.object(
            type(handoff), "write", side_effect=OperationalError("Synthetic retry")
        ), self.assertRaises(OperationalError):
            handoff._detach_visitor_references()

    def test_journey_retention_failure_batch_cannot_starve_later_capture(self):
        # Independent committed QA fixtures may predate this test. Clean their
        # expired captures before creating the batch whose ordering we assert.
        with patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + datetime.timedelta(days=45),
        ):
            self.env["marketing.web.ingress.event"]._cron_expire_retained_values()
        failed = self.env["marketing.website.whatsapp.handoff"]
        for _index in range(101):
            failed |= self._handoff()
        valid = self._handoff()
        original = type(valid)._erase_journey_values

        def erase(record, *args, **kwargs):
            if record in failed:
                raise RuntimeError("Synthetic persistent retention failure")
            return original(record, *args, **kwargs)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + datetime.timedelta(days=45),
        ), patch.object(type(valid), "_erase_journey_values", new=erase):
            cron = self.env["marketing.web.ingress.event"]
            self.assertEqual(cron._cron_expire_retained_values(), 0)
            self.assertFalse(valid.erased_at)
            self.assertEqual(len(failed.filtered("retention_failures")), 100)
            self.assertEqual(cron._cron_expire_retained_values(), 0)
        self.assertTrue(valid.erased_at)
        self.assertFalse(any(failed.mapped("erased_at")))
        self.assertTrue(all(failed.mapped("retention_failures")))

    def _claim(self, **values):
        handoff = self._handoff(**values)
        message = self._message(handoff.reference)
        return handoff, self._results(message)

    def _business(self, match, start=None, end=False, scope="confirmed"):
        lead = self.env["crm.lead"].create(
            {
                "name": "Synthetic journey business",
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
            }
        )
        row = (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.agent)
            ._link(
                match.channel_id.with_user(self.agent),
                lead.with_user(self.agent),
                writer="manual",
            )
        )
        if scope == "confirmed":
            row = row.with_user(self.agent)._confirm_scope(start or self.when, end)
        return lead, row

    def _run(self, match, row=None):
        self.env.company._job_whatsapp_match(match.id, match.journey_revision)
        if row:
            self.env.company._job_marketing_contact_center_crm_conversation_link(row.id)
            self.env.company._job_whatsapp_link(row.id, row.website_journey_revision)

    def _effective(self, lead):
        return self.env["marketing.attribution.crm.effective.link"].search(
            [("lead_id", "=", lead.id)]
        )

    def test_journey_claim_creates_point_without_business_and_never_calls_google(self):
        before = self.env["crm.lead"].search_count([])
        handoff, match = self._claim(
            acquisition_json={
                "gclid": "synthetic-click",
                "gad_campaignid": "23172129564",
            }
        )
        with trap_jobs() as jobs:
            self._run(match)
        point = handoff.journey_touchpoint_id
        self.assertTrue(point)
        self.assertEqual(point.source_system, "website.whatsapp")
        self.assertEqual(point.touchpoint_type, "entry_point")
        self.assertEqual(point.identifier_ids.role, "click")
        self.assertFalse(point.identifier_ids.value_ref)
        self.assertEqual(self.env["crm.lead"].search_count([]), before)
        if "marketing.center.google.click.lookup" in self.env:
            self.assertFalse(
                self.env["marketing.center.google.click.lookup"].search(
                    [("touchpoint_id", "=", point.id)]
                )
            )
        self.assertFalse(any("google" in str(job.func) for job in jobs.enqueued_jobs))
        evidence = point.evidence_ids
        self._run(match)
        self.assertEqual(point.evidence_ids, evidence)

    def test_journey_message_clock_gives_credit_without_retiming_acquisition(self):
        acquired = self.when - datetime.timedelta(days=1)
        handoff, match = self._claim(
            acquisition_json={
                "acquisition_at": acquired.isoformat(),
                "acquisition_provenance": "track",
            }
        )
        lead, row = self._business(match)
        self._run(match, row)
        self.assertEqual(handoff.journey_touchpoint_id.occurred_at, acquired)
        self.assertEqual(handoff.journey_touchpoint_id.observed_at, handoff.clicked_at)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertFalse(match.journey_pending)

    def test_journey_message_edit_and_deletion_preserve_frozen_claim(self):
        handoff, match = self._claim(acquisition_json={"utm_campaign": "Captured"})
        lead, row = self._business(match)
        self._run(match, row)
        lead._job_resolve_native_utm()
        lead._job_resolve_native_utm()
        point = handoff.journey_touchpoint_id
        receipts = self.env["marketing.crm.utm.application"].search_count(
            [("lead_id", "=", lead.id)]
        )
        for state in ("edited", "deleted"):
            match.message_binding_id.sudo().write({"message_state": state})
            self.assertTrue(match._journey_claim_valid())
            self._run(match, row)
            lead._job_resolve_native_utm()
            self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
            self.assertEqual(handoff.journey_touchpoint_id, point)
            self.assertEqual(len(point.evidence_ids), 1)
            self.assertEqual(
                self.env["marketing.crm.utm.application"].search_count(
                    [("lead_id", "=", lead.id)]
                ),
                receipts,
            )
        match.with_user(self.agent).action_reject()
        self._run(match, row)
        self.assertFalse(self._effective(lead))

    def test_journey_global_lead_does_not_anchor_and_foreign_pair_does_not_stall_page(
        self,
    ):
        _handoff, match = self._claim()
        global_lead = self.env["crm.lead"].create(
            {"name": "Global context", "company_id": False}
        )
        row = self.env["contact.center.crm.conversation.link"]._link(
            match.channel_id, global_lead, writer="manual"
        )
        before = global_lead.marketing_event_company_id
        local, local_row = self._business(match)
        self._run(match, row)
        self._run(match, local_row)
        self.assertEqual(global_lead.marketing_event_company_id, before)
        self.assertFalse(self._effective(global_lead))
        self.assertFalse(match.journey_pending or row.website_journey_pending)
        self.assertTrue(self._effective(local)._scope_partition()["eligible"])
        foreign = self.env["res.company"].create({"name": "Synthetic foreign anchor"})
        foreign_env = self.env(
            context=dict(self.env.context, allowed_company_ids=foreign.ids)
        )
        foreign_lead = foreign_env["crm.lead"].create(
            {"name": "Foreign global context", "company_id": False}
        )
        foreign_env["marketing.crm.service"]._company_for_lead(foreign_lead)
        foreign_row = self.env["contact.center.crm.conversation.link"]._link(
            match.channel_id, foreign_lead.with_env(self.env), writer="manual"
        )
        self.env.company._job_whatsapp_match(match.id, match.journey_revision)
        self.env.company._job_whatsapp_link(
            foreign_row.id, foreign_row.website_journey_revision
        )
        self.assertFalse(match.journey_pending or foreign_row.website_journey_pending)
        self.assertFalse(self._effective(foreign_lead.with_env(self.env)))
        with trap_jobs() as jobs:
            row.with_context(allowed_company_ids=foreign.ids)._enqueue_website_journey()
            jobs.assert_enqueued_job(
                self.env.company._job_whatsapp_link,
                args=(row.id, row.website_journey_revision),
            )
            queued = [
                job
                for job in jobs.enqueued_jobs
                if job.func.__name__ == "_job_whatsapp_link"
            ][0]
            self.assertEqual(
                queued.func.__self__.env.context["allowed_company_ids"],
                self.env.company.ids,
            )

    def test_journey_global_context_two_companies_and_native_lead_unlink(self):
        company = self.env["res.company"].create({"name": "Synthetic second company"})
        self.agent.company_ids |= company
        foreign_account = self._account(company)
        foreign_site = self.env["website"].create(
            {
                "name": "Synthetic second Website",
                "domain": "https://second.invalid",
                "company_id": company.id,
            }
        )
        endpoint = self.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Synthetic second endpoint",
                "company_id": company.id,
                "allowed_origins": "https://second.invalid",
                "allowed_hosts": "second.invalid",
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "website_tracking_policy": "informational_notice",
                "privacy_policy_version": "second-v1",
                "privacy_notice_version": "second-v1",
                "privacy_policy_justification": "Synthetic test",
                "identifier_retention_days": 30,
            }
        )
        binding = self.env["marketing.website.ingress.binding"].create(
            {"website_id": foreign_site.id, "endpoint_id": endpoint.id}
        )
        action = self.action.copy(
            {
                "binding_id": binding.id,
                "handoff_account_id": foreign_account.id,
                "route_ref": "second." + uuid.uuid4().hex,
            }
        )
        first_handoff, first = self._claim()
        second_handoff = self._handoff(
            action_id=action.id,
            website_id=foreign_site.id,
            company_id=company.id,
            account_id=foreign_account.id,
            page_url="https://second.invalid/",
            landing_url="https://second.invalid/",
        )
        second = self._results(
            self._message(
                second_handoff.reference,
                binding=self._channel(account=foreign_account),
            )
        )
        self.assertEqual(second.state, "reference")
        global_lead = self.env["crm.lead"].create(
            {"name": "Synthetic global business", "company_id": False}
        )
        rows = self.env["contact.center.crm.conversation.link"]
        for match in (first, second):
            rows |= rows._link(match.channel_id, global_lead, writer="manual")
        # Existing Base/P1 hooks can already pin a Marketing company. P2 must
        # neither choose another one nor let that foreign pair stall the page.
        previous_company = global_lead.marketing_event_company_id
        for match, current_company in ((first, self.env.company), (second, company)):
            scoped = current_company.with_context(
                allowed_company_ids=current_company.ids
            ).with_company(current_company)
            scoped._job_whatsapp_match(match.id, match.journey_revision)
            row = rows.filtered(lambda value: value.channel_id == match.channel_id)
            scoped._job_whatsapp_link(row.id, row.website_journey_revision)
            self.assertFalse(match.journey_pending or row.website_journey_pending)
        self.assertTrue(first_handoff.journey_touchpoint_id)
        self.assertTrue(second_handoff.journey_touchpoint_id)
        self.assertEqual(global_lead.marketing_event_company_id, previous_company)
        self.assertFalse(self._effective(global_lead))
        local, local_row = self._business(first)
        self._run(first, local_row)
        self.assertTrue(self._effective(local)._scope_partition()["eligible"])
        local.unlink()
        self.assertFalse(local.exists())
        self.assertEqual(local_row.state, "unlinked")
        self.assertEqual(local_row.unlinked_reason, "lead_deleted")
        self.env.company._job_whatsapp_match(first.id, first.journey_revision)
        self.env.company._job_whatsapp_link(
            local_row.id, local_row.website_journey_revision
        )
        self.assertFalse(first.journey_pending or local_row.website_journey_pending)
        self.assertFalse(self._effective(global_lead))
        with trap_jobs() as jobs:
            global_lead.with_context(allowed_company_ids=self.env.company.ids).unlink()
        for job in jobs.enqueued_jobs:
            if job.func.__name__ == "_job_whatsapp_link":
                self.assertEqual(
                    job.func.__self__.env.context["allowed_company_ids"],
                    job.func.__self__.ids,
                )
        for row in rows:
            self.assertEqual(row.state, "unlinked")
            self.assertEqual(row.unlinked_reason, "lead_deleted")
            scoped = row.company_id.with_context(
                allowed_company_ids=row.company_id.ids
            ).with_company(row.company_id)
            scoped._job_whatsapp_link(row.id, row.website_journey_revision)
            self.assertFalse(row.website_journey_pending)

    def test_journey_link_first_and_match_first_converge_to_one_point(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        self.env.company._job_whatsapp_link(row.id, row.website_journey_revision)
        self.assertFalse(handoff.journey_touchpoint_id)
        self._run(match, row)
        point = handoff.journey_touchpoint_id
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        other, other_match = self._claim()
        self._run(other_match)
        other_lead, other_row = self._business(other_match)
        self.env.company._job_whatsapp_link(
            other_row.id, other_row.website_journey_revision
        )
        self.assertTrue(self._effective(other_lead)._scope_partition()["eligible"])
        self.assertEqual(
            self.env["marketing.attribution.touchpoint"].search_count(
                [("canonical_key", "=", point.canonical_key)]
            ),
            1,
        )
        self.assertTrue(other.journey_touchpoint_id)

    def test_journey_suggestion_never_projects_and_legacy_stays_readonly(self):
        handoff = self._handoff()
        match = self._results(self._message())
        self._run(match)
        self.assertFalse(handoff.journey_touchpoint_id)
        legacy, legacy_match = self._claim(capture_version=0)
        self._run(legacy_match)
        self.assertFalse(legacy.journey_touchpoint_id)
        self.assertFalse(legacy.capture_privacy_json)

    def test_journey_period_revise_and_reject_preserve_other_authority(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        service = self.env["marketing.crm.service"]
        independent = service._link_touchpoint_lead(
            handoff.journey_touchpoint_id,
            lead,
            authority_key="website.form",
            authority_ref="synthetic-independent",
        )
        revised = row.with_user(self.agent)._confirm_scope(
            self.when + datetime.timedelta(seconds=1)
        )
        self._run(match, row)
        self._run(match, revised)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        own = self.env["marketing.attribution.crm.link"].search(
            [("lead_id", "=", lead.id), ("authority_key", "=", "website.whatsapp")]
        )
        self.assertTrue(own.filtered("revocation_ids"))
        self.assertEqual(
            set(own.revocation_ids.mapped("reason")),
            {"website_whatsapp_authority_removed"},
        )
        match.with_user(self.agent).action_reject()
        self._run(match, revised)
        self.assertTrue(all(own.mapped("revocation_ids")))
        self.assertFalse(independent.revocation_ids)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])

    def test_journey_outside_period_and_context_are_visible_without_credit(self):
        _handoff, match = self._claim()
        lead, row = self._business(match, self.when + datetime.timedelta(seconds=1))
        self._run(match, row)
        self.assertTrue(self._effective(lead)._scope_partition()["ineligible"])
        origins = lead._journey_origins(match.channel_id, row)
        self.assertEqual(origins["items"][-1]["scope"], "outside_period")
        self.assertNotIn("gclid", json.dumps(origins))

    def test_journey_old_epoch_cannot_clear_new_pending_and_failed_job_retries(self):
        _handoff, match = self._claim()
        first = match.journey_revision
        match._enqueue_journey()
        self.assertFalse(self.env.company._job_whatsapp_match(match.id, first))
        self.assertTrue(match.journey_pending)
        with patch.object(
            type(self.env["marketing.website.whatsapp.journey.service"]),
            "_project_claim",
            side_effect=ValidationError("synthetic"),
        ):
            with self.assertRaises(ValidationError):
                self._run(match)
        self.assertTrue(match.journey_pending)
        self._run(match)
        self.assertFalse(match.journey_pending)

    def test_journey_paged_claim_wakes_all_businesses_after_final_page(self):
        _handoff, match = self._claim()
        rows = [self._business(match, scope="context")[1] for _i in range(3)]
        with patch(
            "odoo.addons.marketing_center_website_whatsapp.models.journey_service.BATCH",
            2,
        ), trap_jobs() as jobs:
            self.env.company._job_whatsapp_match(match.id, match.journey_revision)
            self.assertTrue(match.journey_pending)
            self.env.company._job_whatsapp_match(
                match.id, match.journey_revision, rows[1].id
            )
            self.assertFalse(match.journey_pending)
            jobs.assert_enqueued_job(
                self.env.company._job_whatsapp_wake,
                args=(match.id, match.journey_revision),
            )
        with trap_jobs() as jobs:
            self.env.company._job_whatsapp_wake(match.id, match.journey_revision)
            jobs.assert_jobs_count(0, only=self.env.company._job_whatsapp_match)

    def test_journey_capture_policy_drift_blocks_first_but_not_existing_point(self):
        handoff, match = self._claim()
        endpoint = self.action.binding_id.endpoint_id
        endpoint.write({"capture_enabled": False})
        self._run(match)
        self.assertFalse(handoff.journey_touchpoint_id)
        self.assertEqual(
            handoff._journey_privacy_status(first_projection=True),
            "capture_unavailable",
        )
        endpoint.write({"capture_enabled": True})
        current, current_match = self._claim()
        self._run(current_match)
        point = current.journey_touchpoint_id
        endpoint.write({"capture_enabled": False})
        self.assertEqual(current._journey_privacy_status(), "ready")
        self._run(current_match)
        self.assertEqual(current.journey_touchpoint_id, point)
        self.assertEqual(len(point.evidence_ids), 1)

    def test_journey_retention_erases_private_values_and_keeps_audit_metadata(self):
        handoff, match = self._claim(
            acquisition_json={
                "gclid": "synthetic-secret",
                "gad_campaignid": "23172129564",
                "utm_campaign": "Synthetic campaign",
            }
        )
        lead, row = self._business(match)
        self._run(match, row)
        point = handoff.journey_touchpoint_id
        old_hash = point.identifier_ids.comparison_hash
        assets = dict(point.asset_refs_json)
        resolutions = point.asset_resolution_ids
        self.assertTrue(resolutions)
        handoff._erase_journey_values()
        self.assertTrue(point.privacy_erased_at)
        self.assertTrue(point.identifier_ids.erased_at)
        self.assertNotEqual(point.identifier_ids.comparison_hash, old_hash)
        self.assertFalse(point.landing_url)
        self.assertFalse(point.utm_campaign)
        self.assertFalse(point.extensions_json)
        self.assertEqual(point.asset_refs_json, assets)
        self.assertEqual(assets["campaign_id"], "23172129564")
        self.assertEqual(assets["campaign_provider"], "google")
        self.assertEqual(point.asset_resolution_ids, resolutions)
        self.assertEqual(
            self.env["marketing.website.whatsapp.journey.api"]._handoff_item(
                handoff, match, row
            )["campaign"],
            {"status": "erased"},
        )
        self.assertFalse(handoff.acquisition_json)
        self.assertEqual(handoff.page_url, "about:blank")
        self.env.company._job_whatsapp_erasure(handoff.id)
        self._run(match, row)
        self.assertFalse(self._effective(lead))
        self.assertEqual(handoff.journey_touchpoint_id, point)
        self.assertEqual(len(point.evidence_ids), 1)

    def test_journey_case_actions_native_merge_and_panel_unlink_trigger_convergence(
        self,
    ):
        if "contact.center.case" not in self.env:
            self.skipTest("Optional Kanban integration is not installed")
        team = self.env["crm.team"].create(
            {
                "name": "Synthetic P2 case",
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
            }
        )
        stage = self.env["crm.stage"].create(
            {"name": "Synthetic P2 stage", "team_id": team.id}
        )
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Synthetic P2 pipeline",
                "code": "p2_" + uuid.uuid4().hex,
                "company_id": self.env.company.id,
            }
        )
        binding = self.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": pipeline.id, "crm_team_id": team.id}
        )
        core_stage = (
            self.env["contact.center.crm.stage.binding"]
            .search(
                [
                    ("pipeline_binding_id", "=", binding.id),
                    ("crm_stage_id", "=", stage.id),
                ]
            )
            .stage_id
        )
        service_team = self.account.access_team_ids
        service_team.write(
            {"pipeline_ids": [(4, pipeline.id)], "default_pipeline_id": pipeline.id}
        )
        self.env["contact.center.crm.team.binding"].create(
            {"contact_center_team_id": service_team.id, "crm_team_id": team.id}
        )
        self.account.default_pipeline_id = pipeline
        _handoff, match = self._claim()
        case = match.channel_id.contact_center_case_ids.filtered("is_default")
        case.with_user(self.agent).action_transition(core_stage.id)
        with trap_jobs() as jobs:
            action = case.with_user(self.agent).action_create_crm_lead()
        lead = self.env["crm.lead"].browse(action["res_id"])
        row = lead._conversation_links()
        self.assertTrue(row.website_journey_pending)
        jobs.assert_enqueued_job(
            self.env.company._job_whatsapp_link,
            args=(row.id, row.website_journey_revision),
        )
        confirmed = row.with_user(self.agent)._confirm_scope(self.when)
        self._run(match, confirmed)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        duplicate = lead.copy({"name": "Synthetic merge survivor"})
        survivor = (lead | duplicate)._merge_opportunity()
        current = survivor._conversation_links()
        self.assertTrue(current)
        self._run(match, current)
        self.assertFalse(match.journey_pending)
        self.assertTrue(self._effective(survivor)._scope_partition()["eligible"])
        case.invalidate_recordset()
        case.with_user(self.agent).action_unlink_crm_lead()
        self.assertTrue(current.state == "active")
        active_support = self.env["marketing.attribution.crm.link"].search(
            [
                ("lead_id", "=", survivor.id),
                ("authority_key", "=", "website.whatsapp"),
                (
                    "source_ref",
                    "=",
                    "website.whatsapp:match:%s:link:%s" % (match.id, current.id),
                ),
                ("revocation_ids", "=", False),
            ]
        )
        self.assertTrue(active_support)
        self.env["contact.center.ui.api"].with_user(self.agent).unlink_crm_opportunity(
            match.channel_id.id, survivor.id
        )
        self._run(match, current)
        self.assertFalse(self._effective(survivor))
        self.assertEqual(
            set(active_support.revocation_ids.mapped("reason")),
            {"website_whatsapp_authority_removed"},
        )

    def test_journey_automation_step_uses_native_link_trigger(self):
        if "automation.configuration" not in self.env:
            self.skipTest("Optional Automation integration is not installed")
        _handoff, match = self._claim()
        identity = match.message_binding_id.channel_binding_id.identity_id
        alias = self.env["contact.center.identity.alias"].create(
            {
                "account_id": self.account.id,
                "identity_id": identity.id,
                "namespace": "whatsapp.pn",
                "value_raw": "5511998765432@s.whatsapp.net",
                "value_normalized": "5511998765432@s.whatsapp.net",
            }
        )
        from odoo.addons.contact_center_base.tests.test_start_conversation import (
            DirectStartTestAdapter,
        )

        self.env["contact.center.provider.connection"].create(
            {
                "name": "Synthetic P2 direct start",
                "account_id": self.account.id,
                "adapter_key": "test.direct_start",
                "external_ref": str(uuid.uuid4()),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
            }
        )
        configuration = self.env["automation.configuration"].create(
            {
                "name": "Synthetic P2 workflow",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "company_id": self.env.company.id,
                "cc_lead_entry": True,
                "cc_execution_user_id": self.agent.id,
                "is_periodic": True,
            }
        )
        self.env["automation.configuration.step"].create(
            {
                "configuration_id": configuration.id,
                "name": "Synthetic P2 step",
                "step_type": "contact_center",
                "cc_account_id": self.account.id,
                "cc_body": "Synthetic message",
                "trigger_interval": -1,
                "cc_stop_on_reply": False,
                "cc_stop_on_human": False,
            }
        )
        configuration.start_automation()
        configuration.cc_send_enabled = True
        with trap_jobs(), patch.object(
            type(self.env.cr), "now", return_value=fields.Datetime.now()
        ):
            lead = self.env["crm.lead"].create(
                {
                    "name": "Synthetic P2 automation lead",
                    "company_id": self.env.company.id,
                    "type": "lead",
                    "user_id": self.agent.id,
                    "phone": "+5511998765432",
                }
            )
        record = configuration._create_record(lead)
        step = record.automation_step_ids
        with trap_jobs() as jobs, patch.object(
            type(self.env["contact.center.ui.api"]),
            "_send_automation_message",
            return_value={
                "message_id": match.message_binding_id.message_id.id,
                "state": "pending",
            },
        ), patch.object(
            DirectStartTestAdapter,
            "execute_command",
            side_effect=AssertionError("No external transport"),
        ):
            self.assertTrue(step._run_contact_center())
        row = lead._conversation_links()
        self.assertEqual(row.channel_id, match.channel_id)
        self.assertEqual(row.writer, "automation")
        self.assertTrue(row.website_journey_pending)
        jobs.assert_enqueued_job(
            self.env.company._job_whatsapp_link,
            args=(row.id, row.website_journey_revision),
        )
        self._run(match, row)
        self.assertFalse(row.website_journey_pending)
        self.assertTrue(self._effective(lead)._scope_partition()["pending"])
        self.assertTrue(alias)

    def _individual_capture(self):
        endpoint = self.action.binding_id.endpoint_id
        self.website.cookies_bar = True
        endpoint.write(
            {
                "website_tracking_policy": "individual_consent",
                "privacy_legal_basis_code": "consent",
                "consent_ttl_days": 1,
            }
        )
        decision = self.env["marketing.website.consent"]._decide(
            self.action.binding_id, True
        )
        context = dict(
            self.env.context,
            website_consent_internal=CONSENT_CONTEXT_TOKEN,
            website_consent_id=decision.id,
        )
        with patch.object(self, "env", self.env(context=context)):
            handoff, match = self._claim()
        self.assertEqual(handoff.capture_consent_id, decision)
        return handoff, match, decision

    def test_journey_replacing_grant_or_expiring_receipt_keeps_existing_credit(self):
        handoff, match, decision = self._individual_capture()
        lead, row = self._business(match)
        self._run(match, row)
        lead._job_resolve_native_utm()
        lead._job_resolve_native_utm()
        receipts = self.env["marketing.crm.utm.application"].search_count(
            [("lead_id", "=", lead.id)]
        )
        self.env["marketing.website.consent"]._decide(
            self.action.binding_id, True, previous_cookie=decision._cookie()
        )
        self.assertEqual(handoff._journey_privacy_status(), "ready")
        self._run(match, row)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertFalse(decision.whatsapp_erasure_queued)
        # Receipt TTL is shorter than the frozen attribution retention.
        with patch.object(fields.Datetime, "now", return_value=decision.expires_at):
            self.assertEqual(handoff._journey_privacy_status(), "ready")
            self._run(match, row)
            lead._job_resolve_native_utm()
            self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertEqual(
            self.env["marketing.crm.utm.application"].search_count(
                [("lead_id", "=", lead.id)]
            ),
            receipts,
        )

    def test_journey_erasure_preserves_manual_native_utm(self):
        handoff, match = self._claim(acquisition_json={"utm_campaign": "Captured"})
        lead, row = self._business(match)
        self._run(match, row)
        manual = self.env["utm.campaign"].create({"name": "Manual synthetic"})
        lead.campaign_id = manual
        handoff._erase_journey_values()
        self.env.company._job_whatsapp_erasure(handoff.id)
        self._run(match, row)
        lead._job_resolve_native_utm()
        self.assertEqual(lead.campaign_id, manual)
        self.assertFalse(self._effective(lead))

    def test_journey_legacy_ingress_collision_stays_visible_without_duplicate(self):
        handoff, match = self._claim()
        self.action.binding_id.capture_mode = "legacy"
        self.env["marketing.website.action.service"]._ingest_action(
            self.action,
            handoff.event_id,
            str(uuid.uuid4()),
            "organic_link",
            self.origin,
            self.when,
        )
        existing = handoff._journey_legacy_event()
        self.assertTrue(existing.touchpoint_id)
        self.action.binding_id.capture_mode = "native"
        lead, row = self._business(match)
        self._run(match, row)
        self.assertFalse(handoff.journey_touchpoint_id)
        self.assertFalse(self._effective(lead))
        self.assertEqual(
            lead._journey_origins(match.channel_id, row)["items"][-1]["scope"],
            "legacy_collision",
        )

    def test_journey_visitor_business_paging_and_cross_website_tracks(self):
        visitor = self.env["website.visitor"].create(
            {
                "website_id": self.website.id,
                "access_token": uuid.uuid4().hex,
            }
        )
        handoff, match = self._claim(visitor_id=visitor.id)
        self._run(match)
        for _index in range(21):
            self._business(match, scope="context")
        self.env["website.track"].create(
            [
                {
                    "visitor_id": visitor.id,
                    "url": self.origin + "/?utm_campaign=Visible",
                },
                {
                    "visitor_id": visitor.id,
                    "url": "https://other-site.invalid/?utm_campaign=Hidden",
                },
            ]
        )
        api = self.env["marketing.website.whatsapp.journey.api"]
        self.agent.groups_id |= self.env.ref(
            "contact_center_base.group_contact_center_admin"
        ) | self.env.ref("website.group_website_designer")
        api = api.with_user(self.agent)
        visits = api.visitor_journey(visitor.id)
        self.assertEqual(visits["total"], 1)
        self.assertNotIn("Hidden", json.dumps(visits))
        first = api.visitor_journey(visitor.id, "clicks")["items"][0]["conversations"][
            0
        ]
        self.assertEqual(len(first["businesses"]), 20)
        self.assertTrue(first["businesses_has_more"])
        second = api.visitor_businesses(visitor.id, match.id, offset=20)
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["has_more"])
        self.assertFalse(
            {item["id"] for item in first["businesses"]}
            & {item["id"] for item in second["items"]}
        )
        self.assertEqual(handoff.visitor_id, visitor)

    def test_journey_explicit_refusal_queues_once_and_erases_current_decision(self):
        handoff, match, decision = self._individual_capture()
        self._run(match)
        decision.with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN).write(
            {"revoked_at": fields.Datetime.now()}
        )
        decision._explicit_refusal()
        decision._explicit_refusal()
        pending = self.env["queue.job"].search(
            [("identity_key", "=", "whatsapp:withdrawal:%s:0" % decision.id)]
        )
        self.assertEqual(len(pending), 1)
        failed = Job.load(self.env, pending.uuid)
        failed.set_failed(exc_info="Synthetic exhausted withdrawal")
        failed.store()
        decision._explicit_refusal()
        self.assertEqual(
            self.env["queue.job"].search_count(
                [
                    ("identity_key", "=", "whatsapp:withdrawal:%s:0" % decision.id),
                    ("state", "=", "pending"),
                ]
            ),
            1,
        )
        self.env.company._job_whatsapp_withdrawal(decision.id)
        self.assertTrue(handoff.erased_at)
        self.assertFalse(handoff.capture_consent_id)
        with trap_jobs() as jobs:
            decision._explicit_refusal()
            jobs.assert_jobs_count(0)

    def test_journey_first_refusal_queues_even_without_visible_handoffs(self):
        _handoff, _match, previous = self._individual_capture()
        decision = self.env["marketing.website.consent"]._decide(
            self.action.binding_id, True, previous_cookie=previous._cookie()
        )
        self.assertFalse(
            self.env["marketing.website.whatsapp.handoff"].search(
                [("capture_consent_id", "=", decision.id)]
            )
        )
        with trap_jobs() as jobs:
            decision._explicit_refusal()
            jobs.assert_jobs_count(1)
            queued = jobs.enqueued_jobs[0]
            self.assertEqual(
                queued.func.__self__.env.context["allowed_company_ids"],
                decision.company_id.ids,
            )

    def test_journey_business_projection_includes_archived_and_exact_scope(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        api = self.env["marketing.website.whatsapp.journey.api"]
        before = api._business_page(match, 0, 20)["items"][0]
        self.assertFalse(before["eligible"])
        self.assertEqual(before["journey_scope"], "pending")
        self._run(match, row)
        lead.active = False
        item = api._business_page(match, 0, 20)["items"][0]
        self.assertEqual(item["id"], lead.id)
        self.assertTrue(item["archived"])
        self.assertTrue(item["eligible"])
        self.assertEqual(item["journey_scope"], "eligible")
        self.assertEqual(
            lead.action_contact_center_journey()["params"]["lead_id"],
            lead.id,
        )
        legacy, legacy_match = self._claim(capture_version=0)
        _legacy_lead, legacy_row = self._business(legacy_match)
        self.assertEqual(
            api._handoff_item(legacy, legacy_match, legacy_row)["scope"], "legacy"
        )
        blocked, blocked_match = self._claim()
        _blocked_lead, blocked_row = self._business(blocked_match)
        self.action.binding_id.endpoint_id.capture_enabled = False
        self.assertEqual(
            api._handoff_item(blocked, blocked_match, blocked_row)["scope"],
            "capture_unavailable",
        )
        self.assertFalse(
            api._business_page(blocked_match, 0, 20)["items"][0]["eligible"]
        )
        self.assertTrue(handoff.journey_touchpoint_id)

    def test_journey_rpc_rejects_foreign_ids_and_nonadmin_visitor(self):
        handoff, match = self._claim()
        lead, _row = self._business(match)
        other = self.env["crm.lead"].create(
            {"name": "Unrelated", "company_id": self.env.company.id}
        )
        with self.assertRaises(AccessError):
            other.action_website_journey_match(match.id)
        visitor = self.env["website.visitor"].create(
            {"website_id": self.website.id, "access_token": uuid.uuid4().hex}
        )
        with self.assertRaises(AccessError):
            self.env["marketing.website.whatsapp.journey.api"].with_user(
                self.agent
            ).visitor_journey(visitor.id)
        with self.assertRaises(AccessError):
            handoff.write({"acquisition_json": {}})
        self.assertTrue(lead)

    def test_journey_sales_agent_sees_own_website_chain_without_marketing_admin(self):
        _handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        result = lead.with_user(self.agent).get_contact_center_journey()
        origins = result["items"][0]["origins"]
        self.assertTrue(origins["marketing_restricted"])
        self.assertEqual(origins["status"], "ready")
        self.assertEqual(origins["items"][0]["match_id"], match.id)
        self.assertEqual(origins["items"][0]["campaign"]["status"], "restricted")
        self.assertEqual(
            lead.with_user(self.agent).action_website_journey_match(match.id)["res_id"],
            match.id,
        )
        self.outsider.groups_id |= self.env.ref(
            "sales_team.group_sale_salesman_all_leads"
        )
        hidden = lead.with_user(self.outsider).get_contact_center_journey()["items"]
        self.assertEqual(hidden, [{"restricted": True, "can_open": False}])
        with self.assertRaises(AccessError):
            lead.with_user(self.outsider).action_website_journey_match(match.id)

    def test_journey_company_clear_and_restore_preserve_valid_support(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        assertions = self.env["marketing.attribution.crm.link"].search(
            [("authority_key", "=", "website.whatsapp"), ("lead_id", "=", lead.id)]
        )
        self.assertEqual(len(assertions), 1)
        lead.company_id = False
        self.assertFalse(self._effective(lead)._scope_partition()["eligible"])
        self._run(match, row)
        self.assertFalse(assertions.revocation_ids)
        self.assertEqual(
            self.env["marketing.website.whatsapp.journey.api"]._handoff_item(
                handoff, match, row
            )["scope"],
            "business_context",
        )
        lead.company_id = self.env.company
        self._run(match, row)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search(
                [("authority_key", "=", "website.whatsapp"), ("lead_id", "=", lead.id)]
            ),
            assertions,
        )
        lead.company_id = False
        match.with_user(self.agent).action_reject()
        self._run(match, row)
        self.assertTrue(assertions.revocation_ids)
        lead.company_id = self.env.company
        self._run(match, row)
        self.assertFalse(self._effective(lead))

    def test_journey_missing_active_support_is_not_displayed_as_credit(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        self._run(match, row)
        assertions = self.env["marketing.attribution.crm.link"].search(
            [("authority_key", "=", "website.whatsapp"), ("lead_id", "=", lead.id)]
        )
        self.env["marketing.crm.service"]._revoke_attribution_assertions(
            assertions, "website.whatsapp", str(match.id), "synthetic:revoked-support"
        )
        item = self.env["marketing.website.whatsapp.journey.api"]._handoff_item(
            handoff, match, row
        )
        self.assertEqual(item["scope"], "support_review")
        self.assertFalse(self._effective(lead))

    def test_journey_visits_use_allowed_origins_and_label_unconfigured_host(self):
        visitor = self.env["website.visitor"].create(
            {"website_id": self.website.id, "access_token": uuid.uuid4().hex}
        )
        alternate = "https://www.whatsapp.example.test"
        self.action.binding_id.endpoint_id.write(
            {
                "allowed_origins": self.origin + "\n" + alternate,
                "allowed_hosts": "whatsapp.example.test\nwww.whatsapp.example.test",
            }
        )
        self.website.domain = False
        self.env["website.track"].create(
            [
                {"visitor_id": visitor.id, "url": origin + "/?utm_campaign=" + name}
                for origin, name in (
                    (self.origin, "First"),
                    (alternate, "Alternate"),
                    ("https://foreign.invalid", "Hidden"),
                )
            ]
        )
        api = self.env["marketing.website.whatsapp.journey.api"]
        result = api.visitor_journey(visitor.id)
        self.assertEqual(result["total"], 2)
        self.assertNotIn("Hidden", json.dumps(result))
        self.assertEqual(result["status"], "ready")
        self.action.binding_id.active = False
        result = api.visitor_journey(visitor.id)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["status"], "host_unconfigured")
        self.website.domain = self.origin
        self.assertEqual(api.visitor_journey(visitor.id)["total"], 1)

    def test_journey_deleted_business_navigation_raises_access_error(self):
        visitor = self.env["website.visitor"].create(
            {"website_id": self.website.id, "access_token": uuid.uuid4().hex}
        )
        _handoff, match = self._claim(visitor_id=visitor.id)
        lead, _row = self._business(match)
        removed_id = lead.id
        lead.unlink()
        with self.assertRaises(AccessError):
            self.env["marketing.website.whatsapp.journey.api"].open_visitor_business(
                visitor.id, match.id, removed_id
            )

    def test_journey_visitor_without_click_shows_campaign_and_possible_ip_stays_separate(
        self,
    ):
        now = fields.Datetime.now()
        visitors = self.env["website.visitor"].create(
            [
                {
                    "website_id": self.website.id,
                    "access_token": uuid.uuid4().hex,
                    "marketing_ip_address": "192.0.2.12",
                    "marketing_ip_observed_at": now + datetime.timedelta(hours=index),
                }
                for index in range(2)
            ]
        )
        self.env["website.track"].create(
            {
                "visitor_id": visitors[0].id,
                "url": self.origin + "/?gad_campaignid=23172129564&gclid=secret",
                "visit_datetime": now,
            }
        )
        api = self.env["marketing.website.whatsapp.journey.api"]
        visits = api.visitor_journey(visitors[0].id)
        self.assertEqual(len(visits["items"]), 1)
        self.assertEqual(visits["items"][0]["page_url"], self.origin + "/")
        self.assertFalse(api.visitor_journey(visitors[0].id, "clicks")["items"])
        related = api.visitor_journey(visitors[0].id, "possible")
        self.assertEqual(related["items"][0]["visitor_id"], visitors[1].id)
        self.assertNotIn("192.0.2.12", json.dumps(related))
        self.assertTrue(visitors.exists())

    def test_journey_customer_context_cannot_read_private_website_snapshot(self):
        _handoff, match = self._claim(
            acquisition_json={"utm_campaign": "Private acquisition"}
        )
        own, row = self._business(match)
        customer = self.env["res.partner"].create({"name": "Synthetic customer"})
        match.message_binding_id.channel_binding_id.identity_id.with_user(
            self.agent
        ).action_link_partner(customer.id)
        own.partner_id = customer
        current = self.env["crm.lead"].create(
            {
                "name": "Other customer business",
                "partner_id": customer.id,
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
            }
        )
        self._run(match, row)
        for marketing_admin in (False, True):
            if marketing_admin:
                self.agent.groups_id |= self.env.ref(
                    "marketing_center_base.group_marketing_center_admin"
                )
            result = current.with_user(self.agent).get_contact_center_journey(
                area="context"
            )
            self.assertEqual(len(result["items"]), 1)
            item = result["items"][0]
            self.assertTrue(item["can_open"])
            self.assertEqual(item["scope"], "customer_context")
            self.assertFalse(
                any(
                    origin["type"] == "website_whatsapp"
                    for origin in item["origins"]["items"]
                )
            )
            self.assertFalse(item["origins"].get("website_has_more"))
            self.assertNotIn("Private acquisition", json.dumps(result))
            own_origins = own.with_user(self.agent).get_contact_center_journey()[
                "items"
            ][0]["origins"]["items"]
            self.assertTrue(
                any(origin["type"] == "website_whatsapp" for origin in own_origins)
            )

    def test_journey_withdrawal_isolates_failure_and_retries_without_browser_cookie(
        self,
    ):
        first, _match, decision = self._individual_capture()
        with patch.object(
            self,
            "env",
            self.env(
                context=dict(
                    self.env.context,
                    website_consent_internal=CONSENT_CONTEXT_TOKEN,
                    website_consent_id=decision.id,
                )
            ),
        ):
            second = self._handoff()
        self.assertEqual(second.capture_consent_id, decision)
        decision.with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN).write(
            {"revoked_at": fields.Datetime.now()}
        )
        original = type(first)._erase_journey_values
        failed_once = set()

        def erase(record, *args, **kwargs):
            if record.id == first.id and not failed_once:
                failed_once.add(record.id)
                raise RuntimeError("Synthetic transient refusal failure")
            return original(record, *args, **kwargs)

        with patch.object(
            type(first), "_erase_journey_values", new=erase
        ), trap_jobs() as jobs:
            self.env.company._job_whatsapp_withdrawal(decision.id)
            self.assertFalse(first.erased_at)
            self.assertTrue(second.erased_at)
            self.assertEqual(first.retention_failures, 1)
            retries = [
                job
                for job in jobs.enqueued_jobs
                if job.method_name == "_job_whatsapp_withdrawal"
            ]
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0].args, (decision.id, 0, 1))
            self.assertTrue(retries[0].eta)
            retries[0].perform()
        self.assertTrue(first.erased_at)
        self.assertFalse(first.capture_consent_id)

    def test_journey_withdrawal_exhaustion_stays_requeueable(self):
        handoff, _match, decision = self._individual_capture()
        with patch.object(
            type(handoff),
            "_erase_journey_values",
            side_effect=RuntimeError("Synthetic persistent failure"),
        ), trap_jobs() as jobs:
            with self.assertRaises(RuntimeError):
                self.env.company._job_whatsapp_withdrawal(decision.id, attempt=5)
            jobs.assert_jobs_count(0)
        self.assertFalse(handoff.erased_at)
        self.env.company._job_whatsapp_withdrawal(decision.id, attempt=5)
        self.assertTrue(handoff.erased_at)

    def test_journey_message_at_shared_boundary_credits_only_successor(self):
        acquired = self.when - datetime.timedelta(minutes=5)
        handoff, match = self._claim(
            clicked_at=self.when - datetime.timedelta(minutes=1),
            acquisition_json={
                "acquisition_at": acquired.isoformat(),
                "acquisition_provenance": "track",
            },
        )
        first, first_row = self._business(
            match, start=acquired - datetime.timedelta(minutes=1), end=self.when
        )
        second, second_row = self._business(match, start=self.when)
        self._run(match, first_row)
        self._run(match, second_row)
        self.assertEqual(match.message_at, self.when)
        self.assertEqual(handoff.journey_touchpoint_id.occurred_at, acquired)
        self.assertFalse(self._effective(first)._scope_partition()["eligible"])
        self.assertTrue(self._effective(second)._scope_partition()["eligible"])

    def test_journey_erased_native_forms_and_trees_hide_placeholder_url(self):
        handoff, match = self._claim()
        self._run(match)
        handoff._erase_journey_values()
        for record, prefix in (
            (handoff, "view_handoff"),
            (match, "view_website_whatsapp_match"),
        ):
            self.assertEqual(record.page_url, "about:blank")
            self.assertTrue(record.erased_at)
            for view_type in ("form", "tree"):
                view = self.env.ref(
                    "marketing_center_website_whatsapp.%s_%s" % (prefix, view_type)
                )
                arch = etree.fromstring(
                    record.get_view(view_id=view.id, view_type=view_type)["arch"]
                )
                for node in arch.xpath(".//field[@name='page_url']"):
                    modifiers = json.loads(node.get("modifiers", "{}"))
                    self.assertTrue(record.filtered_domain(modifiers["invisible"]))
                if view_type == "form":
                    labels = arch.xpath(
                        ".//span[contains(text(), 'Apagado por privacidade')]"
                    )
                    self.assertTrue(labels)
                    self.assertFalse(
                        record.filtered_domain(
                            json.loads(labels[0].get("modifiers"))["invisible"]
                        )
                    )
                else:
                    labels = arch.xpath(".//field[@name='erased_at']")
                    self.assertEqual(
                        labels[0].get("string"), "Apagado por privacidade em"
                    )
                    self.assertEqual(labels[0].get("optional"), "show")

    def test_journey_visitor_unlink_exposes_gap_without_changing_frozen_origin(self):
        visitor = self.env["website.visitor"].create(
            {"website_id": self.website.id, "access_token": uuid.uuid4().hex}
        )
        acquired = self.when - datetime.timedelta(days=1)
        track = self.env["website.track"].create(
            {
                "visitor_id": visitor.id,
                "url": self.origin + "/",
                "visit_datetime": acquired,
            }
        )
        handoff, match = self._claim(
            visitor_id=visitor.id,
            track_id=track.id,
            acquisition_json={
                "utm_campaign": "Frozen visitor campaign",
                "acquisition_at": acquired.isoformat(),
                "acquisition_provenance": "track",
            },
        )
        lead, row = self._business(match)
        self._run(match, row)
        self.agent.groups_id |= self.env.ref(
            "marketing_center_base.group_marketing_center_admin"
        )
        snapshot = dict(handoff.acquisition_json)
        point = handoff.journey_touchpoint_id
        before = lead.with_user(self.agent).get_contact_center_journey()["items"][0][
            "origins"
        ]["items"][-1]
        self.assertEqual(before["visitor_state"], "available")
        self.assertEqual(before["campaign"]["name"], "Frozen visitor campaign")
        visitor.unlink()
        after = lead.with_user(self.agent).get_contact_center_journey()["items"][0][
            "origins"
        ]["items"][-1]
        self.assertEqual(after["visitor_state"], "removed_or_unavailable")
        for key in (
            "campaign",
            "acquisition_at",
            "clicked_at",
            "message_at",
            "reference",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(handoff.acquisition_json, snapshot)
        self.assertEqual(point.occurred_at, acquired)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])

    def test_journey_first_companyless_convergence_restarts_on_company_restore(self):
        handoff, match = self._claim()
        lead, row = self._business(match)
        lead.company_id = False
        self._run(match, row)
        point = handoff.journey_touchpoint_id
        self.assertTrue(point)
        self.assertFalse(self._effective(lead))
        self.assertFalse(row.website_journey_pending)
        with trap_jobs() as jobs:
            lead.with_user(self.agent).company_id = self.env.company
            jobs.assert_enqueued_job(
                self.env.company._job_whatsapp_link,
                args=(row.id, row.website_journey_revision),
            )
            self.assertTrue(row.website_journey_pending)
            website_jobs = [
                job
                for job in jobs.enqueued_jobs
                if job.method_name == "_job_whatsapp_link"
            ]
            self.assertEqual(len(website_jobs), 1)
            website_jobs[0].perform()
        self.assertFalse(row.website_journey_pending)
        self.assertTrue(self._effective(lead)._scope_partition()["eligible"])
        self.assertEqual(handoff.journey_touchpoint_id, point)
        self.assertEqual(len(point.evidence_ids), 1)
        self.assertEqual(
            self.env["marketing.attribution.crm.link"].search_count(
                [
                    ("authority_key", "=", "website.whatsapp"),
                    ("lead_id", "=", lead.id),
                    ("revocation_ids", "=", False),
                ]
            ),
            1,
        )

    def test_journey_cookie_and_unknown_origin_do_not_infer_acquisition_page(self):
        for provenance in ("cookie", "none"):
            handoff, match = self._claim(
                landing_url=self.origin + "/clicked",
                page_url=self.origin + "/clicked",
                acquisition_json={
                    "acquisition_provenance": provenance,
                    "utm_campaign": "Cookie campaign",
                },
            )
            self._run(match)
            item = self.env["marketing.website.whatsapp.journey.api"]._handoff_item(
                handoff, match
            )
            self.assertFalse(item["landing_url"])
            self.assertEqual(item["page_url"], self.origin + "/clicked")
            self.assertEqual(handoff.landing_url, self.origin + "/clicked")
            self.assertEqual(
                handoff.journey_touchpoint_id.landing_url, self.origin + "/clicked"
            )
