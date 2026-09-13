// Deterministic tests of the actual browser consent module without external APIs.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../static/src/js/consent.esm.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/^export /gm, "") + "\nglobalThis.api = {loadConsent, submitConsent};";
const ready = {available: true, granted: true, config_revision: 1, policy_version: "test", notice_version: "test"};
const answer = (value) => ({ok: true, json: async () => value});
const tick = () => new Promise((resolve) => setImmediate(resolve));
function tab(fetch, bus = [], cookies = {optional: true}) {
    const events = [];
    const deleted = [];
    let widget;
    class Channel {
        constructor() { this.listeners = []; bus.push(this); }
        addEventListener(_name, callback) { this.listeners.push(callback); }
        postMessage(data) { for (const other of bus) { if (other !== this) { for (const cb of other.listeners) { cb({data}); } } } }
    }
    const context = vm.createContext({
        console, URL, Promise,
        CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
        publicWidget: {registry: {cookies_bar: {include(value) { widget = value; }}}},
        eligibleLandingPath: () => false,
        deleteCookie: (name) => deleted.push(name),
        setCookie(_name, value) { cookies.optional = JSON.parse(value).optional; },
        document: {body: {classList: {contains: () => false}}, addEventListener() {}, dispatchEvent(event) { events.push(event); }},
        window: {fetch, BroadcastChannel: Channel, sessionStorage: {removeItem() {}}, location: {pathname: "/", reload() {}}, addEventListener() {}},
    });
    vm.runInContext(source, context);
    return {api: context.api, events, widget, cookies, deleted};
}

// A config response captured before withdrawal must not reactivate consumers.
{
    let resolveGet;
    const page = tab((path) => path.endsWith("/config") ? new Promise((resolve) => { resolveGet = resolve; }) : Promise.resolve(answer({accepted: true, granted: false})));
    const old = page.api.loadConsent();
    await page.api.submitConsent(false);
    assert.ok(["odoo_utm_campaign", "odoo_utm_source", "odoo_utm_medium"].every((name) => page.deleted.includes(name)));
    resolveGet(answer(ready));
    await old;
    assert.equal(page.events.some((event) => event.detail.granted === true), false);
}

// The withdrawal POST waits for an already-sent grant, then revokes its cookie.
{
    let resolveGrant;
    let calls = [];
    let serverGranted = false;
    const page = tab((path, options) => {
        if (path.endsWith("/config")) return Promise.resolve(answer({...ready, granted: false}));
        const choice = JSON.parse(options.body).granted;
        calls.push(choice);
        if (choice) return new Promise((resolve) => { resolveGrant = () => { serverGranted = true; resolve(answer({accepted: true, granted: true})); }; });
        serverGranted = false;
        return Promise.resolve(answer({accepted: true, granted: false}));
    });
    await page.api.loadConsent();
    const grant = page.api.submitConsent(true);
    await tick();
    const withdrawal = page.api.submitConsent(false);
    await tick();
    assert.deepEqual(calls, [true]);
    assert.equal(page.cookies.optional, false);
    resolveGrant();
    await Promise.all([grant, withdrawal]);
    assert.deepEqual(calls, [true, false]);
    assert.equal(serverGranted, false);
    assert.equal(page.events.some((event) => event.detail.granted === true), false);
}

// A signal across tabs can only disable; authoritative GET cannot race it on.
{
    const bus = [], cookies = {optional: true};
    const fetch = (path) => Promise.resolve(answer(path.endsWith("/config") ? {...ready, granted: cookies.optional} : {accepted: true, granted: false}));
    const first = tab(fetch, bus, cookies), second = tab(fetch, bus, cookies);
    await Promise.all([first.api.loadConsent(), second.api.loadConsent()]);
    second.events.length = 0;
    await first.api.submitConsent(false);
    await tick();
    assert.ok(second.events.length > 0);
    assert.equal(second.events.some((event) => event.detail.granted === true), false);
    assert.equal(cookies.optional, false);
}

// A nested icon has the same native meaning as its containing accept button.
{
    const page = tab((path) => Promise.resolve(answer(path.endsWith("/config") ? {...ready, granted: false} : {accepted: true, granted: true})));
    await page.api.loadConsent();
    let nativeTarget;
    page.widget._onAcceptClick.call({_super(event) { nativeTarget = event.target.id; }}, {target: {id: "icon"}, currentTarget: {id: "cookies-consent-all"}});
    assert.equal(nativeTarget, "cookies-consent-all");
    await tick();
}
// The server's temporary mode is independent of an absent or refused choice.
{
    let temporary = true;
    const mode = () => ({...ready, granted: false, tracking_test_mode: temporary, capture_allowed: temporary});
    const page = tab((path) => Promise.resolve(answer(path.endsWith("/config")
        ? mode() : {...mode(), accepted: true})), [], {optional: false});
    const initial = await page.api.loadConsent();
    assert.equal(initial.granted, false);
    assert.equal(initial.tracking_test_mode, true);
    assert.equal(page.deleted.length, 0);
    await page.api.submitConsent(false);
    assert.equal(page.cookies.optional, false);
    assert.equal(page.deleted.length, 0);
    assert.equal(page.events.some((event) => event.detail.granted === true), false);
    assert.equal(page.events.filter((event) => event.type.endsWith("consent-changed"))
        .every((event) => event.detail.tracking_test_mode === true), true);
    temporary = false;
    const restored = await page.api.loadConsent(true);
    assert.equal(restored.tracking_test_mode, false);
    assert.equal(restored.capture_allowed, false);
    assert.ok(page.deleted.includes("odoo_utm_source"));
}

// Cross-tab refusal preserves test attribution but cannot grant real consent.
{
    const bus = [], cookies = {optional: false};
    const fetch = (path) => Promise.resolve(answer({
        ...ready, granted: false, tracking_test_mode: true, capture_allowed: true,
        ...(!path.endsWith("/config") ? {accepted: true} : {}),
    }));
    const first = tab(fetch, bus, cookies), second = tab(fetch, bus, cookies);
    await Promise.all([first.api.loadConsent(), second.api.loadConsent()]);
    second.events.length = 0;
    await first.api.submitConsent(false);
    await tick();
    assert.equal(second.deleted.length, 0);
    assert.equal(second.events.some((event) => event.detail.granted === true), false);
    assert.equal(second.events.every((event) => event.detail.tracking_test_mode === true), true);
}
console.log("Consent race tests: 6 scenarios passed, including temporary mode, refusal and restoration.");
