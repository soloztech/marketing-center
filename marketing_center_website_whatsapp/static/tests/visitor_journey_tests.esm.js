/** @odoo-module **/
/* global QUnit */
import "@marketing_center_website_whatsapp/js/visitor_journey.esm";
import {
    nextAnimationFrame,
    start,
    startServer,
} from "@mail/../tests/helpers/test_utils";
import {makeDeferred} from "@web/../tests/helpers/utils";

const click = (extra = {}) => ({
    type: "website_whatsapp",
    reference: "A2B3",
    privacy: "ready",
    provenance: "track",
    campaign: {
        status: "resolved",
        name: "Synthetic campaign",
        evidence: "url_and_local_catalog",
    },
    page_url: "https://example.test/product",
    landing_url: "https://example.test/",
    visit_at: "2026-10-07 10:00:00",
    acquisition_at: "2026-10-07 10:00:00",
    clicked_at: "2026-10-07 10:02:00",
    visitor_state: "available",
    conversations: [],
    ...extra,
});
async function setup(options = {}) {
    await startServer();
    const calls = [];
    const result = await start({
        mockRPC(route, args) {
            if (args.model === "marketing.website.whatsapp.journey.api") {
                calls.push(args);
                if (args.method === "visitor_journey") {
                    if (options.error) {
                        throw new Error("offline");
                    }
                    if (options.deferred) {
                        return options.deferred;
                    }
                    const area = args.args[1];
                    const offset = args.kwargs.offset || 0;
                    if (area === "visits" && options.errorOffset === offset) {
                        options.errorOffset = null;
                        throw new Error("page offline once");
                    }
                    const items = options[area] || [];
                    return {
                        status: {visits: options.visitsStatus}[area] || "ready",
                        items: items.slice(offset, offset + 20),
                        offset,
                        limit: 20,
                        has_more: offset + 20 < items.length,
                    };
                }
                if (args.method === "open_visitor_conversation") {
                    return options.openDeferred;
                }
                if (args.method === "visitor_businesses") {
                    return {
                        items: [
                            {id: 21, name: "Last visible business", eligible: false},
                        ],
                        offset: args.kwargs.offset,
                        has_more: false,
                        status: "ready",
                    };
                }
                if (
                    args.method === "open_visitor_business" ||
                    args.method === "open_possible_visitor"
                ) {
                    throw new Error("revoked");
                }
            }
            if (args.model === "contact.center.ui.api") {
                if (args.method === "systray_summary") {
                    return {schema_version: 1, enabled: false};
                }
                if (args.method === "bootstrap") {
                    return {
                        schema_version: 1,
                        user: {id: 3},
                        accounts: [],
                        agents: [],
                        tags: [],
                        capabilities: {},
                    };
                }
            }
        },
    });
    // Do not await action completion: loading remains observable, and closing
    // during a pending request must suppress its late response.
    result.env.services.action.doAction({
        type: "ir.actions.client",
        tag: "marketing.website.whatsapp.journey",
        params: {visitor_id: 6211},
    });
    await nextAnimationFrame();
    return {...result, calls};
}
QUnit.module("marketing_center_website_whatsapp > visitor journey", () => {
    QUnit.test(
        "cookie, unknown and clamped acquisition show click fallback",
        async (assert) => {
            await setup({
                clicks: ["cookie", "none", "track"].map((provenance) =>
                    click({
                        provenance,
                        landing_url:
                            provenance === "track"
                                ? "https://example.test/landing"
                                : false,
                        acquisition_at:
                            provenance === "track" ? "2026-10-07 10:00:01" : false,
                        at: "2026-10-07 10:00:00",
                        clicked_at: "2026-10-07 10:00:00",
                    })
                ),
            });
            const text = document.body.textContent;
            assert.ok(text.includes("Cookie de campanha"));
            assert.ok(text.includes("Origem não identificada"));
            assert.ok(text.includes("Histórico de visitas do site"));
            assert.strictEqual(
                (text.match(/Página de aquisição: Não identificada\./g) || []).length,
                2
            );
            assert.strictEqual(
                (text.match(/a ocorrência usa o horário do clique/g) || []).length,
                3
            );
        }
    );
    QUnit.test(
        "missing trusted host is configuration state rather than no visits",
        async (assert) => {
            await setup({visitsStatus: "host_unconfigured"});
            assert.ok(
                document.body.textContent.includes(
                    "configure um domínio ou origem autorizada"
                )
            );
            assert.notOk(
                document.body.textContent.includes("Nenhuma visita disponível")
            );
        }
    );
    QUnit.test(
        "archived and unprojected businesses keep explicit states",
        async (assert) => {
            await setup({
                clicks: [
                    click({
                        conversations: [
                            {
                                match_id: 1,
                                state: "reference",
                                businesses: [
                                    "eligible",
                                    "pending",
                                    "legacy",
                                    "capture_unavailable",
                                    "decision_changed",
                                ].map((journey_scope, index) => ({
                                    id: index + 1,
                                    name: `Business ${index}`,
                                    archived: index === 0,
                                    eligible: journey_scope === "eligible",
                                    journey_scope,
                                })),
                            },
                        ],
                    }),
                ],
            });
            const text = document.body.textContent;
            for (const label of [
                "Arquivado",
                "Dentro do período confirmado",
                "Aguardando processamento",
                "Captura anterior à jornada: sem crédito automático",
                "Captura bloqueada pela configuração atual",
                "Decisão de consentimento alterada",
            ])
                assert.ok(text.includes(label), label);
        }
    );
    QUnit.test(
        "visitor without click keeps campaign visits and empty conversation state",
        async (assert) => {
            await setup({
                visits: [
                    {
                        at: "2026-10-07 10:00:00",
                        page_url: "https://example.test/",
                        campaign: {
                            status: "unresolved",
                            url_campaign_id: "23172129564",
                        },
                    },
                ],
            });
            assert.ok(document.body.textContent.includes("23172129564"));
            assert.ok(document.body.textContent.includes("Nenhum clique vinculado"));
            assert.ok(
                document.body.textContent.includes("rede pode ser compartilhada")
            );
        }
    );
    QUnit.test(
        "click exposes separate clocks, catalog evidence and restricted conversation",
        async (assert) => {
            await setup({clicks: [click({conversations: [{restricted: true}]})]});
            const text = document.body.textContent;
            assert.ok(text.includes("Synthetic campaign"));
            assert.ok(text.includes("A2B3"));
            assert.ok(text.includes("Aquisição:"));
            assert.ok(text.includes("Clique:"));
            assert.ok(text.includes("Conversa com acesso restrito"));
            assert.notOk(text.includes("gclid"));
        }
    );
    QUnit.test(
        "erased origin and absent acquisition date are explicit",
        async (assert) => {
            await setup({
                clicks: [
                    click({
                        privacy: "privacy_unavailable",
                        campaign: {status: "erased"},
                        acquisition_at: false,
                        page_url: false,
                        landing_url: false,
                    }),
                ],
            });
            assert.ok(document.body.textContent.includes("Apagado por privacidade"));
            assert.ok(document.body.textContent.includes("Não identificado"));
            assert.notOk(document.body.textContent.includes("https://example.test"));
        }
    );
    QUnit.test(
        "visits paginate independently from possible accesses",
        async (assert) => {
            const {click: press, calls} = await setup({
                visits: Array.from({length: 21}, () => ({
                    at: false,
                    page_url: false,
                    campaign: {status: "unknown"},
                })),
            });
            await press('[data-area="visits"] .d-flex > button:last-child');
            assert.strictEqual(calls[calls.length - 1].kwargs.offset, 20);
            assert.containsOnce(document.body, '[data-area="visits"] article');
        }
    );
    QUnit.test(
        "failed second page clears rows and retries the same offset",
        async (assert) => {
            const {click: press, calls} = await setup({
                visits: Array.from({length: 45}, () => ({
                    at: false,
                    page_url: false,
                    campaign: {status: "unknown"},
                })),
                errorOffset: 20,
            });
            await press('[data-area="visits"] .d-flex > button:last-child');
            assert.containsNone(document.body, '[data-area="visits"] article');
            assert.ok(
                document.querySelector(
                    '[data-area="visits"] .d-flex > button:last-child'
                ).disabled
            );
            await press('[data-area="visits"] [role="alert"] button');
            assert.containsN(document.body, '[data-area="visits"] article', 20);
            assert.deepEqual(
                calls
                    .filter(
                        (call) =>
                            call.method === "visitor_journey" &&
                            call.args[1] === "visits"
                    )
                    .map((call) => call.kwargs.offset),
                [0, 20, 20]
            );
        }
    );
    QUnit.test(
        "listing failure offers retry without sensitive data",
        async (assert) => {
            await setup({error: true});
            assert.containsN(document.body, 'section [role="alert"]', 3);
            assert.ok(document.body.textContent.includes("Tentar novamente"));
        }
    );
    QUnit.test(
        "double opening and late response after close do not open a chat",
        async (assert) => {
            const deferred = makeDeferred();
            const {click: press, calls} = await setup({
                clicks: [
                    click({
                        conversations: [
                            {
                                match_id: 11,
                                state: "reference",
                                message_at: "2026-10-07 10:03:00",
                                businesses: [],
                            },
                        ],
                    }),
                ],
                openDeferred: deferred,
            });
            const button = document.querySelector('[data-area="clicks"] .btn-primary');
            button.click();
            button.click();
            await nextAnimationFrame();
            assert.strictEqual(
                calls.filter((call) => call.method === "open_visitor_conversation")
                    .length,
                1
            );
            await press(".modal-footer button");
            deferred.resolve({schema_version: 1, item: {channel_id: 11}});
            await nextAnimationFrame();
            assert.containsNone(document.body, ".o_ChatWindow");
        }
    );
    QUnit.test(
        "nested business pages keep conversation and request authorized continuation",
        async (assert) => {
            const {click: press, calls} = await setup({
                clicks: [
                    click({
                        conversations: [
                            {
                                match_id: 11,
                                state: "confirmed",
                                businesses_offset: 0,
                                businesses_has_more: true,
                                businesses_status: "ready",
                                businesses: [
                                    {
                                        id: 20,
                                        name: "First visible business",
                                        eligible: false,
                                    },
                                ],
                            },
                        ],
                    }),
                ],
            });
            const buttons = [
                ...document.querySelectorAll('[data-area="clicks"] button'),
            ];
            await press(
                buttons.find((button) =>
                    button.textContent.includes("Próximos negócios")
                )
            );
            assert.ok(document.body.textContent.includes("Last visible business"));
            assert.notOk(document.body.textContent.includes("First visible business"));
            const request = calls.find((call) => call.method === "visitor_businesses");
            assert.deepEqual(request.args, [6211, 11]);
            assert.strictEqual(request.kwargs.offset, 20);
        }
    );
    QUnit.test(
        "revoked business action reports the current access failure",
        async (assert) => {
            const {click: press} = await setup({
                clicks: [
                    click({
                        conversations: [
                            {
                                match_id: 11,
                                state: "confirmed",
                                message_at: false,
                                businesses: [
                                    {
                                        id: 20,
                                        name: "Synthetic business",
                                        eligible: false,
                                    },
                                ],
                            },
                        ],
                    }),
                ],
            });
            await press('[data-area="clicks"] .btn-link');
            assert.ok(document.body.textContent.includes("O acesso pode ter mudado"));
        }
    );
});
