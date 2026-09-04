/** @odoo-module **/

export const TRACKED_QUERY_FIELDS = Object.freeze([
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
  "gclid",
  "gbraid",
  "wbraid",
  "fbclid",
]);

const MAX_FIELD_LENGTH = 512;
const MAX_URL_LENGTH = 2048;
const CLICK_FIELDS = new Set(["gclid", "gbraid", "wbraid", "fbclid"]);
const OPAQUE_CLICK_VALUE = /^[A-Za-z0-9][A-Za-z0-9._:~-]{0,511}$/;
const TECHNICAL_PATH_PREFIXES = Object.freeze([
  "/auth",
  "/marketing",
  "/my",
  "/portal",
  "/web",
  "/website",
]);

function hasControlCharacter(value) {
  return Array.from(value).some((character) => {
    const codePoint = character.codePointAt(0);
    return codePoint <= 31 || codePoint === 127;
  });
}

function cleanQueryValue(value, fieldName) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (
    !normalized ||
    normalized.length > MAX_FIELD_LENGTH ||
    hasControlCharacter(normalized) ||
    (CLICK_FIELDS.has(fieldName) && !OPAQUE_CLICK_VALUE.test(normalized))
  ) {
    return "";
  }
  return normalized;
}

function parsedHttpUrl(value, base) {
  if (!value) {
    return null;
  }
  let parsed = null;
  try {
    parsed = new URL(value, base);
  } catch (_error) {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return null;
  }
  return parsed;
}

function strippedHttpUrl(value, base) {
  const parsed = parsedHttpUrl(value, base);
  const normalized = parsed ? `${parsed.origin}${parsed.pathname || "/"}` : "";
  return normalized.length <= MAX_URL_LENGTH ? normalized : "";
}

function referrerOrigin(value, base) {
  const parsed = parsedHttpUrl(value, base);
  return parsed ? `${parsed.origin}/` : "";
}

export function opaqueUuid(cryptoProvider) {
  if (!cryptoProvider || typeof cryptoProvider.getRandomValues !== "function") {
    throw new Error("Secure random values are unavailable");
  }
  if (typeof cryptoProvider.randomUUID === "function") {
    return cryptoProvider.randomUUID();
  }
  const bytes = new Uint8Array(16);
  cryptoProvider.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hexadecimal = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0")
  ).join("");
  return [
    hexadecimal.slice(0, 8),
    hexadecimal.slice(8, 12),
    hexadecimal.slice(12, 16),
    hexadecimal.slice(16, 20),
    hexadecimal.slice(20),
  ].join("-");
}

export function trackedQueryValues(search) {
  const query = new URLSearchParams(typeof search === "string" ? search : "");
  const result = {};
  for (const fieldName of TRACKED_QUERY_FIELDS) {
    const value = cleanQueryValue(query.get(fieldName), fieldName);
    if (value) {
      result[fieldName] = value;
    }
  }
  return result;
}

export function eligibleLandingPath(pathname) {
  if (typeof pathname !== "string" || !pathname.startsWith("/")) {
    return false;
  }
  return !TECHNICAL_PATH_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`)
  );
}

export function buildLandingPayload(
  locationValue,
  referrer,
  eventId,
  sessionRef,
  occurredAt
) {
  const origin = locationValue && locationValue.origin;
  const pathname = locationValue && locationValue.pathname;
  const landingUrl = strippedHttpUrl(`${origin || ""}${pathname || "/"}`);
  if (!landingUrl) {
    throw new Error("A valid HTTP(S) landing location is required");
  }
  const payload = {
    event_id: eventId,
    event_type: "entry_point",
    occurred_at: occurredAt.toISOString(),
    landing_url: landingUrl,
    consent_state: "unknown",
    session_ref: sessionRef,
    ...trackedQueryValues(locationValue.search),
  };
  const referrerUrl = referrerOrigin(referrer, landingUrl);
  if (referrerUrl) {
    payload.referrer_url = referrerUrl;
  }
  return payload;
}
