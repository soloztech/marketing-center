/** @odoo-module **/

/* global QUnit */

import {
    bootstrapGeneration,
    captureModeHint,
    invalidateBootstrap,
    loadBootstrap,
} from "@marketing_center_website/js/bootstrap_config.esm";
import {createModeFormPostBridge} from "@marketing_center_website/js/action_capture.esm";

QUnit.module("marketing_center_website > shared bootstrap");

QUnit.test(
    "readers share one generation; prior response cannot authorize after refusal",
    async (assert) => {
        const original = window.fetch;
        let resolve;
        let calls = 0;
        window.fetch = (path, options) => {
            assert.strictEqual(path, "/marketing/website/bootstrap-config");
            assert.strictEqual(options.cache, "no-store");
            calls++;
            return new Promise((done) => {
                resolve = done;
            });
        };
        try {
            invalidateBootstrap();
            const first = loadBootstrap();
            const second = loadBootstrap();
            assert.strictEqual(
                first,
                second,
                "same Promise for ingress and consent readers"
            );
            assert.strictEqual(calls, 1);
            const before = bootstrapGeneration();
            document.dispatchEvent(
                new CustomEvent("marketing_center:consent-changed", {
                    detail: {granted: false, confirmed: false},
                })
            );
            assert.ok(bootstrapGeneration() > before);
            resolve({
                ok: true,
                json: async () => ({
                    schema_version: 1,
                    ingress: {enabled: true},
                    consent: {available: true, granted: true},
                }),
            });
            assert.strictEqual(await first, null, "stale envelope is denied");
            window.fetch = async () => ({
                ok: true,
                json: async () => ({
                    schema_version: 1,
                    ingress: {enabled: true},
                    consent: {available: true, granted: true, capture_allowed: true},
                }),
            });
            const fenced = await loadBootstrap();
            assert.strictEqual(
                fenced.ingress.enabled,
                false,
                "a GET before the POST cannot lift withdrawal"
            );
            assert.strictEqual(fenced.consent.granted, false);
        } finally {
            window.fetch = original;
            document.dispatchEvent(
                new CustomEvent("marketing_center:consent-changed", {
                    detail: {granted: true, confirmed: true},
                })
            );
            invalidateBootstrap();
        }
    }
);

QUnit.test("HTML hint has three states", (assert) => {
    let node = document.getElementById("marketing_measurement_config");
    const created = !node;
    if (created) {
        node = document.createElement("div");
        node.id = "marketing_measurement_config";
        document.body.appendChild(node);
    }
    const previous = node.getAttribute("data-capture-mode");
    try {
        node.dataset.captureMode = "native";
        assert.strictEqual(captureModeHint(), "native");
        node.dataset.captureMode = "legacy";
        assert.strictEqual(captureModeHint(), "legacy");
        node.dataset.captureMode = "unknown";
        assert.strictEqual(captureModeHint(), null);
        node.removeAttribute("data-capture-mode");
        assert.strictEqual(captureModeHint(), null);
    } finally {
        if (created) node.remove();
        else if (previous === null) node.removeAttribute("data-capture-mode");
        else node.setAttribute("data-capture-mode", previous);
    }
});

QUnit.test(
    "native uses original post and never constructs the legacy adapter",
    async (assert) => {
        let mode = "native";
        let enabled = false;
        let built = 0;
        let nativeCalls = 0;
        let originalCalls = 0;
        let legacyCalls = 0;
        const original = () => {
            originalCalls++;
            return Promise.resolve("original result");
        };
        const native = (...args) => {
            nativeCalls++;
            return original(...args);
        };
        const post = createModeFormPostBridge(
            original,
            native,
            () => mode,
            () => enabled,
            (originalPost) => {
                built++;
                assert.strictEqual(originalPost, original);
                return (...args) => {
                    legacyCalls++;
                    return originalPost(...args);
                };
            }
        );
        assert.strictEqual(await post("/website/form/crm.lead", {}), "original result");
        assert.strictEqual(nativeCalls, 1);
        assert.strictEqual(originalCalls, 1);
        assert.strictEqual(built, 0);
        mode = "legacy";
        await post("/website/form/crm.lead", {});
        assert.strictEqual(built, 0, "disabled legacy goes straight to original");
        enabled = true;
        await post("/website/form/crm.lead", {});
        await post("/website/form/crm.lead", {});
        assert.strictEqual(built, 1);
        assert.strictEqual(legacyCalls, 2);
        mode = "native";
        await post("/website/form/crm.lead", {});
        assert.strictEqual(nativeCalls, 2);
        assert.strictEqual(legacyCalls, 2, "switch back does not chain through legacy");
        mode = null;
        enabled = false;
        assert.strictEqual(await post("/website/form/crm.lead", {}), "original result");
        assert.strictEqual(
            nativeCalls,
            2,
            "unknown mode preserves the original form POST"
        );
        assert.strictEqual(legacyCalls, 2);
    }
);
