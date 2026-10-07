/** @odoo-module **/
import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {deserializeDateTime, formatDateTime} from "@web/core/l10n/dates";
import {Dialog} from "@web/core/dialog/dialog";
import {openContactCenterChat} from "@contact_center_ui/js/contact_center_messaging.esm";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {validateEnvelope} from "@contact_center_ui/js/contact_center_model.esm";

const api = "marketing.website.whatsapp.journey.api";
const blank = () => ({
    items: [],
    offset: 0,
    limit: 20,
    has_more: false,
    loading: false,
    error: "",
});
export class VisitorJourney extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.alive = true;
        this.state = useState({
            visits: blank(),
            clicks: blank(),
            possible: blank(),
            opening: false,
            error: "",
        });
        onWillDestroy(() => {
            this.alive = false;
        });
        onWillStart(() =>
            Promise.all(["visits", "clicks", "possible"].map((area) => this.load(area)))
        );
    }
    isAlive() {
        return this.alive && !this.props.closeState.closed;
    }
    date(value) {
        return value ? formatDateTime(deserializeDateTime(value)) : "Não identificado";
    }
    provenance(value) {
        return (
            {
                referrer: "URL da página clicada",
                track: "Histórico de visitas do site",
                cookie: "Cookie de campanha",
                none: "Origem não identificada",
                legacy: "Captura anterior",
            }[value] || "Origem não identificada"
        );
    }
    campaign(value) {
        if (value.status === "restricted") {
            return "Campanha com acesso restrito";
        }
        if (value.status === "erased") {
            return "Apagado por privacidade";
        }
        if (value.status === "utm") return `UTM informado: ${value.name || ""}`;
        if (value.status === "ambiguous")
            return `Campanha na URL: ${value.url_campaign_id} (mais de uma conta no catálogo)`;
        return (
            value.name ||
            (value.url_campaign_id
                ? `Campanha na URL: ${value.url_campaign_id} (não encontrada no catálogo)`
                : "Campanha não identificada")
        );
    }
    businessStatus(business) {
        return (
            {
                eligible: "Dentro do período confirmado",
                pending: "Aguardando processamento",
                outside_period: "Fora do período deste negócio",
                scope_review: "Contexto: revisar período",
                business_context: "Contexto: empresa ainda não atribuída",
                legacy: "Captura anterior à jornada: sem crédito automático",
                legacy_collision: "Evento anterior: revisar origem",
                capture_unavailable: "Captura bloqueada pela configuração atual",
                decision_changed: "Decisão de consentimento alterada",
                privacy_unavailable: "Origem indisponível por privacidade",
                suggested: "Sugestão sem crédito",
                rejected: "Associação descartada",
                superseded: "Associação superada",
                claim_unavailable: "Associação indisponível para atribuição",
                support_review: "Sem atribuição ativa: revisar origem",
            }[business.journey_scope] ||
            (business.eligible
                ? "Dentro do período confirmado"
                : "Contexto, fora do período ou aguardando revisão")
        );
    }
    async load(area, offset = 0) {
        const page = this.state[area];
        if (!this.isAlive() || page.loading) {
            return;
        }
        Object.assign(page, {loading: true, error: "", items: [], offset});
        try {
            const result = await this.orm.call(
                api,
                "visitor_journey",
                [this.props.visitorId, area],
                {offset, limit: 20}
            );
            if (this.isAlive()) {
                Object.assign(page, result, {error: ""});
                this.state.error = "";
            }
        } catch {
            if (this.isAlive()) {
                page.has_more = false;
                page.error =
                    "Consulta indisponível. Confira seu acesso e tente novamente.";
            }
        } finally {
            if (this.isAlive()) {
                page.loading = false;
            }
        }
    }
    async open(matchId, leadId = false) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const args = [this.props.visitorId, matchId];
            if (leadId) {
                args.push(leadId);
            }
            const result = await this.orm.call(
                api,
                leadId ? "open_visitor_business" : "open_visitor_conversation",
                args
            );
            if (!this.isAlive()) {
                return;
            }
            if (leadId) {
                await this.action.doAction(result);
            } else {
                const messaging = await this.env.services.messaging.get();
                if (!this.isAlive()) {
                    return;
                }
                validateEnvelope(result);
                openContactCenterChat(messaging, result.item);
                this.props.close();
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Não foi possível abrir. O acesso pode ter mudado.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async related(visitorId) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const result = await this.orm.call(api, "open_possible_visitor", [
                this.props.visitorId,
                visitorId,
            ]);
            if (this.isAlive()) {
                await this.action.doAction(result);
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Acesso relacionado indisponível.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async businesses(chat, offset) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const page = await this.orm.call(
                api,
                "visitor_businesses",
                [this.props.visitorId, chat.match_id],
                {offset, limit: 20}
            );
            if (this.isAlive()) {
                Object.assign(chat, {
                    businesses: page.items,
                    businesses_status: page.status,
                    businesses_offset: page.offset,
                    businesses_has_more: page.has_more,
                });
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Negócios indisponíveis. Confira seu acesso.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async allConversations(row) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const action = await this.orm.call(api, "open_visitor_matches", [
                this.props.visitorId,
                row.handoff_id,
            ]);
            if (this.isAlive()) {
                await this.action.doAction(action);
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Associações indisponíveis. Confira seu acesso.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
}
VisitorJourney.template = "marketing_center_website_whatsapp.VisitorJourney";
VisitorJourney.components = {Dialog};
VisitorJourney.props = {visitorId: Number, closeState: Object, close: Function};
registry
    .category("actions")
    .add("marketing.website.whatsapp.journey", (env, action) => {
        const closeState = {closed: false};
        env.services.dialog.add(
            VisitorJourney,
            {visitorId: action.params.visitor_id, closeState},
            {
                onClose: () => {
                    closeState.closed = true;
                },
            }
        );
    });
