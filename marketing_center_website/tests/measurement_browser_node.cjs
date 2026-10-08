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
    ["measurement_context.esm.js", "eligibleMeasurementConfig"],
    [
      "measurement.esm.js",
      "measurementUrl,referrerOrigin,annotateWebsiteActions,startMeasurement",
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
  for (const authorization of [REFUSED, INFORMATIONAL, {...REFUSED, granted: true}]) {
    for (const nativeCookie of [
      "",
      '{"optional":true}',
      '{"optional":false}',
      "{bad",
      '"true"',
      "null",
      "[]",
    ]) {
      const f = fixture({authorization, nativeCookie});
      await flush();
      f.document.emit("marketing_center:action-confirmed", {
        kind: "form_submission",
        event_id: REF,
      });
      assert.equal(
        f.events("generate_lead").length,
        1,
        "confirmed capture does not depend on cookie choice"
      );
      f.document.emit("marketing_center:consent-changed", {
        ...REFUSED,
        confirmed: true,
      });
      f.document.emit("marketing_center:action-confirmed", {
        kind: "whatsapp_handoff",
        event_id: SECOND_REF,
      });
      assert.equal(f.events("whatsapp_handoff").length, 1);
      assert.equal(f.scripts.length, 0, "only Website loads Google");
      assert.equal(f.events("page_view").length, 0, "only Website emits pageviews");
      assert.equal(f.commands().filter((row) => row[0] !== "event").length, 0);
      assert.equal(f.window["ga-disable-G-TEST1234"], undefined);
      assert.equal(f.cookieWrites.length, 0);
      assert.equal(f.consentReads(), 0, "Google adapter has no extra consent request");
    }
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
    const f = fixture(options);
    await flush();
    f.document.emit("marketing_center:action-confirmed", {
      kind: "form_submission",
      event_id: REF,
    });
    assert.equal(
      f.events("generate_lead").length,
      0,
      "private/stale/editor config cannot emit business events"
    );
    assert.equal(f.scripts.length, 0);
    assert.equal(f.cookieWrites.length, 0);
  }
  const f = fixture();
  await flush();
  f.document.body.className = "editor_enable";
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: REF,
  });
  assert.equal(f.events("generate_lead").length, 0);
}

async function testGoogleEventsAndPrivacy() {
  const f = fixture({cookie: "_ga=123; _ga_TEST1234=456; session_id=opaque"});
  await flush();
  assert.equal(
    f.commands().length,
    0,
    "visiting a page is never a lead or adapter pageview"
  );
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
  for (const detail of [
    {kind: "toString", event_id: REF},
    {kind: "form_submission", event_id: "invalid"},
  ])
    f.document.emit("marketing_center:action-confirmed", detail);
  f.document.emit("submit", {event_id: REF});
  assert.equal(
    f.commands().length,
    0,
    "ordinary submit or invalid event cannot fabricate a lead"
  );
  const event = {kind: "form_submission", event_id: REF, email: "private@example.com"};
  f.document.emit("marketing_center:action-confirmed", event);
  f.document.emit("marketing_center:action-confirmed", event);
  assert.equal(f.events("generate_lead").length, 1, "confirmed UUID deduplicated");
  f.context.startMeasurement(f.config);
  f.document.emit("marketing_center:action-confirmed", {
    kind: "whatsapp_handoff",
    event_id: SECOND_REF,
  });
  assert.equal(
    f.events("whatsapp_handoff").length,
    1,
    "repeat boot does not duplicate listeners"
  );
  let deferred;
  f.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: "10000000-0000-4000-8000-000000000003",
    waitUntil: (promise) => {
      deferred = promise;
    },
  });
  const payload = f.events("generate_lead").at(-1)[2];
  assert.equal(payload.event_timeout, 600);
  payload.event_callback();
  await deferred;
  assert.equal(JSON.stringify(f.commands()).includes("private@example.com"), false);
  assert.equal(
    f.cookieWrites.length,
    0,
    "existing cookies remain owned by their native providers"
  );
  assert.equal(f.scripts.length, 0);
  assert.equal(f.events("page_view").length, 0);
  assert.ok(f.commands().every((row) => row[0] === "event"));
  const native = fixture();
  let nativeCalls = 0;
  native.window.gtag = function () {
    nativeCalls++;
  };
  native.document.emit("marketing_center:action-confirmed", {
    kind: "form_submission",
    event_id: REF,
  });
  assert.equal(nativeCalls, 1, "adapter uses the native gtag function");
  assert.equal(
    native.commands().length,
    0,
    "no competing queue when native gtag exists"
  );
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
    "Marketing Website: native Google ownership, independent confirmed events, editor/context guards and exact action annotation passed."
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
