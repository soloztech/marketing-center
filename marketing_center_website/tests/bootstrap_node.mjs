// Exercise the shared shipped dependency and both readers in one browser realm.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {installBootstrap} from "./bootstrap_fixture_node.mjs";

const read = (name) =>
  fs
    .readFileSync(new URL(`../static/src/js/${name}.esm.js`, import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/^export /gm, "");
const reply = (value) => ({ok: true, json: async () => value});
const envelope = {
  schema_version: 1,
  ingress: {
    enabled: true,
    capture_mode: "legacy",
    informational_notice: false,
    ingest_path: "/marketing/web-ingress/11111111-1111-4111-8111-111111111111",
    public_key: "a".repeat(32),
    config_revision: 1,
  },
  consent: {available: true, granted: true, capture_allowed: true, config_revision: 1},
};
function page(fetch, hint = null, pathname = "/blog") {
  const handlers = new Map();
  const saved = [];
  const context = vm.createContext({
    Promise,
    Date,
    URL,
    JSON,
    CustomEvent: class {
      constructor(type, {detail}) {
        this.type = type;
        this.detail = detail;
      }
    },
    document: {
      referrer: "",
      body: {classList: {contains: () => false}},
      getElementById: () => (hint === null ? null : {dataset: {captureMode: hint}}),
      addEventListener(name, fn) {
        handlers.set(name, [...(handlers.get(name) || []), fn]);
      },
      dispatchEvent(event) {
        for (const fn of handlers.get(event.type) || []) fn(event);
      },
    },
    window: {
      fetch,
      TextEncoder,
      location: {pathname},
      addEventListener() {
        /* No cross-tab browser in this fixture. */
      },
      localStorage: {
        setItem() {
          /* No persistent storage. */
        },
        removeItem() {
          /* No persistent storage. */
        },
      },
      sessionStorage: {
        getItem: () => "",
        setItem: (...args) => saved.push(args),
        removeItem() {
          /* Attribution cleanup is tested in the consent runner. */
        },
      },
    },
    publicWidget: {
      registry: {
        cookies_bar: {
          include() {
            /* The stock widget is tested in the consent runner. */
          },
        },
      },
    },
    eligibleLandingPath: () => false,
    deleteCookie() {
      /* Cookie cleanup has its own runner. */
    },
    setCookie() {
      /* Cookie cleanup has its own runner. */
    },
    opaqueUuid: () => "22222222-2222-4222-8222-222222222222",
    buildLandingPayload: () => ({}),
  });
  installBootstrap(context);
  const landing = vm.runInContext(
    `(() => {${read(
      "landing_bootstrap"
    )}\nreturn {loadConfig, captureLandingEntry};})()`,
    context
  );
  const consent = vm.runInContext(
    `(() => {${read("consent")}\nreturn {loadConsent, submitConsent};})()`,
    context
  );
  const helpers = vm.runInContext(
    `(() => {${read("landing_capture")}\nreturn {eligibleLandingPath};})()`,
    context
  );
  context.eligibleLandingPath = helpers.eligibleLandingPath;
  return {context, saved, ...landing, ...consent};
}
{
  const calls = [];
  const p = page(async (path) => {
    calls.push(path);
    return reply(envelope);
  });
  const [ingress, consent] = await Promise.all([p.loadConfig(), p.loadConsent()]);
  assert.equal(ingress.capture_mode, "legacy");
  assert.equal(consent.granted, true);
  assert.deepEqual(calls, ["/marketing/website/bootstrap-config"]);
  await Promise.all([p.loadConfig(), p.loadConsent()]);
  assert.equal(calls.length, 1, "same generation shares the resolved Promise");
}
{
  let resolve;
  const p = page(
    () =>
      new Promise((done) => {
        resolve = done;
      })
  );
  const old = Promise.all([p.loadConfig(), p.loadConsent()]);
  p.context.document.dispatchEvent({
    type: "marketing_center:consent-changed",
    detail: {granted: false, confirmed: false},
  });
  resolve(reply(envelope));
  const [config, consent] = await old;
  assert.equal(config, null);
  assert.equal(consent.granted, false);
  assert.equal(p.saved.length, 0);
  p.context.window.fetch = async () => reply(envelope); // GET can precede revocation POST.
  assert.equal((await p.loadConfig()).enabled, false);
  assert.equal((await p.loadConsent()).granted, false);
}
for (const hint of ["native", null, "unknown"]) {
  const calls = [];
  const p = page(async (path) => {
    calls.push(path);
    return reply(envelope);
  }, hint);
  await p.captureLandingEntry();
  assert.equal(calls.length, hint === "native" ? 0 : 2);
  if (hint === "native") assert.equal(p.saved.length, 0);
}
for (const pathname of ["/blog", "/jobs", "/contactus-thank-you"]) {
  const calls = [];
  const p = page(
    async (path) => {
      calls.push(path);
      return reply(envelope);
    },
    null,
    pathname
  );
  assert.equal(await p.captureLandingEntry(), true);
  assert.deepEqual(calls, [
    "/marketing/website/bootstrap-config",
    envelope.ingress.ingest_path,
  ]);
}
{
  const p = page(async () => {
    throw Error("offline");
  }, "native");
  assert.equal(await p.loadConfig(), null);
  assert.equal((await p.loadConsent()).granted, false);
  assert.equal(
    p.saved.length,
    0,
    "unavailable configuration creates no capture storage"
  );
}
console.log(
  "Shared bootstrap: one fetch; stale/refused generation denied; tri-state native skip; no-hint blog/jobs/thank-you compatibility; unavailable fail-closed."
);
