/** @odoo-module **/

/* global QUnit */

import {
    boundedActionResponse,
    createFormPostBridge,
    formExchangePayload,
    nativeFormTarget,
    nativeTrackedLinkActivation,
    prepareNativeFormUrl,
    technicalActionRef,
    trustedHumanActivation,
    validRedirectPath,
    whatsappActionRef,
    whatsappClaimPayload,
    whatsappFallbackPath,
} from "@marketing_center_website/js/action_capture.esm";

const ACTION_REF = "11111111-1111-4111-8111-111111111111";
const EVENT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_REF = "33333333-3333-4333-8333-333333333333";
const RECEIPT = `1788264000.1788264120.${"a".repeat(64)}`;

function removeNeutralizedDatabaseBanner() {
    const banner = document.getElementById("oe_neutralize_banner");
    if (!banner) {
        return;
    }
    const wrapper = banner.parentElement;
    if (wrapper && wrapper !== document.body) {
        wrapper.remove();
        return;
    }
    banner.remove();
}

function claim() {
    return {
        actionRef: ACTION_REF,
        eventId: EVENT_ID,
        sessionRef: SESSION_REF,
    };
}

QUnit.module("marketing_center_website > technical actions", {
    beforeEach: removeNeutralizedDatabaseBanner,
});

QUnit.test(
    "action timeout covers stalled bodies with or without abort support",
    async (assert) => {
        for (const abortSupported of [true, false]) {
            let timeout;
            let cleared = false;
            let aborted = false;
            const scope = {
                setTimeout(callback, milliseconds) {
                    assert.strictEqual(milliseconds, 5000);
                    timeout = callback;
                    return 1;
                },
                clearTimeout() {
                    cleared = true;
                },
                fetch: async () => ({
                    ok: true,
                    status: 200,
                    json: () =>
                        new Promise(() => {
                            // Headers arrived, but the response body never completes.
                        }),
                }),
            };
            if (abortSupported) {
                scope.AbortController = class {
                    abort() {
                        aborted = true;
                    }
                };
            }
            const request = boundedActionResponse(
                "/marketing/website-action/whatsapp/claim",
                {},
                scope
            );
            await Promise.resolve();
            timeout();
            await assert.rejects(request, /timed out/);
            assert.ok(cleared, "deadline timer is released");
            assert.strictEqual(aborted, abortSupported);
        }
    }
);

QUnit.test("completed action responses release their deadline", async (assert) => {
    let cleared = false;
    const payload = {redirect_path: "/local-result"};
    const response = await boundedActionResponse(
        "/local-action",
        {},
        {
            fetch: async () => ({ok: true, status: 200, json: async () => payload}),
            setTimeout: () => 1,
            clearTimeout() {
                cleared = true;
            },
        }
    );
    assert.deepEqual(response, {ok: true, status: 200, payload});
    assert.ok(cleared);
});

QUnit.test("adds only opaque references to the exact native form route", (assert) => {
    const prepared = prepareNativeFormUrl(
        "/website/form/crm.lead",
        "https://www.example.test",
        claim()
    );
    assert.ok(prepared.tracked);
    const parsed = new URL(prepared.url, "https://www.example.test");
    assert.strictEqual(parsed.pathname, "/website/form/crm.lead");
    assert.deepEqual(Array.from(parsed.searchParams.keys()).sort(), [
        "mc_action",
        "mc_event",
        "mc_session",
    ]);
    assert.notOk(
        prepareNativeFormUrl(
            "https://evil.example/website/form/crm.lead",
            "https://www.example.test",
            claim()
        ).tracked
    );
    assert.notOk(
        prepareNativeFormUrl(
            "/other/form/crm.lead",
            "https://www.example.test",
            claim()
        ).tracked
    );
});

QUnit.test("passes the native body and promise through by identity", async (assert) => {
    assert.expect(5);
    const opaqueBody = {deliberately: "not inspected"};
    let receivedBody = null;
    let receivedUrl = null;
    const nativePromise = Promise.resolve(
        JSON.stringify({id: 9, marketing_center_receipt: RECEIPT})
    );
    const nativePost = (url, body) => {
        receivedUrl = url;
        receivedBody = body;
        return nativePromise;
    };
    let exchanged = null;
    const bridge = createFormPostBridge(
        nativePost,
        "https://www.example.test",
        claim,
        (payload) => {
            exchanged = payload;
        }
    );
    const returned = bridge("/website/form/crm.lead", opaqueBody);
    assert.strictEqual(returned, nativePromise, "the native promise is unchanged");
    assert.strictEqual(receivedBody, opaqueBody, "the native body is unchanged");
    assert.ok(receivedUrl.startsWith("/website/form/crm.lead?"));
    await nativePromise;
    await Promise.resolve();
    assert.deepEqual(exchanged, formExchangePayload(claim(), RECEIPT));
    assert.notOk(JSON.stringify(exchanged).includes("deliberately"));
});

QUnit.test("does not consume a claim for unrelated legacy AJAX", (assert) => {
    let takeCount = 0;
    const originalResult = {};
    const bridge = createFormPostBridge(
        () => originalResult,
        "https://www.example.test",
        () => {
            takeCount += 1;
            return claim();
        },
        () => assert.notOk(true, "must not exchange")
    );
    assert.notOk(nativeFormTarget("/web/dataset/call_kw", "https://www.example.test"));
    assert.strictEqual(bridge("/web/dataset/call_kw", {}), originalResult);
    assert.strictEqual(takeCount, 0);
});

QUnit.test("requires a visible trusted user activation", (assert) => {
    const visible = {visibilityState: "visible"};
    const active = {userActivation: {isActive: true}};
    const event = {
        isTrusted: true,
        button: 0,
        altKey: false,
        ctrlKey: false,
        metaKey: false,
        shiftKey: false,
    };
    assert.ok(trustedHumanActivation(event, visible, active));
    assert.notOk(trustedHumanActivation({...event, isTrusted: false}, visible, active));
    assert.notOk(trustedHumanActivation(event, {visibilityState: "hidden"}, active));
    assert.notOk(
        trustedHumanActivation(event, visible, {
            userActivation: {isActive: false},
        })
    );
    assert.notOk(trustedHumanActivation({...event, ctrlKey: true}, visible, active));
});

QUnit.test("reads only the explicit technical marker from the event path", (assert) => {
    let requestedAttribute = "";
    const event = {
        composedPath() {
            return [
                {},
                {
                    getAttribute(name) {
                        requestedAttribute = name;
                        return ACTION_REF;
                    },
                },
            ];
        },
    };
    assert.strictEqual(
        technicalActionRef(event, "data-marketing-whatsapp-action"),
        ACTION_REF
    );
    assert.strictEqual(requestedAttribute, "data-marketing-whatsapp-action");
});

QUnit.test(
    "leaves eligible native tracked links under link.tracker ownership",
    (assert) => {
        const nativeEvent = {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            if (name === "href") {
                                return "/r/nativeCode";
                            }
                            if (name === "data-marketing-whatsapp-action") {
                                return ACTION_REF;
                            }
                            return null;
                        },
                    },
                ];
            },
        };
        const externalEvent = {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            return name === "href"
                                ? "https://other.example/r/nativeCode"
                                : null;
                        },
                    },
                ];
            },
        };
        const mailingTraceEvent = {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            if (name === "href") {
                                return "/r/nativeCode/m/42";
                            }
                            if (name === "data-marketing-whatsapp-action") {
                                return ACTION_REF;
                            }
                            return null;
                        },
                    },
                ];
            },
        };
        const smsTraceEvent = {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            if (name === "href") {
                                return "/r/nativeCode/s/84";
                            }
                            if (name === "data-marketing-whatsapp-action") {
                                return ACTION_REF;
                            }
                            return null;
                        },
                    },
                ];
            },
        };
        const handoffEvent = {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            if (name === "href") {
                                return "/contactus";
                            }
                            if (name === "data-marketing-whatsapp-action") {
                                return ACTION_REF;
                            }
                            return null;
                        },
                    },
                ];
            },
        };

        assert.ok(nativeTrackedLinkActivation(nativeEvent, "https://www.example.test"));
        assert.strictEqual(
            whatsappActionRef(nativeEvent, "https://www.example.test"),
            "",
            "the native redirect remains responsible for recording the click"
        );
        assert.strictEqual(
            whatsappActionRef(mailingTraceEvent, "https://www.example.test"),
            "",
            "native mass-mailing traces remain under Link Tracker ownership"
        );
        assert.strictEqual(
            whatsappActionRef(smsTraceEvent, "https://www.example.test"),
            "",
            "native SMS traces remain under Link Tracker ownership"
        );
        assert.notOk(
            nativeTrackedLinkActivation(externalEvent, "https://www.example.test")
        );
        assert.strictEqual(
            whatsappActionRef(handoffEvent, "https://www.example.test"),
            ACTION_REF,
            "a dedicated WhatsApp handoff remains eligible"
        );
    }
);

QUnit.test("keeps WhatsApp claims and redirects opaque and bounded", (assert) => {
    assert.deepEqual(whatsappClaimPayload(claim()), {
        action_ref: ACTION_REF,
        event_id: EVENT_ID,
        session_ref: SESSION_REF,
    });
    assert.ok(validRedirectPath(`/marketing/website-action/go/${"A".repeat(43)}`));
    assert.notOk(validRedirectPath("https://wa.me/5519999999999"));
    assert.notOk(
        validRedirectPath(`/marketing/website-action/go/${"A".repeat(43)}?next=evil`)
    );
});

QUnit.test("keeps a safe ordinary link as the WhatsApp fail-open path", (assert) => {
    function activation(href) {
        return {
            composedPath() {
                return [
                    {
                        getAttribute(name) {
                            if (name === "data-marketing-whatsapp-action") {
                                return ACTION_REF;
                            }
                            return name === "href" ? href : null;
                        },
                    },
                ];
            },
        };
    }

    const origin = "https://www.example.test";
    assert.strictEqual(
        whatsappFallbackPath(activation("/contactus?from=hero#form"), origin),
        "/contactus?from=hero#form"
    );
    assert.strictEqual(
        whatsappFallbackPath(activation("https://www.example.test/support"), origin),
        "/support"
    );
    assert.strictEqual(
        whatsappFallbackPath(activation("https://other.example/support"), origin),
        "",
        "an external URL cannot become the technical fallback"
    );
    assert.strictEqual(
        whatsappFallbackPath(
            activation("https://user@www.example.test/support"),
            origin
        ),
        ""
    );
});
