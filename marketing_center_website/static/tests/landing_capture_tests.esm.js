/** @odoo-module **/

/* global QUnit */

import {
    buildLandingPayload,
    eligibleLandingPath,
    opaqueUuid,
    trackedQueryValues,
} from "@marketing_center_website/js/landing_capture.esm";

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

QUnit.module("marketing_center_website > landing capture", {
    beforeEach: removeNeutralizedDatabaseBanner,
});

QUnit.test("uses only the explicit attribution query allowlist", (assert) => {
    assert.deepEqual(
        trackedQueryValues(
            "?utm_source=google&gclid=opaque-123&email=private%40example.invalid"
        ),
        {utm_source: "google", gclid: "opaque-123"}
    );
    assert.deepEqual(
        trackedQueryValues(
            "?utm_term=line%0Abreak&fbclid=opaque-meta&gclid=contains%25pii"
        ),
        {fbclid: "opaque-meta"},
        "control characters and non-opaque click values are discarded"
    );
});

QUnit.test(
    "removes every query and fragment from landing and referrer URLs",
    (assert) => {
        const payload = buildLandingPayload(
            {
                origin: "https://www.example.test",
                pathname: "/solar",
                search: "?utm_campaign=spring&name=Private",
            },
            "https://search.example.test/result?q=private#fragment",
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            new Date("2026-09-01T12:00:00Z")
        );
        assert.deepEqual(payload, {
            event_id: "11111111-1111-4111-8111-111111111111",
            event_type: "entry_point",
            occurred_at: "2026-09-01T12:00:00.000Z",
            landing_url: "https://www.example.test/solar",
            consent_state: "unknown",
            session_ref: "22222222-2222-4222-8222-222222222222",
            utm_campaign: "spring",
            referrer_url: "https://search.example.test/",
        });
        assert.notOk(JSON.stringify(payload).includes("Private"));
        assert.notOk(JSON.stringify(payload).includes("q=private"));
    }
);

QUnit.test("creates RFC 4122 version 4 opaque identifiers", (assert) => {
    const deterministicCrypto = {
        getRandomValues(values) {
            values.fill(0xaa);
            return values;
        },
    };
    assert.strictEqual(
        opaqueUuid(deterministicCrypto),
        "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    );
});

QUnit.test("does not capture Odoo technical, login or portal routes", (assert) => {
    for (const pathname of [
        "/web/login",
        "/my/orders",
        "/portal",
        "/auth/oauth",
        "/website/info",
        "/marketing/website-ingress/config",
    ]) {
        assert.notOk(eligibleLandingPath(pathname), pathname);
    }
    assert.ok(eligibleLandingPath("/"));
    assert.ok(eligibleLandingPath("/contactus"));
    assert.ok(eligibleLandingPath("/solutions/solar"));
});
