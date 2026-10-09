"""Read-only journey projections, always authorized at the current boundary."""
# Keep optional feature extensions separate in the cooperative ORM chain.
# pylint: disable=consider-merging-classes-inherited


import datetime
import ipaddress
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, ValidationError

from odoo.addons.marketing_center_website.models.crm.native_submission import (
    acquisition_values,
)
from odoo.addons.marketing_center_website.services.ingress.contracts import (
    WebIngressContractError,
    normalize_allowed_hosts,
    normalize_allowed_origins,
)

from .correlation import CLAIMED_STATES
from .handoff import ADMIN
from .journey_capture import captured_datetime


def page_url(value):
    """Never serialize identifiers embedded in a visit URL."""
    try:
        url = urlsplit(value or "")
        if url.scheme not in {"http", "https"} or url.username or url.password:
            return False
        return urlunsplit((url.scheme, url.netloc, url.path or "/", "", ""))
    except ValueError:
        return False


def stamp(value):
    return fields.Datetime.to_string(value) if value else False


class JourneyApi(models.AbstractModel):
    _name = "marketing.website.whatsapp.journey.api"
    _description = "Authorized Website acquisition journey"

    @api.model
    def _campaign(self, acquisition, company):
        catalog = self.env["marketing.center.external.entity"]
        sources = self.env["marketing.center.source"]
        if not all(
            model.check_access_rights("read", raise_exception=False)
            for model in (catalog, sources)
        ):
            return {"status": "restricted"}
        campaign = acquisition.get("gad_campaignid")
        if not campaign:
            return {
                "status": "utm" if acquisition.get("utm_campaign") else "unknown",
                "name": acquisition.get("utm_campaign") or False,
            }
        domain = [
            ("company_id", "=", company.id),
            ("service", "=", "google.ads"),
            ("active", "=", True),
            ("state", "=", "active"),
            ("read_enabled", "=", True),
        ]
        if sources.search_count(domain) != sources.sudo().search_count(domain):
            return {"status": "restricted"}
        resolver = self.env["marketing.attribution.asset.resolution.service"]
        effective = SimpleNamespace(
            company_id=company,
            touchpoint_id=SimpleNamespace(id=False),
            canonical_key="",
        )
        result = resolver._resolution_values(
            effective,
            "campaign_id",
            campaign,
            {"campaign_id": campaign, "campaign_provider": "google"},
            resolver._asset_resolver_specs(),
        )
        entity = catalog.browse(result.get("entity_id") or []).exists()
        if entity:
            try:
                entity.check_access_rule("read")
                entity.source_id.check_access_rule("read")
            except AccessError:
                return {"status": "restricted"}
        return {
            "status": result["state"],
            "url_campaign_id": campaign,
            "name": entity.name if entity else False,
            "evidence": "url_and_local_catalog" if entity else "url_only",
        }

    @api.model
    def _handoff_scope(self, handoff, match=None, link=None):
        """Shared display predicate; context cannot masquerade as earned credit."""
        privacy = handoff._journey_privacy_status(
            first_projection=not handoff.journey_touchpoint_id
        )
        collision = (
            not handoff.journey_touchpoint_id
            and handoff.capture_version == 2
            and bool(handoff._journey_legacy_event())
        )
        scope = "unlinked"
        if match:
            scope = "suggested" if match.state == "suggested" else match.state
            if match.state in CLAIMED_STATES and link:
                scope = (
                    "legacy_collision"
                    if collision
                    else "legacy"
                    if handoff.capture_version != 2
                    else privacy
                    if privacy != "ready"
                    else "claim_unavailable"
                    if not match._journey_claim_valid()
                    else "business_context"
                    if not match._journey_business_company_valid(link)
                    else "pending"
                    if not handoff.journey_touchpoint_id
                    or match.journey_pending
                    or link.website_journey_pending
                    or link.marketing_scope_pending
                    else "support_review"
                    if not match._journey_has_support(link)
                    else "eligible"
                    if link._scope_contains(match.message_at)
                    else "outside_period"
                    if link.scope_state == "confirmed"
                    else "scope_review"
                )
        if (
            match
            and link
            and handoff.journey_touchpoint_id
            and scope in {"eligible", "outside_period"}
        ):
            decision_scope = link._crm_origin_evidence_scope(
                match.message_at, handoff.journey_touchpoint_id.canonical_key
            )
            scope = {
                "eligible": "eligible",
                "pending": "scope_review",
                "ineligible": "outside_period",
            }[decision_scope]
        return scope, privacy, collision

    @api.model
    def _handoff_item(self, handoff, match=None, link=None):
        # Only call after authorizing the visitor or business and conversation.
        handoff = handoff.sudo()
        scope, privacy, collision = self._handoff_scope(handoff, match, link)
        acquisition = handoff.acquisition_json or {}
        erased = privacy == "privacy_unavailable"
        result = {
            "type": "website_whatsapp",
            "at": stamp(handoff._acquisition_time()),
            "visit_at": stamp(handoff.visit_at),
            "acquisition_at": stamp(
                captured_datetime(acquisition.get("acquisition_at"))
            ),
            "clicked_at": stamp(handoff.clicked_at),
            "message_at": stamp(match.message_at) if match else False,
            "reference": handoff.reference,
            "match_id": match.id if match else False,
            "match_state": match.state if match else False,
            "scope": scope,
            "privacy": privacy,
            "legacy_collision": collision,
            "provenance": handoff.acquisition_provenance or "legacy",
            "visitor_state": "available"
            if handoff.visitor_id
            else "removed_or_unavailable",
            "page_url": False if erased else page_url(handoff.page_url),
            "landing_url": False
            if erased or handoff.acquisition_provenance in ("cookie", "none")
            else page_url(handoff.landing_url),
            "campaign": {"status": "erased"}
            if erased
            else self._campaign(acquisition, handoff.company_id),
        }
        if match and link and handoff.journey_touchpoint_id:
            result["evidence_key"] = handoff.journey_touchpoint_id.canonical_key
            result["can_review"] = False
            try:
                self.env["crm.lead"].browse(
                    link.lead_id.id
                )._journey_origin_review_context(
                    match.channel_id.id, result["evidence_key"]
                )
                result["can_review"] = True
            except (AccessError, ValidationError):
                result["can_review"] = False
        return result

    @api.model
    def _page(self, offset, limit):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValidationError(_("Página da jornada inválida."))
        return offset, min(limit, 100)

    @api.model
    def _visit_origins(self, website):
        binding = website._marketing_measurement_binding()
        origins = ()
        if binding:
            try:
                hosts = normalize_allowed_hosts(binding.endpoint_id.allowed_hosts)
                origins = tuple(
                    origin
                    for origin in normalize_allowed_origins(
                        binding.endpoint_id.allowed_origins
                    )
                    if urlsplit(origin).hostname in hosts
                )
            except WebIngressContractError:
                origins = ()
        if origins:
            return origins
        try:
            parsed = urlsplit(website.domain or "")
        except ValueError:
            return ()
        if (
            parsed.scheme in {"https", "http"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
        ):
            return (urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),)
        return ()

    @api.model
    def _visitor(self, visitor_id):
        if not self.env.user.has_group(ADMIN):
            raise AccessError(
                _("A jornada do visitante exige administração do Contact Center.")
            )
        visitor_id = self.env["contact.center.ui.api"]._positive_id(
            visitor_id, _("visitante")
        )
        visitor = self.env["website.visitor"].browse(visitor_id).exists()
        visitor.check_access_rights("read")
        visitor.check_access_rule("read")
        if (
            not visitor
            or not visitor.website_id
            or visitor.website_id.company_id not in self.env.companies
        ):
            raise AccessError(_("Visitante indisponível nesta sessão."))
        visitor.website_id.check_access_rights("read")
        visitor.website_id.check_access_rule("read")
        return visitor

    @api.model
    def visitor_journey(self, visitor_id, area="visits", offset=0, limit=20):
        visitor = self._visitor(visitor_id)
        offset, limit = self._page(offset, limit)
        if area not in {"visits", "clicks", "possible"}:
            raise ValidationError(_("Área da jornada inválida."))
        company = visitor.website_id.company_id
        items = []
        status = "ready"
        if area == "visits":
            model = self.env["website.track"]
            model.check_access_rights("read")
            # Login can merge native tracks across Websites. A visitor's current
            # Website alone does not authorize the other Websites' history.
            origins = self._visit_origins(visitor.website_id)
            domain = [
                ("visitor_id", "=", visitor.id),
                "|",
                ("page_id", "=", False),
                ("page_id.website_id", "in", [False, visitor.website_id.id]),
            ]
            if origins:
                domain += ["|"] * (len(origins) - 1) + [
                    ("url", "=like", origin + "/%") for origin in origins
                ]
            else:
                # Without a configured host there is no reliable attribution of
                # global-page tracks to this Website. Do not infer from a path.
                domain.append(("id", "=", 0))
                status = "host_unconfigured"
            total = model.search_count(domain)
            for track in model.search(
                domain, offset=offset, limit=limit, order="visit_datetime desc,id desc"
            ):
                items.append(
                    {
                        "at": stamp(track.visit_datetime),
                        "page_url": page_url(track.url),
                        "campaign": self._campaign(
                            acquisition_values(track.url), company
                        ),
                    }
                )
        elif area == "clicks":
            model = self.env["marketing.website.whatsapp.handoff"]
            domain = [
                ("visitor_id", "=", visitor.id),
                ("website_id", "=", visitor.website_id.id),
                ("company_id", "=", company.id),
            ]
            total = model.search_count(domain)
            for handoff in model.search(
                domain, offset=offset, limit=limit, order="clicked_at desc,id desc"
            ):
                item = self._handoff_item(handoff)
                item["conversations"] = []
                item["handoff_id"] = handoff.id
                matches = self.env["marketing.website.whatsapp.match"].search(
                    [("handoff_id", "=", handoff.id)], limit=21, order="id"
                )
                item["conversations_has_more"] = len(matches) > 20
                for match in matches[:20]:
                    try:
                        self.env["contact.center.ui.api"]._authorized_channel(
                            match.channel_id.id
                        )
                    except (AccessError, MissingError, ValidationError):
                        item["conversations"].append({"restricted": True})
                        continue
                    page = self._business_page(match, 0, 20)
                    item["conversations"].append(
                        {
                            "match_id": match.id,
                            "state": match.state,
                            "message_at": stamp(match.message_at),
                            "businesses": page["items"],
                            "businesses_status": page["status"],
                            "businesses_offset": 0,
                            "businesses_has_more": page["has_more"],
                        }
                    )
                items.append(item)
        else:
            model = self.env["website.visitor"]
            at = visitor.marketing_ip_observed_at
            ip = visitor.marketing_ip_address
            try:
                ipaddress.ip_address(ip or "")
                valid_ip = True
            except ValueError:
                valid_ip = False
            if not valid_ip or not at:
                total = 0
            else:
                domain = [
                    ("id", "!=", visitor.id),
                    ("website_id", "=", visitor.website_id.id),
                    ("marketing_ip_address", "=", ip),
                    (
                        "marketing_ip_observed_at",
                        ">=",
                        at - datetime.timedelta(hours=24),
                    ),
                    (
                        "marketing_ip_observed_at",
                        "<=",
                        at + datetime.timedelta(hours=24),
                    ),
                ]
                # SQL orders the bounded candidates by distance without exposing
                # the network value. Native rules still authorize every result.
                query = model._where_calc(domain)
                model._apply_ir_rules(query, "read")
                tables, where, params = query.get_sql()
                self.env.cr.execute(
                    "SELECT count(*) FROM " + tables + " WHERE " + where, params
                )
                total = self.env.cr.fetchone()[0]
                self.env.cr.execute(
                    "SELECT website_visitor.id FROM "
                    + tables
                    + " WHERE "
                    + where
                    + " ORDER BY abs(extract(epoch from "
                    + "(marketing_ip_observed_at - %s::timestamp))),"
                    + "website_visitor.id LIMIT %s OFFSET %s",
                    list(params) + [at, limit, offset],
                )
                for row in model.browse(
                    [record[0] for record in self.env.cr.fetchall()]
                ):
                    items.append(
                        {
                            "visitor_id": row.id,
                            "observed_at": stamp(row.marketing_ip_observed_at),
                            "reference_observed_at": stamp(at),
                        }
                    )
        return {
            "area": area,
            "status": status,
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(items) < total,
        }

    @api.model
    def _visitor_match(self, visitor_id, match_id):
        visitor = self._visitor(visitor_id)
        match_id = self.env["contact.center.ui.api"]._positive_id(
            match_id, _("associação")
        )
        match = self.env["marketing.website.whatsapp.match"].browse(match_id).exists()
        match.check_access_rights("read")
        match.check_access_rule("read")
        if not match or match.handoff_id.visitor_id != visitor:
            raise AccessError(_("Conversa indisponível para este visitante."))
        self.env["contact.center.ui.api"]._authorized_channel(match.channel_id.id)
        return match

    @api.model
    def open_visitor_conversation(self, visitor_id, match_id):
        match = self._visitor_match(visitor_id, match_id)
        return self.env["contact.center.ui.api"].get_conversation(match.channel_id.id)

    @api.model
    def _business_page(self, match, offset, limit):
        leads = self.env["crm.lead"].with_context(active_test=False)
        if not leads.check_access_rights("read", raise_exception=False):
            return {
                "status": "restricted",
                "items": [],
                "has_more": False,
                "offset": offset,
            }
        # Apply native CRM rules before counting or paging. Restricted businesses
        # contribute neither names, identifiers, placeholders nor a hidden count.
        query = leads._where_calc([("company_id", "in", [False, match.company_id.id])])
        leads._apply_ir_rules(query, "read")
        tables, where, params = query.get_sql()
        where = (where or "TRUE") + (
            " AND EXISTS (SELECT 1 FROM contact_center_crm_conversation_link link "
            "WHERE link.lead_id = crm_lead.id AND link.channel_id = %s "
            "AND link.company_id = %s AND link.state = 'active')"
        )
        params = list(params) + [match.channel_id.id, match.company_id.id]
        leads.flush_model()
        self.env["contact.center.crm.conversation.link"].flush_model()
        self.env.cr.execute(
            "SELECT crm_lead.id FROM "
            + tables
            + " WHERE "
            + where
            + " ORDER BY crm_lead.id LIMIT %s OFFSET %s",
            params + [limit + 1, offset],
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        items = []
        rows = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", match.channel_id.id),
                    ("company_id", "=", match.company_id.id),
                    ("state", "=", "active"),
                    ("lead_id", "in", ids[:limit]),
                ]
            )
        )
        for lead in leads.browse(ids[:limit]):
            lead._journey_check()
            row = rows.filtered(lambda value: value.lead_id == lead)
            scope, _privacy, _collision = self._handoff_scope(
                match.handoff_id.sudo(), match, row
            )
            items.append(
                {
                    "id": lead.id,
                    "name": lead.display_name,
                    "archived": not lead.active,
                    "scope": row.scope_state,
                    "journey_scope": scope,
                    "eligible": scope == "eligible",
                }
            )
        return {
            "status": "ready",
            "items": items,
            "has_more": len(ids) > limit,
            "offset": offset,
        }

    @api.model
    def visitor_businesses(self, visitor_id, match_id, offset=0, limit=20):
        match = self._visitor_match(visitor_id, match_id)
        offset, limit = self._page(offset, limit)
        return self._business_page(match, offset, limit)

    @api.model
    def open_visitor_matches(self, visitor_id, handoff_id):
        visitor = self._visitor(visitor_id)
        handoff_id = self.env["contact.center.ui.api"]._positive_id(
            handoff_id, _("clique")
        )
        handoff = (
            self.env["marketing.website.whatsapp.handoff"].browse(handoff_id).exists()
        )
        handoff.check_access_rights("read")
        handoff.check_access_rule("read")
        if (
            not handoff
            or handoff.visitor_id != visitor
            or handoff.website_id != visitor.website_id
        ):
            raise AccessError(_("Clique indisponível para este visitante."))
        handoff.account_id._contact_center_check_user_scope()
        return {
            "type": "ir.actions.act_window",
            "res_model": "marketing.website.whatsapp.match",
            "views": [(False, "tree"), (False, "form")],
            "domain": [
                ("handoff_id", "=", handoff.id),
                ("company_id", "=", handoff.company_id.id),
            ],
        }

    @api.model
    def open_visitor_business(self, visitor_id, match_id, lead_id):
        self.open_visitor_conversation(visitor_id, match_id)
        lead_id = self.env["contact.center.ui.api"]._positive_id(lead_id, _("negócio"))
        match = self.env["marketing.website.whatsapp.match"].browse(match_id)
        lead = self.env["crm.lead"].browse(lead_id).exists()
        if not lead:
            raise AccessError(_("Negócio indisponível nesta conversa."))
        lead._journey_check()
        if not lead._journey_links().filtered(
            lambda row: row.channel_id == match.channel_id
        ):
            raise AccessError(_("Negócio indisponível nesta conversa."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "crm.lead",
            "res_id": lead.id,
            "views": [(False, "form")],
        }

    @api.model
    def open_possible_visitor(self, visitor_id, related_id):
        visitor = self._visitor(visitor_id)
        related = self._visitor(related_id)
        at, other_at = (
            visitor.marketing_ip_observed_at,
            related.marketing_ip_observed_at,
        )
        try:
            ipaddress.ip_address(visitor.marketing_ip_address or "")
            valid_ip = True
        except ValueError:
            valid_ip = False
        if (
            visitor == related
            or visitor.website_id != related.website_id
            or not at
            or not other_at
            or not valid_ip
            or visitor.marketing_ip_address != related.marketing_ip_address
            or abs((at - other_at).total_seconds()) > 24 * 3600
        ):
            raise AccessError(_("Acesso relacionado indisponível."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "website.visitor",
            "res_id": related.id,
            "views": [(False, "form")],
        }


class WebsiteVisitor(models.Model):
    _inherit = "website.visitor"

    def action_marketing_journey(self):
        self.ensure_one()
        self.env["marketing.website.whatsapp.journey.api"]._visitor(self.id)
        return {
            "type": "ir.actions.client",
            "tag": "marketing.website.whatsapp.journey",
            "params": {"visitor_id": self.id},
        }


class CrmLead(models.Model):
    _inherit = "crm.lead"

    def action_website_journey_match(self, match_id):
        self.ensure_one()
        self._journey_check()
        self.env["marketing.attribution.effective.touchpoint"].check_access_rights(
            "read"
        )
        match_id = self.env["contact.center.ui.api"]._positive_id(
            match_id, _("associação")
        )
        match = self.env["marketing.website.whatsapp.match"].browse(match_id).exists()
        match.check_access_rights("read")
        match.check_access_rule("read")
        if not match:
            raise AccessError(_("Origem indisponível."))
        self._journey_channel(match.channel_id.id)
        return {
            "type": "ir.actions.act_window",
            "res_model": match._name,
            "res_id": match.id,
            "views": [(False, "form")],
        }

    def action_website_journey_matches(self, channel_id):
        self.ensure_one()
        self.env["marketing.attribution.effective.touchpoint"].check_access_rights(
            "read"
        )
        channel = self._journey_channel(channel_id)
        return {
            "type": "ir.actions.act_window",
            "res_model": "marketing.website.whatsapp.match",
            "views": [(False, "tree"), (False, "form")],
            "domain": [
                ("channel_id", "=", channel.id),
                ("company_id", "=", channel.contact_center_company_id.id),
            ],
        }

    def _journey_origins(self, channel, link):
        result = super()._journey_origins(channel, link)
        if not link:
            # Other customer conversations are context, not this business's
            # authority to read the private Website acquisition snapshot.
            return result
        if result["status"] == "restricted":
            result.update(status="ready", marketing_restricted=True)
        offset = self.env.context.get("crm_journey_origin_offset", 0)
        matches = self.env["marketing.website.whatsapp.match"].search(
            [
                ("channel_id", "=", channel.id),
                ("company_id", "=", channel.contact_center_company_id.id),
            ],
            limit=21,
            offset=offset,
            order="id desc",
        )
        if not self.env[
            "marketing.attribution.effective.touchpoint"
        ].check_access_rights("read", raise_exception=False):
            result["marketing_restricted"] = True
            for match in matches[:20].sudo():
                handoff = match.handoff_id
                point = handoff.journey_touchpoint_id
                if not point:
                    continue
                item = self._journey_minimal_origin(point, link, match.message_at)
                item.update(
                    type="website",
                    visit_at=stamp(handoff.visit_at),
                    acquisition_at=stamp(
                        captured_datetime(
                            (handoff.acquisition_json or {}).get("acquisition_at")
                        )
                    ),
                    clicked_at=stamp(handoff.clicked_at),
                    message_at=stamp(match.message_at),
                )
                if (
                    handoff._journey_privacy_status() != "ready"
                    or not match._journey_claim_valid()
                ):
                    item.update(
                        scope="ineligible",
                        campaign_name=False,
                        ad_name=False,
                        source_name=False,
                        medium_name=False,
                        can_review=False,
                    )
                result["items"].append(item)
        else:
            result["items"].extend(
                self.env["marketing.website.whatsapp.journey.api"]._handoff_item(
                    row.handoff_id, row, link
                )
                for row in matches[:20]
            )
        result["website_has_more"] = len(matches) > 20
        result["has_more"] = bool(result.get("has_more") or len(matches) > 20)
        result["next_offset"] = offset + 20
        return result

    def _journey_origin_credit(self, channel, link, evidence_key):
        match = (
            self.env["marketing.website.whatsapp.match"]
            .sudo()
            .search(
                [
                    ("company_id", "=", link.company_id.id),
                    ("channel_id", "=", channel.id),
                    (
                        "handoff_id.journey_touchpoint_id.canonical_key",
                        "=",
                        evidence_key,
                    ),
                    ("state", "in", list(CLAIMED_STATES)),
                ],
                order="id desc",
                limit=1,
            )
        )
        if match:
            return (
                match.message_at
                if match._journey_claim_valid()
                and match.handoff_id._journey_privacy_status() == "ready"
                else None
            )
        return super()._journey_origin_credit(channel, link, evidence_key)
