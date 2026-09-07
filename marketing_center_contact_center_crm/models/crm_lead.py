from odoo import models

from odoo.addons.contact_center_crm.models.conversation_link import (
    conversation_graph_is_locked,
)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    def _marketing_contact_center_lock_graph(
        self,
        *,
        touch_leads=False,
        touch_channels=False,
    ):
        """Fence the Contact graph before either bridge locks CRM leads.

        ``contact_center_crm`` and ``marketing_center_crm`` both extend
        ``crm.lead``.  Their relative MRO is an installation detail, so neither
        dependency can by itself guarantee the cross-addon lock order.  This
        final bridge is deliberately the orchestrator: catalog/binding/core
        authorities are acquired before the CRM lead and linked conversations, and the
        context capability makes both dependency implementations reuse that
        already-locked graph.
        """

        if not self.ids or conversation_graph_is_locked(self.env):
            return self
        # The graph helper discovers hidden bridge rows under sudo.  Preserve
        # the caller's authorization boundary by checking it before that wait.
        self.check_access_rights("write")
        self.check_access_rule("write")
        return self._contact_center_lock_conversation_graph(
            touch_leads=touch_leads,
            touch_channels=touch_channels,
        )

    def write(self, values):
        leads = self
        if {"company_id", "team_id", "stage_id"} & set(values):
            leads = self._marketing_contact_center_lock_graph(touch_channels=True)
        return super(CrmLead, leads).write(values)

    def _merge_opportunity(
        self,
        user_id=False,
        team_id=False,
        auto_unlink=True,
        max_length=5,
    ):
        leads = self._marketing_contact_center_lock_graph(
            touch_leads=True,
            touch_channels=True,
        )
        return super(CrmLead, leads)._merge_opportunity(
            user_id=user_id,
            team_id=team_id,
            auto_unlink=auto_unlink,
            max_length=max_length,
        )
