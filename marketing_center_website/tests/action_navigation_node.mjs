// Exercise the actual shipped bridge/bootstrap with delayed receipts and analytics.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const read = (name) => fs.readFileSync(new URL(`../static/src/js/${name}.esm.js`, import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "").replace(/^export /gm, "");
const claim = {actionRef: "11111111-1111-4111-8111-111111111111", eventId: "22222222-2222-4222-8222-222222222222", sessionRef: "33333333-3333-4333-8333-333333333333"};
const receipt = `1788264000.1788264120.${"a".repeat(64)}`;
const result = JSON.stringify({id: 9, marketing_center_receipt: receipt});
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const order = [];
const window = {
    setTimeout, clearTimeout, TextEncoder, AbortController, location: {origin: "https://example.test"},
    async fetch() { await sleep(45); order.push("exchange"); return {ok: true, status: 202, json: async () => ({accepted: true})}; },
};
const context = vm.createContext({
    console, Promise, URL, Date, setTimeout, clearTimeout, window,
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    document: {getElementById() { return {dataset: {captureMode: "legacy"}}; }, addEventListener() { /* No browser events in this fixture. */ }, dispatchEvent(event) {
        assert.equal(event.type, "marketing_center:action-confirmed");
        assert.equal(event.detail.kind, "form_submission");
        assert.deepEqual(Object.keys(event.detail).sort(), ["event_id", "kind", "waitUntil"]);
        order.push("confirmed");
        event.detail.waitUntil(sleep(30).then(() => order.push("analytics_callback")));
    }},
    ajax: {post: () => Promise.resolve(result)},
    loadConfig: async () => ({capture_mode: "legacy"}), validConfig: () => true,
});
vm.runInContext(read("action_capture") + "\n" + read("action_bootstrap") + "\nglobalThis.api={createFormPostBridge,exchangeForm};", context);
await sleep(0);
const {createFormPostBridge, exchangeForm} = context.api;
const bridge = (native, exchange, limit = 2000) => createFormPostBridge(native, "https://example.test", () => claim, exchange, limit);

// A delayed successful exchange and registered callback finish before navigation.
assert.equal(await bridge(() => Promise.resolve(result), exchangeForm)("/website/form/crm.lead", {}), result);
order.push("navigation");
assert.deepEqual(order, ["exchange", "confirmed", "analytics_callback", "navigation"]);

// A hung or rejected tracker does not alter successful form results or hang forever.
const started = Date.now();
assert.equal(await bridge(() => Promise.resolve(result), () => new Promise(() => { /* Deliberately pending to verify the timeout. */ }), 25)("/website/form/crm.lead", {}), result);
assert.ok(Date.now() - started < 500);
assert.equal(await bridge(() => Promise.resolve(result), () => Promise.reject(new Error("tracking failed")))("/website/form/crm.lead", {}), result);

// Failed native forms and success pages alone must never exchange or signal a lead.
let exchanged = false;
const failure = new Error("native failed");
await assert.rejects(bridge(() => Promise.reject(failure), () => {exchanged = true;})("/website/form/crm.lead", {}), (error) => error === failure);
assert.equal(await bridge(() => Promise.resolve('{"error":"invalid"}'), () => {exchanged = true;})("/website/form/crm.lead", {}), '{"error":"invalid"}');
const untouched = Promise.resolve("thank you page");
assert.equal(bridge(() => untouched, () => {exchanged = true;})("/contactus-thank-you", {}), untouched);
assert.equal(exchanged, false);
console.log("Action navigation tests: delayed exchange/callback precede navigation; bounded failures preserve native results; invalid forms and thank-you visits do not convert.");
