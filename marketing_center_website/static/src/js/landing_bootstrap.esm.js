/** @odoo-module **/

import {
  buildLandingPayload,
  eligibleLandingPath,
  opaqueUuid,
} from "@marketing_center_website/js/landing_capture.esm";

const CONFIG_PATH = "/marketing/website-ingress/config";
const PUBLIC_KEY_HEADER = "X-Marketing-Ingress-Key";
const CONFIG_REVISION_HEADER = "X-Marketing-Ingress-Revision";
const STORAGE_PREFIX = "marketing_center.website.v1";
const MAX_REQUEST_BYTES = 8192;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const INGEST_PATH_PATTERN = new RegExp(
  `^/marketing/web-ingress/${UUID_PATTERN.source.slice(1, -1)}$`,
  "i"
);
let configPromise = null;

function storageGet(key) {
  try {
    return window.sessionStorage.getItem(key) || "";
  } catch (_error) {
    return "";
  }
}

function storageSet(key, value) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch (_error) {
    // Browser storage is an optimization. Server-side ingress remains authoritative.
  }
}

export function validConfig(value) {
  return Boolean(
    value &&
      value.enabled === true &&
      typeof value.ingest_path === "string" &&
      INGEST_PATH_PATTERN.test(value.ingest_path) &&
      typeof value.public_key === "string" &&
      value.public_key.length >= 32 &&
      value.public_key.length <= 128 &&
      Number.isInteger(value.config_revision) &&
      value.config_revision > 0
  );
}

function sessionValue(key) {
  const existing = storageGet(key);
  if (UUID_PATTERN.test(existing)) {
    return existing;
  }
  const created = opaqueUuid(window.crypto);
  storageSet(key, created);
  return created;
}

export function websiteSessionRef() {
  return sessionValue(`${STORAGE_PREFIX}.session_ref`);
}

export function newActionEventId() {
  return opaqueUuid(window.crypto);
}

export async function loadConfig() {
  if (!configPromise) {
    configPromise = window
      .fetch(CONFIG_PATH, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      })
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null);
  }
  return configPromise;
}

export async function captureLandingEntry() {
  if (!eligibleLandingPath(window.location.pathname)) {
    return false;
  }
  const config = await loadConfig();
  if (!validConfig(config)) {
    return false;
  }
  const endpointRef = config.ingest_path.slice(
    config.ingest_path.lastIndexOf("/") + 1
  );
  const acceptedKey = `${STORAGE_PREFIX}.accepted.${endpointRef}`;
  if (storageGet(acceptedKey) === "1") {
    return false;
  }
  const sessionRef = websiteSessionRef();
  const eventId = sessionValue(`${STORAGE_PREFIX}.event_id.${endpointRef}`);
  const payload = buildLandingPayload(
    window.location,
    document.referrer,
    eventId,
    sessionRef,
    new Date()
  );
  const body = JSON.stringify(payload);
  if (
    typeof window.TextEncoder !== "function" ||
    new window.TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES
  ) {
    return false;
  }
  const response = await window.fetch(config.ingest_path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    keepalive: true,
    headers: {
      "Content-Type": "application/json",
      [PUBLIC_KEY_HEADER]: config.public_key,
      [CONFIG_REVISION_HEADER]: String(config.config_revision),
    },
    body,
  });
  if (response.ok) {
    storageSet(acceptedKey, "1");
    return true;
  }
  if (response.status === 409) {
    // The ingress is first-wins: this event id already has authoritative evidence.
    storageSet(acceptedKey, "1");
  }
  return false;
}

captureLandingEntry().catch(() => false);
