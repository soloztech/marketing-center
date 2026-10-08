// Run the shipped Odoo modules in an isolated DOM/Google boundary. No network,
// lead creation or provider event occurs during these regression scenarios.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto, createHash} = require("node:crypto");

const REF = "10000000-0000-4000-8000-000000000001";
const SECOND_REF = "10000000-0000-4000-8000-000000000002";
const INFORMATIONAL = {
  available: true,
  granted: false,
  informational_notice: true,
  capture_allowed: true,
};
const REFUSED = {
  available: true,
  granted: false,
  informational_notice: false,
  capture_allowed: false,
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const flush = async () => {
  for (let index = 0; index < 8; index++) await Promise.resolve();
};

class Events {
  constructor() {
    this.listeners = new Map();
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  emit(name, detail) {
    for (const callback of this.listeners.get(name) || []) callback({detail});
  }
}
class Element extends Events {
  constructor(tagName) {
    super();
    this.tagName = tagName.toUpperCase();
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.classList = {
      contains: (value) => this.className.split(" ").includes(value),
      add: (value) => {
        if (!this.classList.contains(value)) this.className += " " + value;
      },
      toggle: (value, enabled) => {
        this.className = this.className
          .split(" ")
          .filter((item) => item !== value)
          .join(" ");
        if (enabled) this.classList.add(value);
      },
    };
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  hasAttribute(name) {
    return this.attributes.has(name);
  }
  removeAttribute(name) {
    this.attributes.delete(name);
  }
  appendChild(child) {
    this.children.push(child);
    child.parentElement = this;
    return child;
  }
  remove() {
    this.parentElement.children = this.parentElement.children.filter(
      (child) => child !== this
    );
  }
  contains(value) {
    return this === value || this.children.some((child) => child.contains(value));
  }
  closest(selector) {
    return selector.startsWith(".") && this.classList.contains(selector.slice(1))
      ? this
      : this.parentElement?.closest(selector);
  }
  querySelector(selector) {
    return (
      this.children.find(
        (child) =>
          selector.startsWith(".") && child.classList.contains(selector.slice(1))
      ) ||
      this.children.map((child) => child.querySelector(selector)).find(Boolean) ||
      null
    );
  }
  set innerHTML(_value) {
    throw new Error("Settings text must never be parsed as HTML");
  }
}

function fixture(options = {}) {
  const document = new Events();
  const window = new Events();
  const config = new Element("div");
  config.dataset = {
    websiteId: "7",
    path: "/contactus",
    ga4: "G-TEST1234",
    form: "",
    whatsapp: "",
    ...options.dataset,
  };
  const bar = new Element("div");
  const controls = [new Element("a"), new Element("a")];
  const cookieWrites = [];
  let cookie = options.cookie || "";
  Object.defineProperty(document, "cookie", {
    get: () => cookie,
    set: (value) => {
      cookieWrites.push(value);
    },
  });
  const scripts = [];
  const body = new Element("body");
  if (options.editor) body.className = "editor_enable";
  Object.assign(document, {
    readyState: "complete",
    title: "Public page",
    referrer: "https://search.example/results?q=private@example.com",
    body,
    head: {appendChild: (script) => scripts.push(script)},
    getElementById: (id) =>
      id === "marketing_measurement_config"
        ? options.noConfig
          ? null
          : config
        : id === "website_cookies_bar"
        ? options.noBar
          ? null
          : bar
        : id === "cookies-consent-essential" && !options.missingEssential
        ? controls[0]
        : null,
    querySelectorAll: (selector) =>
      selector === "[data-marketing-consent-revoke]"
        ? []
        : selector === "a[href]"
        ? options.links || []
        : options.forms || [],
    createElement: (tag) => new Element(tag),
  });
  window.location = new URL(
    options.url ||
      "https://sales.example.test/contactus?utm_source=google&email=private%40example.com&token=secret#private"
  );
  window.TextEncoder = TextEncoder;
  window.crypto = options.noCrypto ? null : webcrypto;
  let authorization = options.authorization || REFUSED;
  let consentReads = 0;
  const context = vm.createContext({
    document,
    window,
    cookieUtils: {
      getCookie: () =>
        options.nativeCookie === undefined
          ? '{"required":true,"optional":true}'
          : options.nativeCookie,
      isAllowedCookie: () => options.nativeAllowed !== false,
    },
    URL,
    Set,
    WeakSet,
    Date,
    Uint8Array,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    loadConsent: async () => {
      consentReads++;
      return authorization;
    },
    eligibleLandingPath: (pathname) =>
      !/^\/(?:web|website|my|auth|portal|marketing)(?:\/|$)/.test(pathname),
    validOpaqueUuid: (value) => UUID.test(value),
  });
  for (const [filename, exports] of [
    [
      "measurement_context.esm.js",
      "eligibleMeasurementConfig,refreshMeasurementConsent",
    ],
    [
      "measurement.esm.js",
      "measurementUrl,referrerOrigin,measurementAllowed,annotateWebsiteActions,startMeasurement",
    ],
  ]) {
    const source = fs
      .readFileSync(path.join(__dirname, "../static/src/js", filename), "utf8")
      .replace(/^import[\s\S]*?;\s*$/gm, "")
      .replace(/^export /gm, "");
    vm.runInContext(
      `(() => {${source}\nObject.assign(globalThis, {${exports}});})();`,
      context
    );
  }
  return {
    document,
    window,
    context,
    config,
    bar,
    controls,
    scripts,
    cookieWrites,
    setAuthorization: (value) => {
      authorization = value;
    },
    setCookie: (value) => {
      cookie = value;
    },
    consentReads: () => consentReads,
    commands: () => (window.dataLayer || []).map((row) => Array.from(row)),
    events: (name) =>
      (window.dataLayer || [])
        .map((row) => Array.from(row))
        .filter((row) => row[0] === "event" && row[1] === name),
  };
}

async function testNativeMatrix() {
  for (const policy of [INFORMATIONAL, {...REFUSED, granted: true}]) {
    for (const nativeCookie of [
      "",
      '{"optional":true}',
      '{"optional":false}',
      "{bad",
      '"true"',
      "null",
      "[]",
      '{"optional":"true"}',
    ]) {
      const f = fixture({authorization: policy, nativeCookie});
      await flush();
      assert.equal(
        f.scripts.length,
        nativeCookie === '{"optional":true}' ? 1 : 0,
        JSON.stringify([policy, nativeCookie])
      );
      f.document.emit("marketing_center:consent-changed", {...policy, confirmed: true});
      assert.equal(
        f.window["ga-disable-G-TEST1234"],
        nativeCookie !== '{"optional":true}'
      );
    }
  }
  for (const options of [{missingEssential: true}, {nativeAllowed: false}]) {
    const f = fixture({...options, authorization: INFORMATIONAL});
    await flush();
    assert.equal(f.scripts.length, 0);
  }
  const options = {authorization: INFORMATIONAL, nativeCookie: ""};
  const f = fixture(options);
  await flush();
  assert.equal(f.scripts.length, 0);
  options.nativeCookie = '{"optional":true}';
  f.document.emit("marketing_center:native-cookie-choice", {granted: true});
  await flush();
  assert.equal(f.scripts.length, 1);
  options.nativeCookie = '{"optional":false}';
  f.document.emit("marketing_center:native-cookie-choice", {granted: false});
  await flush();
  assert.equal(f.window["ga-disable-G-TEST1234"], true);
  options.nativeCookie = "";
  f.window.emit("focus");
  await flush();
  assert.equal(
    f.window["ga-disable-G-TEST1234"],
    true,
    "refuse -> revoke -> reload/no-choice stays denied"
  );
  for (const lifecycle of ["pageshow", "visibilitychange"]) {
    options.nativeCookie = '{"optional":true}';
    f.document.emit("marketing_center:native-cookie-choice", {granted: true});
    await flush();
    assert.equal(f.window["ga-disable-G-TEST1234"], false);
    options.nativeCookie = "";
    if (lifecycle === "pageshow") {
      for (const callback of f.window.listeners.get("pageshow"))
        callback({persisted: true});
    } else {
      f.document.visibilityState = "visible";
      f.document.emit("visibilitychange");
    }
    assert.equal(
      f.window["ga-disable-G-TEST1234"],
      true,
      "return immediately denies stale GA grant"
    );
    await flush();
    assert.equal(
      f.window["ga-disable-G-TEST1234"],
      true,
      "revoked native choice stays denied on " + lifecycle
    );
  }
}

async function testContextBoundaries() {
  for (const options of [
    {noConfig: true},
    {editor: true},
    {dataset: {websiteId: ""}},
    {dataset: {path: "/other"}},
    {url: "http://sales.example.test/contactus"},
    {url: "https://sales.example.test/web/login", dataset: {path: "/web/login"}},
  ]) {
    const f = fixture({...options, authorization: INFORMATIONAL});
    await flush();
    assert.equal(f.scripts.length, 0);
    assert.equal(
      f.consentReads(),
      0,
      "private/stale/editor markup cannot bootstrap measurement"
    );
    assert.equal(f.cookieWrites.length, 0);
    assert.equal(f.controls.length, 2, "capture exclusion keeps both stock controls");
  }
  const denied = fixture({
    authorization: {available: false, granted: false, capture_allowed: false},
  });
  await flush();
  assert.equal(denied.scripts.length, 0);
  assert.equal(denied.controls.length, 2);
  const f = fixture({authorization: INFORMATIONAL});
  await flush();
  f.document.body.className = "editor_enable";
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: REF,
  });
  assert.equal(f.events("generate_lead").length, 0);
  f.window.emit("focus");
  await flush();
  assert.equal(f.window["ga-disable-G-TEST1234"], true);
}

async function testGoogleEventsAndPrivacy() {
  const f = fixture({cookie: "_ga=123; _ga_TEST1234=456; session_id=opaque"});
  await flush();
  assert.equal(f.scripts.length, 0, "Google stays unloaded before authorization");
  assert.equal(
    f.context.measurementUrl(f.window.location.href, "/contactus"),
    "https://sales.example.test/contactus?utm_source=google"
  );
  assert.equal(
    f.context.measurementUrl(
      "https://example.test/?utm_term=user%40example.com&gclid=valid-id&utm_source=first&utm_source=second&fbclid=bad%00id",
      "/"
    ),
    "https://example.test/?gclid=valid-id&utm_source=first"
  );
  assert.equal(
    f.context.referrerOrigin(f.document.referrer),
    "https://search.example/"
  );
  assert.equal(f.context.referrerOrigin("javascript:alert(1)"), "");
  assert.equal(
    f.context.measurementAllowed({informational_notice: true, capture_allowed: false}),
    false
  );
  assert.equal(
    f.context.measurementAllowed({informational_notice: "true", capture_allowed: true}),
    false
  );
  assert.equal(f.context.measurementAllowed(INFORMATIONAL, false), false);
  assert.ok(
    f.cookieWrites.some((value) => value.includes("Domain=.example.test")),
    "GA parent-domain cookies are removed without a hardcoded deployment domain"
  );
  assert.ok(
    f.cookieWrites.every(
      (value) => /^_ga(?:_|=)/.test(value) && !value.includes("soloz")
    )
  );
  f.document.emit("marketing_center:consent-changed", {
    granted: true,
    confirmed: false,
  });
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: REF,
  });
  assert.equal(f.scripts.length, 0);
  assert.equal(f.events("generate_lead").length, 0);
  f.document.emit("marketing_center:consent-ready", {granted: true});
  assert.equal(f.scripts.length, 1);
  assert.equal(f.events("page_view").length, 1);
  assert.equal(f.events("generate_lead").length, 0, "visiting a page is never a lead");
  f.document.emit("marketing_center:action-confirmed", {
    kind: "toString",
    event_id: REF,
  });
  const event = {kind: "form_submission", event_id: REF, email: "private@example.com"};
  f.document.emit("marketing_center:action-confirmed", event);
  f.document.emit("marketing_center:action-confirmed", event);
  assert.equal(f.events("generate_lead").length, 1, "confirmed UUID deduplicated");
  f.document.emit("marketing_center:action-confirmed", {
    kind: "whatsapp_handoff",
    event_id: SECOND_REF,
  });
  assert.equal(f.events("whatsapp_handoff").length, 1);
  let deferred;
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: "10000000-0000-4000-8000-000000000003",
    waitUntil: (promise) => {
      deferred = promise;
    },
  });
  assert.ok(deferred, "confirmed native redirect receives bounded delivery promise");
  const payload = f.events("generate_lead").at(-1)[2];
  assert.equal(payload.event_timeout, 600);
  payload.event_callback();
  await deferred;
  assert.equal(
    JSON.stringify(f.commands()).includes("private@example.com"),
    false,
    "submitted PII and referrer query absent from Google"
  );
  for (const command of f.commands().filter((row) => row[0] === "consent")) {
    assert.equal(command[2].ad_storage, "denied");
    assert.equal(command[2].ad_user_data, "denied");
    assert.equal(command[2].ad_personalization, "denied");
  }
  f.document.emit("marketing_center:consent-changed", {
    granted: false,
    confirmed: false,
  });
  assert.equal(f.window["ga-disable-G-TEST1234"], true);
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: "10000000-0000-4000-8000-000000000004",
  });
  assert.equal(f.events("generate_lead").length, 2);
  f.document.emit("marketing_center:consent-changed", {granted: true, confirmed: true});
  f.context.startMeasurement(f.config);
  assert.equal(f.scripts.length, 1);
  assert.equal(f.events("page_view").length, 1);
  const informational = fixture({authorization: INFORMATIONAL});
  await flush();
  assert.equal(informational.scripts.length, 1);
  assert.equal(
    informational.cookieWrites.length,
    0,
    "operator informational capture never grants visitor consent"
  );
  informational.document.emit("marketing_center:consent-changed", {
    ...INFORMATIONAL,
    confirmed: true,
  });
  assert.equal(informational.window["ga-disable-G-TEST1234"], false);
}

function link(href, marker) {
  const value = new Element("a");
  value.setAttribute("href", href);
  if (marker) value.setAttribute("data-marketing-whatsapp-action", marker);
  return value;
}
async function testActionAnnotations() {
  const phone = "15551234567";
  const message = "Request a quote";
  const fingerprint = createHash("sha256")
    .update(phone + "\n" + message)
    .digest("hex");
  const href = "https://wa.me/" + phone + "?text=" + encodeURIComponent(message);
  const matching = link(href);
  const special = link("https://wa.me/" + phone + "?text=Please%20send%20datasheet");
  const existing = link(href, SECOND_REF);
  const nativeTracked = link("/r/track-code");
  const forms = [new Element("form")];
  const links = [
    matching,
    special,
    existing,
    nativeTracked,
    link(href + "&text=duplicate"),
    link(href + "#fragment"),
    link(
      "https://wa.me.evil.example/" + phone + "?text=" + encodeURIComponent(message)
    ),
    link("https://user:pass@wa.me/" + phone + "?text=" + encodeURIComponent(message)),
    link("https://api.whatsapp.com/send/?phone=" + phone),
  ];
  const f = fixture({
    forms,
    links,
    dataset: {ga4: "", form: REF, whatsapp: REF, whatsappLinkHash: fingerprint},
  });
  await f.context.annotateWebsiteActions(f.config);
  assert.equal(
    forms[0].getAttribute("data-marketing-form-action"),
    REF,
    "single native CRM form can be annotated without Google"
  );
  assert.equal(matching.getAttribute("data-marketing-whatsapp-action"), REF);
  assert.equal(
    matching.getAttribute("href"),
    href,
    "ordinary CTA destination is unchanged"
  );
  assert.equal(
    special.getAttribute("data-marketing-whatsapp-action"),
    null,
    "special editorial message is never replaced by server fixed message"
  );
  assert.equal(
    existing.getAttribute("data-marketing-whatsapp-action"),
    SECOND_REF,
    "explicit configured markers are preserved"
  );
  for (const value of links.slice(3))
    assert.equal(value.getAttribute("data-marketing-whatsapp-action"), null);
  assert.equal(f.scripts.length, 0);
  const ambiguous = [new Element("form"), new Element("form")];
  const multiple = fixture({forms: ambiguous, dataset: {form: REF}});
  await multiple.context.annotateWebsiteActions(multiple.config);
  assert.ok(
    ambiguous.every((form) => !form.hasAttribute("data-marketing-form-action")),
    "ambiguous forms require explicit markers"
  );
  const preconfigured = new Element("form");
  preconfigured.setAttribute("data-marketing-form-action", SECOND_REF);
  const explicit = fixture({forms: [preconfigured], dataset: {form: REF}});
  await explicit.context.annotateWebsiteActions(explicit.config);
  assert.equal(preconfigured.getAttribute("data-marketing-form-action"), SECOND_REF);
  const noCryptoLink = link(href);
  const noCrypto = fixture({
    noCrypto: true,
    links: [noCryptoLink],
    dataset: {whatsapp: REF, whatsappLinkHash: fingerprint},
  });
  await noCrypto.context.annotateWebsiteActions(noCrypto.config);
  assert.equal(noCryptoLink.getAttribute("data-marketing-whatsapp-action"), null);
  assert.equal(
    noCryptoLink.getAttribute("href"),
    href,
    "missing crypto leaves ordinary link usable"
  );
}

(async () => {
  await testNativeMatrix();
  await testContextBoundaries();
  await testGoogleEventsAndPrivacy();
  await testActionAnnotations();
  console.log(
    "Marketing Website: native optional permission matrix, editor/context guards, GA privacy/confirmed events and exact action annotation passed."
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
