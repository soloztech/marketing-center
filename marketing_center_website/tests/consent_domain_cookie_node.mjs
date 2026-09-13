// Exercise the shipped cleanup against separate host and Domain cookie scopes.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../static/src/js/consent.esm.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "").replace(/^export /gm, "") + "\nglobalThis.api={submitConsent};";
const names = ["odoo_utm_campaign", "odoo_utm_source", "odoo_utm_medium"];
const hostname = "soloz.com.br";
const jar = new Map();
for (const name of names) {
    jar.set(`${name}|host|/`, "host value");
    jar.set(`${name}|domain:${hostname}|/`, "domain value");
}
jar.set("session_id|host|/", "required session");
jar.set("website_cookies_bar|host|/", "native choice");
jar.set("odoo_utm_campaign|domain:other.example|/", "unrelated domain");
const assignments = [];
const document = {
    body: {classList: {contains: () => false}}, addEventListener() {}, dispatchEvent() {},
    set cookie(raw) {
        assignments.push(raw);
        const [pair, ...parts] = raw.split(";").map((part) => part.trim());
        const name = pair.split("=", 1)[0];
        const attrs = Object.fromEntries(parts.map((part) => {
            const equal = part.indexOf("=");
            return equal < 0 ? [part.toLowerCase(), ""] : [part.slice(0, equal).toLowerCase(), part.slice(equal + 1)];
        }));
        assert.equal(attrs.path, "/");
        assert.equal(attrs["max-age"], "0");
        assert.ok([hostname, "." + hostname].includes(attrs.domain));
        jar.delete(`${name}|domain:${attrs.domain.replace(/^\./, "")}|/`);
    },
};
const context = vm.createContext({
    Promise, URL, document,
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    eligibleLandingPath: () => false,
    publicWidget: {registry: {cookies_bar: {include() {}}}},
    deleteCookie(name) {jar.delete(`${name}|host|/`);},
    setCookie() {},
    window: {
        location: {hostname, pathname: "/"}, sessionStorage: {}, addEventListener() {},
        localStorage: {setItem() {}, removeItem() {}},
        fetch: async () => ({ok: true, json: async () => ({accepted: true, granted: false})}),
    },
});
vm.runInContext(source, context);
await context.api.submitConsent(false);
for (const name of names) {
    assert.equal(jar.has(`${name}|host|/`), false);
    assert.equal(jar.has(`${name}|domain:${hostname}|/`), false);
}
assert.equal(assignments.length, 6);
assert.equal(jar.get("session_id|host|/"), "required session");
assert.equal(jar.get("website_cookies_bar|host|/"), "native choice");
assert.equal(jar.get("odoo_utm_campaign|domain:other.example|/"), "unrelated domain");
console.log("Consent cookie scopes: all three host/Domain UTMs erased; required cookies and other domains preserved.");
