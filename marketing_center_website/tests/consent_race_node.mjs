// Deterministic tests of the actual browser consent module without external APIs.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source =
  fs
    .readFileSync(new URL("../static/src/js/consent.esm.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/^export /gm, "") + "\nglobalThis.api = {loadConsent, submitConsent};";
const ready = {
  available: true,
  granted: true,
  config_revision: 1,
  policy_version: "test",
  notice_version: "test",
};
const answer = (value) => ({ok: true, json: async () => value});
const tick = () => new Promise((resolve) => setImmediate(resolve));
function tab(fetch, bus = [], cookies = {optional: true}, noticeBar = null) {
  const events = [];
  const deleted = [];
  const removedStorage = [];
  const handlers = new Map();
  const cookieWrites = [];
  let widget;
  class Channel {
    constructor() {
      this.listeners = [];
      bus.push(this);
    }
    addEventListener(_name, callback) {
      this.listeners.push(callback);
    }
    postMessage(data) {
      for (const other of bus) {
        if (other !== this) {
          for (const cb of other.listeners) {
            cb({data});
          }
        }
      }
    }
  }
  const context = vm.createContext({
    console,
    URL,
    Promise,
    CustomEvent: class {
      constructor(type, options) {
        this.type = type;
        this.detail = options.detail;
      }
    },
    publicWidget: {
      registry: {
        cookies_bar: {
          include(value) {
            widget = value;
          },
        },
      },
    },
    eligibleLandingPath: () => false,
    deleteCookie: (name) => deleted.push(name),
    setCookie(name, value) {
      cookieWrites.push(name);
      cookies.optional = JSON.parse(value).optional;
    },
    document: {
      body: {classList: {contains: () => false}},
      getElementById: (id) => (id === "website_cookies_bar" ? noticeBar : null),
      addEventListener(name, fn) {
        handlers.set(name, fn);
      },
      dispatchEvent(event) {
        events.push(event);
      },
    },
    window: {
      fetch,
      BroadcastChannel: Channel,
      sessionStorage: {
        "marketing_center.website.v1.test": "preserve",
        removeItem(key) {
          removedStorage.push(key);
        },
      },
      location: {
        pathname: "/",
        reload() {
          /* Browser API stub; no side effect needed in this fixture. */
        },
      },
      addEventListener() {
        /* Browser API stub; no side effect needed in this fixture. */
      },
    },
  });
  vm.runInContext(source, context);
  return {
    api: context.api,
    removedStorage,
    events,
    widget,
    cookies,
    deleted,
    cookieWrites,
    clickRevoke() {
      handlers.get("click")({
        isTrusted: true,
        preventDefault() {
          /* Browser API stub; no side effect needed in this fixture. */
        },
        target: {closest: () => ({})},
      });
    },
  };
}

// A config response captured before withdrawal must not reactivate consumers.
{
  let resolveGet;
  let reads = 0;
  const page = tab((path) =>
    path.endsWith("/config")
      ? ++reads === 1
        ? new Promise((resolve) => {
            resolveGet = resolve;
          })
        : Promise.resolve(answer({...ready, granted: false}))
      : Promise.resolve(answer({accepted: true, granted: false}))
  );
  const old = page.api.loadConsent();
  await page.api.submitConsent(false);
  assert.ok(
    ["odoo_utm_campaign", "odoo_utm_source", "odoo_utm_medium"].every((name) =>
      page.deleted.includes(name)
    )
  );
  resolveGet(answer(ready));
  await old;
  assert.equal(
    page.events.some((event) => event.detail.granted === true),
    false
  );
}

// The withdrawal POST waits for an already-sent grant, then revokes its cookie.
{
  let resolveGrant;
  const calls = [];
  let serverGranted = false;
  const page = tab((path, options) => {
    if (path.endsWith("/config"))
      return Promise.resolve(answer({...ready, granted: false}));
    const choice = JSON.parse(options.body).granted;
    calls.push(choice);
    if (choice)
      return new Promise((resolve) => {
        resolveGrant = () => {
          serverGranted = true;
          resolve(answer({accepted: true, granted: true}));
        };
      });
    serverGranted = false;
    return Promise.resolve(answer({accepted: true, granted: false}));
  });
  await page.api.loadConsent();
  const grant = page.api.submitConsent(true);
  await tick();
  page.cookies.optional = false; // Original native widget has persisted the refusal.
  const withdrawal = page.api.submitConsent(false);
  await tick();
  assert.deepEqual(calls, [true]);
  assert.equal(page.cookies.optional, false);
  resolveGrant();
  await Promise.all([grant, withdrawal]);
  assert.deepEqual(calls, [true, false]);
  assert.equal(serverGranted, false);
  assert.equal(
    page.events.some((event) => event.detail.granted === true),
    false
  );
}

// A signal across tabs can only disable; authoritative GET cannot race it on.
{
  const bus = [],
    cookies = {optional: true};
  const fetch = (path) =>
    Promise.resolve(
      answer(
        path.endsWith("/config")
          ? {...ready, granted: cookies.optional}
          : {accepted: true, granted: false}
      )
    );
  const first = tab(fetch, bus, cookies),
    second = tab(fetch, bus, cookies);
  await Promise.all([first.api.loadConsent(), second.api.loadConsent()]);
  second.events.length = 0;
  cookies.optional = false; // Shared native cookie written by the stock handler.
  await first.api.submitConsent(false);
  await tick();
  assert.ok(second.events.length > 0);
  assert.equal(
    second.events.some((event) => event.detail.granted === true),
    false
  );
  assert.equal(cookies.optional, false);
}

// A nested icon has the same native meaning as its containing accept button.
{
  const page = tab((path) =>
    Promise.resolve(
      answer(
        path.endsWith("/config")
          ? {...ready, granted: false}
          : {accepted: true, granted: true}
      )
    )
  );
  await page.api.loadConsent();
  let nativeTarget;
  page.widget._onAcceptClick.call(
    {
      el: {dataset: {}},
      _super(event) {
        nativeTarget = event.target.id;
      },
    },
    {target: {id: "icon"}, currentTarget: {id: "cookies-consent-all"}}
  );
  assert.equal(nativeTarget, "cookies-consent-all");
  await tick();
}
// The native handler persists informational choices too; no backend grant is fabricated.
{
  let writes = 0,
    posts = 0;
  const page = tab((path) => {
    if (path.endsWith("/decision")) posts++;
    return Promise.resolve(
      answer({
        ...ready,
        available: false,
        granted: false,
        informational_notice: true,
        capture_allowed: true,
      })
    );
  });
  await page.api.loadConsent();
  page.widget._onAcceptClick.call(
    {
      el: {dataset: {}},
      _super() {
        writes++;
      },
    },
    {target: {id: "cookies-consent-all"}, currentTarget: {id: "cookies-consent-all"}}
  );
  await tick();
  assert.equal(writes, 1);
  assert.equal(posts, 0);
  assert.ok(
    page.events.some((e) => e.type === "marketing_center:native-cookie-choice")
  );
  assert.equal(
    page.events.some(
      (e) =>
        e.detail.granted === true && e.type !== "marketing_center:native-cookie-choice"
    ),
    false
  );
  page.clickRevoke();
  await tick();
  assert.equal(
    page.cookieWrites.length,
    1,
    "only explicit reopen writes a temporary native refusal"
  );
  assert.ok(page.deleted.includes("website_cookies_bar"));
}
// The server's informational mode is independent of an absent or refused choice.
{
  let informational = true;
  const mode = () => ({
    ...ready,
    granted: false,
    informational_notice: informational,
    capture_allowed: informational,
  });
  const page = tab(
    (path) =>
      Promise.resolve(
        answer(path.endsWith("/config") ? mode() : {...mode(), accepted: true})
      ),
    [],
    {optional: false}
  );
  const initial = await page.api.loadConsent();
  assert.equal(initial.granted, false);
  assert.equal(initial.informational_notice, true);
  assert.equal(page.deleted.length, 0);
  await page.api.submitConsent(false);
  assert.equal(page.cookies.optional, false);
  assert.ok(page.deleted.includes("odoo_utm_source"));
  assert.equal(
    page.events.some((event) => event.detail.granted === true),
    false
  );
  assert.equal(
    page.events
      .filter((event) => event.type.endsWith("consent-changed"))
      .every((event) => event.detail.informational_notice === true),
    true
  );
  informational = false;
  const restored = await page.api.loadConsent(true);
  assert.equal(restored.informational_notice, false);
  assert.equal(restored.capture_allowed, false);
  assert.ok(page.deleted.includes("odoo_utm_source"));
}

// Cross-tab refusal preserves informational attribution but cannot grant real consent.
{
  const bus = [],
    cookies = {optional: false};
  const fetch = (path) =>
    Promise.resolve(
      answer({
        ...ready,
        granted: false,
        informational_notice: true,
        capture_allowed: true,
        ...(!path.endsWith("/config") ? {accepted: true} : {}),
      })
    );
  const first = tab(fetch, bus, cookies),
    second = tab(fetch, bus, cookies);
  await Promise.all([first.api.loadConsent(), second.api.loadConsent()]);
  second.events.length = 0;
  cookies.optional = false; // Shared native cookie written by the stock handler.
  await first.api.submitConsent(false);
  await tick();
  assert.equal(second.deleted.length, 0);
  assert.equal(
    second.events.some((event) => event.detail.granted === true),
    false
  );
  assert.equal(
    second.events.every((event) => event.detail.informational_notice === true),
    true
  );
}

// A failed optional-refusal POST cannot change the operator's capture policy.
for (const failure of ["http", "network", "not-accepted"]) {
  const mode = {
    ...ready,
    granted: false,
    informational_notice: true,
    capture_allowed: true,
  };
  const page = tab((path) => {
    if (path.endsWith("/config")) return Promise.resolve(answer(mode));
    if (failure === "not-accepted") return Promise.resolve(answer({accepted: false}));
    return failure === "http"
      ? Promise.resolve({ok: false})
      : Promise.reject(new Error("offline"));
  });
  await page.api.loadConsent();
  assert.equal(await page.api.submitConsent(false), false);
  assert.deepEqual(page.removedStorage, []);
  page.clickRevoke();
  await tick();
  assert.deepEqual(page.removedStorage, []);
  const decisions = page.events.filter(
    (e) => e.type.endsWith("consent-changed") || e.type.endsWith("consent-ready")
  );
  assert.ok(
    decisions.every(
      (e) => e.detail.informational_notice === true && e.detail.capture_allowed === true
    )
  );
  assert.ok(decisions.every((e) => e.detail.granted === false));
}

// The choice bridge works before boot config finishes, without GA4 listeners.
for (const informational of [true, false]) {
  for (const granted of [true, false]) {
    for (const failure of ["none", "http", "network"]) {
      let oldResolve,
        reads = 0,
        posts = 0;
      const mode = {
        ...ready,
        available: !informational,
        granted: false,
        informational_notice: informational,
        capture_allowed: informational,
      };
      const page = tab(
        (path) => {
          if (path.endsWith("/config")) {
            if (++reads === 1)
              return new Promise((resolve) => {
                oldResolve = resolve;
              });
            return Promise.resolve(answer(mode));
          }
          posts++;
          if (failure === "network") return Promise.reject(new Error("offline"));
          if (failure === "not-accepted")
            return Promise.resolve(answer({accepted: false}));
          if (failure === "http") return Promise.resolve({ok: false});
          return Promise.resolve(
            answer({
              ...mode,
              accepted: true,
              granted: !informational && granted,
              capture_allowed: informational || granted,
            })
          );
        },
        [],
        {optional: granted}
      );
      const boot = page.api.loadConsent();
      const decision = page.api.submitConsent(granted).catch(() => false);
      await tick();
      oldResolve(answer(mode));
      await Promise.all([boot, decision]);
      await tick();
      const last = page.events
        .filter(
          (e) => e.type.endsWith("consent-ready") || e.type.endsWith("consent-changed")
        )
        .at(-1).detail;
      if (informational) {
        assert.equal(last.informational_notice, true);
        assert.equal(last.capture_allowed, true);
        assert.equal(last.granted, false);
        assert.deepEqual(page.removedStorage, []);
        if (granted) assert.equal(posts, 0);
      } else if (!granted || failure !== "none") assert.equal(last.granted, false);
      else assert.equal(last.granted, true);
    }
  }
}

// Another tab can refuse while this tab still has an unresolved policy GET.
// Preserve first-party session data until the authoritative informational policy arrives.
{
  const bus = [];
  const info = {
    available: false,
    granted: false,
    informational_notice: true,
    capture_allowed: true,
  };
  const first = tab(
    (path) =>
      Promise.resolve(
        answer(path.endsWith("/config") ? info : {accepted: true, granted: false})
      ),
    bus
  );
  let resolveOld;
  let reads = 0;
  const second = tab(
    () =>
      ++reads === 1
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve(answer(info)),
    bus
  );
  const stale = second.api.loadConsent();
  await first.api.loadConsent();
  await first.api.submitConsent(false);
  await tick();
  resolveOld(answer(info));
  await stale;
  assert.deepEqual(second.removedStorage, []);
  assert.equal(second.events.at(-1).detail.informational_notice, true);
  assert.equal(second.events.at(-1).detail.capture_allowed, true);
  assert.equal(
    second.events.some((event) => event.detail.granted === true),
    false
  );
}

console.log(
  "Consent race tests passed: stock choice bridge, informational attribution, refusal and restoration."
);
