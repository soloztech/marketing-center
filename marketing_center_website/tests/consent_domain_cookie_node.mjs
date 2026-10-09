// Exercise the shipped cleanup against separate host and Domain cookie scopes.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {installBootstrap} from "./bootstrap_fixture_node.mjs";

const source =
  fs
    .readFileSync(new URL("../static/src/js/consent.esm.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/^export /gm, "") + "\nglobalThis.api={submitConsent};";
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
  body: {classList: {contains: () => false}},
  addEventListener() {
    /* Browser API stub; no side effect needed in this fixture. */
  },
  dispatchEvent() {
    /* Browser API stub; no side effect needed in this fixture. */
  },
  set cookie(raw) {
    assignments.push(raw);
    const [pair, ...parts] = raw.split(";").map((part) => part.trim());
    const name = pair.split("=", 1)[0];
    const attrs = Object.fromEntries(
      parts.map((part) => {
        const equal = part.indexOf("=");
        return equal < 0
          ? [part.toLowerCase(), ""]
          : [part.slice(0, equal).toLowerCase(), part.slice(equal + 1)];
      })
    );
    assert.equal(attrs.path, "/");
    assert.equal(attrs["max-age"], "0");
    assert.ok([hostname, "." + hostname].includes(attrs.domain));
    jar.delete(`${name}|domain:${attrs.domain.replace(/^\./, "")}|/`);
  },
};
const context = vm.createContext({
  Promise,
  URL,
  document,
  CustomEvent: class {
    constructor(type, options) {
      this.type = type;
      this.detail = options.detail;
    }
  },
  eligibleLandingPath: () => false,
  publicWidget: {
    registry: {
      cookies_bar: {
        include() {
          /* Browser API stub; no side effect needed in this fixture. */
        },
      },
    },
  },
  deleteCookie(name) {
    jar.delete(`${name}|host|/`);
  },
  setCookie() {
    /* Browser API stub; no side effect needed in this fixture. */
  },
  window: {
    location: {hostname, pathname: "/"},
    sessionStorage: {},
    addEventListener() {
      /* Browser API stub; no side effect needed in this fixture. */
    },
    localStorage: {
      setItem() {
        /* Browser API stub; no side effect needed in this fixture. */
      },
      removeItem() {
        /* Browser API stub; no side effect needed in this fixture. */
      },
    },
    fetch: async (path) => ({
      ok: true,
      json: async () =>
        path.endsWith("bootstrap-config")
          ? {
              available: true,
              granted: false,
              config_revision: 1,
              policy_version: "test",
              notice_version: "test",
            }
          : {accepted: true, granted: false},
    }),
  },
});
installBootstrap(context, "consent");
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
for (const name of names) {
  jar.set(`${name}|host|/`, "retained");
  jar.set(`${name}|domain:${hostname}|/`, "retained");
}
assignments.length = 0;
const unavailable = vm.createContext({
  ...context,
  window: {...context.window, fetch: async () => ({ok: false})},
});
installBootstrap(unavailable, "consent");
vm.runInContext(source, unavailable);
await unavailable.api.submitConsent(false);
assert.equal(assignments.length, 0);
for (const name of names) {
  assert.equal(jar.get(`${name}|host|/`), "retained");
  assert.equal(jar.get(`${name}|domain:${hostname}|/`), "retained");
}
console.log(
  "Consent cookie scopes: all three host/Domain UTMs erased; required cookies and other domains preserved."
);
