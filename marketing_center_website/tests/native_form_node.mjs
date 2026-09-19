// Real shipped JS in an isolated browser boundary; no network or real leads.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const read = (name) => fs.readFileSync(new URL(`../static/src/js/${name}.esm.js`, import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "").replace(/^export /gm, "");
const origin = "https://example.test";
const requests = [];
const confirmed = [];
const listeners = new Map();
let count = 0;
let failure = true;
let nextError = false;
const uuid = () => `10000000-0000-4000-8000-${String(++count).padStart(12, "0")}`;
const context = vm.createContext({
    URL, Date, setTimeout, clearTimeout, console,
    window: {location: {origin}, setTimeout, clearTimeout, TextEncoder,
        fetch() { throw Error("Native capture must not request exchange or landing ingress"); }},
    document: {
        getElementById() { return {dataset: {captureMode: "native"}}; },
        addEventListener(type, fn) { listeners.set(type, fn); },
        dispatchEvent(event) { confirmed.push(event.detail); },
    },
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    loadConfig: async () => ({enabled: true, capture_mode: "native"}),
    validConfig: () => false,
    newActionEventId: uuid,
    websiteSessionRef() { throw Error("No session identifier is needed"); },
    ajax: {post(url, body) {
        requests.push({url, body});
        if (nextError) { nextError = false; return Promise.reject(Error("network")); }
        const eventId = new URL(url, origin).searchParams.get("mc_event");
        return Promise.resolve(JSON.stringify(failure ? {error: "invalid"} :
            {id: 42, marketing_center_event_id: eventId}));
    }},
});
vm.runInContext(read("action_capture") + "\n" + read("action_bootstrap"), context);
await Promise.resolve();
const form = {tagName: "FORM", getAttribute: () => "crm.lead"};
const activate = () => listeners.get("submit")({composedPath: () => [form]});
activate();
const body = {opaque: true};
await context.ajax.post("/website/form/crm.lead", body);
assert.equal(confirmed.length, 0, "invalid native form is never a conversion");
const first = new URL(requests[0].url, origin);
assert.deepEqual([...first.searchParams.keys()], ["mc_event"]);
assert.equal(requests[0].body, body, "opaque form body is untouched");
activate();
nextError = true;
await assert.rejects(context.ajax.post("/website/form/crm.lead", {}), /network/);
assert.equal(confirmed.length, 0);
assert.equal(new URL(requests.at(-1).url, origin).searchParams.get("mc_event"), first.searchParams.get("mc_event"));
failure = false;
activate();
const result = await context.ajax.post("/website/form/crm.lead", {});
assert.equal(JSON.parse(result).id, 42);
assert.equal(confirmed.length, 1);
assert.equal(confirmed[0].kind, "form_submission");
assert.equal(confirmed[0].event_id, first.searchParams.get("mc_event"), "a retry keeps its submission token");
activate();
await context.ajax.post("/website/form/crm.lead", {});
assert.notEqual(confirmed[1].event_id, confirmed[0].event_id, "next completed submission gets another identity");

// Disabled/unsupported browser capture never prevents the native success path.
context.newActionEventId = () => { throw Error("crypto unavailable"); };
activate();
await context.ajax.post("/website/form/crm.lead", {});
assert.equal(new URL(requests.at(-1).url, origin).searchParams.get("mc_event"), null);

// Loading the native-mode landing bootstrap does not collect/session-store/post.
const landingRequests = [];
const landing = vm.createContext({
    window: {
        location: {origin, pathname: "/contactus"},
        fetch: async (url, options) => { landingRequests.push([url, options.method]);
            return {ok: true, json: async () => ({enabled: true, capture_mode: "native"})}; },
        get sessionStorage() { throw Error("Native mode does not need session storage"); },
    },
    document: {addEventListener() { /* No browser events in this fixture. */ }},
    eligibleLandingPath: () => true,
    buildLandingPayload() { throw Error("No landing payload in native mode"); },
    opaqueUuid() { throw Error("No session/event UUID for page visits"); },
});
vm.runInContext(read("landing_bootstrap"), landing);
for (let i = 0; i < 10; i++) await Promise.resolve();
assert.deepEqual(landingRequests, [["/marketing/website-ingress/config", "GET"]]);
console.log("Native Website JS: no landing/session/exchange; stable retry token; confirmed form event; native body/result/failure preserved.");
