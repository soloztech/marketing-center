import datetime
import uuid

from odoo import Command, fields
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.models.native_utm import _NATIVE_UTM_WRITE_TOKEN
from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO


class TestMarketingCampaignBoardLeads(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Board other"})
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Board CRM Meta",
                "company_id": cls.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_8001",
                "state": "active",
            }
        )
        viewer_group = cls.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        own = cls.env.ref("sales_team.group_sale_salesman")
        every = cls.env.ref("sales_team.group_sale_salesman_all_leads")
        cls.marketer = cls._user("marketer", viewer_group)
        cls.seller = cls._user("seller", viewer_group | own)
        cls.colleague = cls._user("colleague", viewer_group | own)
        cls.manager = cls._user("manager", viewer_group | every)
        team = cls.env["marketing.center.team"].create(
            {"name": "Board CRM roster", "company_id": cls.company.id}
        )
        for user in cls.marketer | cls.seller | cls.colleague | cls.manager:
            cls.env["marketing.center.team.member"].create(
                {"team_id": team.id, "user_id": user.id, "role": "viewer"}
            )
        cls.env["marketing.center.team.source"].create(
            {"team_id": team.id, "source_id": cls.source.id, "access_mode": "read"}
        )
        cls.utm = cls.env["utm.campaign"].create({"name": "Board UTM"})
        cls.campaign = cls._campaign("1")
        cls.campaign.with_context(
            marketing_native_utm_write_token=_NATIVE_UTM_WRITE_TOKEN
        ).write({"native_utm_campaign_id": cls.utm.id})
        cls.unlinked = cls._campaign("2")

    @classmethod
    def _user(cls, suffix, groups):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Board %s" % suffix,
                    "login": "board-%s-%s" % (suffix, uuid.uuid4()),
                    "company_id": cls.company.id,
                    "company_ids": [Command.set((cls.company | cls.other_company).ids)],
                    "groups_id": [Command.set(groups.ids)],
                }
            )
        )

    @classmethod
    def _campaign(cls, number):
        result = cls.env["marketing.center.catalog.service"]._upsert_entity(
            cls.company,
            cls.source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref="act_8001/campaigns/%s" % number,
                external_id=number,
                name="Board campaign %s" % number,
                remote_status="active",
                observed_at=datetime.datetime(2026, 9, 20, 12),
            ),
        )
        return cls.env["marketing.center.external.entity"].browse(result.entity_id)

    def _lead(self, owner, *, company=None, active=True, age_days=0):
        lead = self.env["crm.lead"].create(
            {
                "name": "Board lead %s" % uuid.uuid4(),
                "user_id": owner.id,
                "company_id": (company or self.company).id,
                "campaign_id": self.utm.id,
                "active": active,
            }
        )
        if age_days:
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE crm_lead SET create_date = %s WHERE id = %s",
                [fields.Datetime.now() - datetime.timedelta(days=age_days), lead.id],
            )
            lead.invalidate_recordset(["create_date"])
        return lead

    def _label(self, user, record=None, companies=None):
        record = (record or self.campaign).with_user(user)
        if companies:
            record = record.with_context(allowed_company_ids=companies.ids)
        record.invalidate_recordset()
        return record.campaign_lead_label

    def test_leads_are_counted_in_the_user_security_context(self):
        self._lead(self.seller)
        self._lead(self.seller, active=False)  # lost leads were still acquired
        self._lead(self.colleague)
        self._lead(self.seller, age_days=45)  # outside the 30-day window
        self._lead(self.seller, company=self.other_company)
        self.assertEqual(self._label(self.marketer), "No CRM access")
        self.assertEqual(
            self._label(self.seller, companies=self.company),
            "2 leads visible to you in 30 days",
            "a salesperson restricted to own documents counts only them",
        )
        self.assertEqual(
            self._label(self.manager, companies=self.company),
            "3 leads visible to you in 30 days",
        )
        self.assertEqual(
            self._label(self.manager, companies=self.company | self.other_company),
            "4 leads visible to you in 30 days",
            "the active companies decide which leads are visible",
        )
        self.assertEqual(
            self._label(self.manager, self.unlinked), "No linked UTM campaign"
        )

    def test_board_view_shows_the_lead_line_only_with_crm(self):
        view = self.env["marketing.center.external.entity"].get_views(
            [
                (
                    self.env.ref(
                        "marketing_center_base.view_marketing_campaign_board_kanban"
                    ).id,
                    "kanban",
                )
            ]
        )["views"]["kanban"]
        self.assertIn('name="campaign_lead_label"', view["arch"])
