// Real shipped code; isolated DOM/network boundary and no messages sent.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const read = (url) => fs.readFileSync(url, "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "").replace(/^export /gm, "");
const origin = "https://example.test";
const action = "10000000-0000-4000-8000-000000000001";
const eventId = "20000000-0000-4000-8000-000000000001";
const reference = "CP-23456789ABCD";
const original = "https://wa.me/5519999999999?text=" + encodeURIComponent("Olá!");
const destinationUrl = new URL(original);
destinationUrl.searchParams.set("text", "Olá!\n\nReferência: " + reference);
const destination = destinationUrl.href;
let editorEnabled = false;
let configPresent = true;
let mode = "success";
let popupBlocked = false;
const listeners = new Map();
const requests = [];
const navigations = [];
const popups = [];
const notifications = [];
const config = {dataset: {
    action, destination: "5519999999999", path: "/new-public-page", websiteId: "1",
}};
const document = {
    visibilityState: "visible",
    body: {classList: {contains(name) {return name === "editor_enable" && editorEnabled;}}},
    getElementById(id) {return id === "marketing_whatsapp_handoff_config" && configPresent ? config : null;},
    addEventListener(name, fn) { listeners.set(name, fn); },
    dispatchEvent(event) { notifications.push(event.detail); },
};
const window = {
    location: {origin, protocol: "https:", pathname: config.dataset.path, assign(url) { navigations.push(url); }},
    AbortController, setTimeout(fn, ms) { return setTimeout(fn, Math.min(ms, 5)); }, clearTimeout,
    async fetch(path, options) {
        requests.push({path, options});
        if (mode === "network") throw Error("Synthetic network failure");
        if (mode === "stall") return {ok: true, status: 202, json: () => new Promise(() => {})};
        return {
            ok: mode === "success" || mode === "hostile", status: mode === "reject" ? 400 : 202,
            json: async () => ({accepted: true, url: mode === "hostile" ? "https://evil.invalid" : destination, reference, event_id: eventId}),
        };
    },
    open() {
        if (popupBlocked) return null;
        const popup = {opener: "parent", closed: false, location: {replace(url) { popup.url = url; }}};
        popups.push(popup);
        return popup;
    },
};
const context = vm.createContext({
    URL, Date, Promise, window, document,
    navigator: {userActivation: {isActive: true}},
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    newActionEventId: () => eventId,
    loadConsent: async () => ({informational_notice: true, capture_allowed: true}),
});
vm.runInContext(read(new URL("../../../marketing_center_website/static/src/js/action_capture.esm.js", import.meta.url)) + "\n" +
    read(new URL("../src/js/whatsapp_handoff.esm.js", import.meta.url)), context);
const settle = () => new Promise((resolve) => setTimeout(resolve, 25));
await settle();
function link(target = "_self", href = original) {
    const attributes = {href, target};
    return {tagName: "A", getAttribute(name) {return attributes[name] || null;}, hasAttribute(name) {return name in attributes;}};
}
function activate(anchor = link(), overrides = {}) {
    const event = {isTrusted: true, button: 0, defaultPrevented: false,
        composedPath: () => [anchor], preventDefault() {this.defaultPrevented = true;}, ...overrides};
    context.onWhatsAppActivation(event);
    return event;
}
const firstLink = link();
assert.equal(activate(firstLink).defaultPrevented, true);
assert.equal(activate(firstLink).defaultPrevented, true, "Rapid repeated activation is suppressed while pending");
await settle();
assert.equal(requests.length, 1);
assert.equal(navigations.at(-1), destination);
assert.deepEqual(JSON.parse(requests[0].options.body), {action_ref: action, event_id: eventId});
assert.equal(requests[0].path, "/marketing/website-whatsapp/claim");
assert.equal(notifications.length, 1);

const editorialText = "Olá! Gostaria de solicitar o Memorial Técnico Estrutural — Soloz Dupla Pro.";
const editorialUrl = new URL(original);
editorialUrl.searchParams.set("text", editorialText);
const beforeEditorial = requests.length;
assert.equal(activate(link("_self", editorialUrl.href)).defaultPrevented, true,
    "An unannotated editorial CTA on the configured number is captured");
await settle();
assert.equal(new URL(navigations.at(-1)).searchParams.get("text"),
    editorialText + "\n\nReferência: " + reference);
assert.deepEqual(JSON.parse(requests[beforeEditorial].options.body), {action_ref: action, event_id: eventId},
    "Editorial text is never submitted to the server");
const beforeOther = requests.length;
assert.equal(activate(link("_self", original.replace("5519999999999", "5519888888888"))).defaultPrevented, false);
assert.equal(requests.length, beforeOther, "Other destinations are left untouched");
assert.equal(context.verifiedTarget({accepted: true, reference, event_id: eventId,
    url: destination.replace("5519999999999", "5519888888888")}, new URL(original)), "",
    "A server response for another number cannot change the selected destination");
assert.equal(context.verifiedTarget({accepted: true, reference: "CP-ZZZZZZZZZZZZ", event_id: eventId,
    url: destination}, new URL(original)), "", "Reference must agree with the fixed server URL");

for (const failure of ["reject", "network", "stall", "hostile"]) {
    mode = failure;
    const before = requests.length;
    activate();
    await settle();
    assert.equal(navigations.at(-1), original, `${failure} preserves original WhatsApp URL`);
    assert.equal(notifications.length, 1, "Failures do not emit confirmations");
    const attempts = requests.slice(before);
    assert.equal(attempts.length, ["network", "stall"].includes(failure) ? 2 : 1);
    assert.equal(new Set(attempts.map((item) => item.options.body)).size, 1, "Retries retain event UUID");
}
mode = "success";
activate(link("_blank"));
assert.equal(popups.at(-1).opener, null);
await settle();
assert.equal(popups.at(-1).url, destination);
popupBlocked = true;
const count = requests.length;
assert.equal(activate(link("_blank")).defaultPrevented, false, "Blocked popup leaves original link usable");
assert.equal(requests.length, count);
popupBlocked = false;

for (const modifier of [{ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {button: 1}, {isTrusted: false}]) {
    assert.equal(activate(link(), modifier).defaultPrevented, false);
}
assert.equal(requests.length, count, "Modifier/middle/programmatic navigation is not captured");
editorEnabled = true;
assert.equal(activate().defaultPrevented, false, "Editor remains untouched");
editorEnabled = false;
configPresent = false;
assert.equal(activate().defaultPrevented, false, "Absent WhatsApp config preserves the original link");
configPresent = true;
config.dataset.path = "/different-page";
assert.equal(activate().defaultPrevented, false, "Config for another page cannot be reused");
config.dataset.path = window.location.pathname;
config.dataset.websiteId = "invalid";
assert.equal(activate().defaultPrevented, false, "Missing Website scope cannot capture");
config.dataset.websiteId = "1";
window.location.protocol = "http:";
assert.equal(activate().defaultPrevented, false, "Insecure navigation remains untouched");
window.location.protocol = "https:";
assert.equal(requests.length, count, "Rejected configurations must not make claim requests");
listeners.get("marketing_center:consent-changed")({detail: {granted: false, capture_allowed: false}});
assert.equal(activate().defaultPrevented, false, "Explicit denied policy bypasses optional capture");
listeners.get("marketing_center:consent-changed")({detail: {granted: true}});
assert.equal(activate(link(), {detail: 0}).defaultPrevented, true, "Keyboard click activates capture");
await settle();
assert.equal(navigations.at(-1), destination);

window.location.pathname = "/contactus-thank-you";
config.dataset.path = window.location.pathname;
assert.equal(document.getElementById("marketing_measurement_config"), null);
const beforeThankYou = requests.length;
assert.equal(activate(link("_self", editorialUrl.href)).defaultPrevented, true,
    "A published thank-you CTA uses its own config without GA/form measurement");
await settle();
assert.deepEqual(JSON.parse(requests[beforeThankYou].options.body), {action_ref: action, event_id: eventId});
assert.equal(new URL(navigations.at(-1)).searchParams.get("text"),
    editorialText + "\n\nReferência: " + reference);
console.log("WhatsApp handoff JS: independent page/Website config, thank-you without GA/forms, editorial and floating CTAs preserved, fixed destination, stable retry UUID, bounded stalled body, fail-open, keyboard, popup, modifier/editor/policy behavior passed.");
