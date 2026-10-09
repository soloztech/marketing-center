// A pre-withdrawal ingress config/response cannot recreate optional storage.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {installBootstrap} from "./bootstrap_fixture_node.mjs";
const source =
  fs
    .readFileSync(
      new URL("../static/src/js/landing_bootstrap.esm.js", import.meta.url),
      "utf8"
    )
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/^export /gm, "") + "\nglobalThis.api={loadConfig,captureLandingEntry};";
const tick = () => new Promise((resolve) => setImmediate(resolve));
const config = {
  enabled: true,
  capture_mode: "legacy",
  ingest_path: "/marketing/web-ingress/11111111-1111-4111-8111-111111111111",
  public_key: "a".repeat(32),
  config_revision: 1,
};
function run(fetch) {
  let listener;
  const saved = [];
  const context = vm.createContext({
    Promise,
    Date,
    JSON,
    eligibleLandingPath: () => true,
    opaqueUuid: () => "22222222-2222-4222-8222-222222222222",
    buildLandingPayload: () => ({}),
    window: {
      fetch,
      TextEncoder,
      location: {pathname: "/"},
      sessionStorage: {getItem: () => "", setItem: (...value) => saved.push(value)},
    },
    document: {
      referrer: "",
      getElementById: () => null,
      addEventListener(_type, callback) {
        listener = callback;
      },
    },
  });
  installBootstrap(context, "ingress");
  vm.runInContext(source, context);
  return {
    api: context.api,
    saved,
    withdraw() {
      const event = {
        type: "marketing_center:consent-changed",
        detail: {granted: false},
      };
      context.document.dispatchEvent(event);
      listener(event);
      saved.length = 0;
    },
  };
}
{
  let resolve;
  const page = run(
    () =>
      new Promise((done) => {
        resolve = done;
      })
  );
  page.withdraw();
  resolve({ok: true, json: async () => config});
  await tick();
  assert.deepEqual(page.saved, []);
}
{
  let complete;
  const page = run((path) =>
    path.endsWith("bootstrap-config")
      ? Promise.resolve({ok: true, json: async () => config})
      : new Promise((done) => {
          complete = done;
        })
  );
  await tick();
  page.withdraw();
  complete({ok: true});
  await tick();
  assert.deepEqual(page.saved, []);
}
console.log(
  "Landing withdrawal tests: stale config and stale accepted response cannot recreate optional storage."
);
