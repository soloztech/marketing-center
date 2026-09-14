/** @odoo-module **/

import {loadConsent} from "@marketing_center_website/js/consent.esm";
import {eligibleMeasurementConfig, refreshMeasurementConsent} from "@marketing_center_website/js/cookie_notice.esm";
import {validOpaqueUuid} from "@marketing_center_website/js/action_capture.esm";

// The Google adapter consumes confirmed Website events; it never reads form
// values or decides whether a CRM object/conversation was actually created.
const CAMPAIGN_KEYS = new Set([
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "wbraid", "fbclid",
]);
const DENIED = {
    analytics_storage: "denied", ad_storage: "denied",
    ad_user_data: "denied", ad_personalization: "denied",
};
const started = new WeakSet();

export function measurementUrl(href, expectedPath) {
    const url = new URL(href);
    const result = new URL(expectedPath, url.origin);
    for (const [key, value] of url.searchParams) {
        // Google gets a stricter allowlist than first-party ingress: no arbitrary
        // queries, fragments, e-mail addresses or control characters.
        if (CAMPAIGN_KEYS.has(key) && value.length <= 200 &&
            !/[\x00-\x1f\x7f@]/.test(value) && !result.searchParams.has(key)) {
            result.searchParams.set(key, value);
        }
    }
    return result.href;
}

export function referrerOrigin(value) {
    try {
        const url = new URL(value);
        return ["https:", "http:"].includes(url.protocol) ? url.origin + "/" : "";
    } catch (_error) {
        return "";
    }
}

export function measurementAllowed(value, confirmed = true) {
    return confirmed === true && (value?.granted === true ||
        (value?.informational_notice === true && value?.capture_allowed === true));
}

export async function annotateWebsiteActions(config) {
    if (!eligibleMeasurementConfig(config)) {
        return;
    }
    if (validOpaqueUuid(config.dataset.form || "")) {
        const forms = document.querySelectorAll(
            '.s_website_form form[data-model_name="crm.lead"], form.s_website_form[data-model_name="crm.lead"]'
        );
        // A single configured action cannot distinguish multiple native forms.
        // Existing explicit markers remain owned by their configured actions.
        if (forms.length === 1 && !forms[0].hasAttribute("data-marketing-form-action")) {
            forms[0].setAttribute("data-marketing-form-action", config.dataset.form);
        }
    }
    const fingerprint = config.dataset.whatsappLinkHash || "";
    if (!validOpaqueUuid(config.dataset.whatsapp || "") || !/^[a-f0-9]{64}$/.test(fingerprint) ||
        !window.crypto?.subtle || typeof window.TextEncoder !== "function") {
        return;
    }
    for (const link of document.querySelectorAll("a[href]")) {
        if (link.hasAttribute("data-marketing-whatsapp-action")) {
            continue;
        }
        const rawHref = link.getAttribute("href");
        let target;
        try {
            target = new URL(rawHref, window.location.origin);
        } catch (_error) {
            continue;
        }
        if (target.protocol === "https:" && target.hostname === "wa.me" && !target.port &&
            !target.username && !target.password && !target.hash &&
            /^\/[1-9][0-9]{7,14}$/.test(target.pathname) && target.href.length <= 2048 &&
            Array.from(target.searchParams.keys()).every((key) => key === "text") &&
            target.searchParams.getAll("text").length <= 1) {
            // The server handoff has a fixed message. Do not replace editorial
            // messages (e.g. requesting a particular document) with that CTA.
            const bytes = new window.TextEncoder().encode(
                target.pathname.slice(1) + "\n" + (target.searchParams.get("text") || "")
            );
            try {
                const digest = await window.crypto.subtle.digest("SHA-256", bytes);
                const actual = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
                if (actual === fingerprint && eligibleMeasurementConfig(config) &&
                    link.getAttribute("href") === rawHref &&
                    !link.hasAttribute("data-marketing-whatsapp-action")) {
                    link.setAttribute("data-marketing-whatsapp-action", config.dataset.whatsapp);
                }
            } catch (_error) { /* Ordinary navigation remains usable without capture. */ }
        }
    }
}

export function startMeasurement(config) {
    if (!eligibleMeasurementConfig(config) || started.has(config)) {
        return;
    }
    started.add(config);
    annotateWebsiteActions(config).catch(() => null);
    const id = config.dataset.ga4;
    if (!/^G-[A-Z0-9]{4,20}$/.test(id || "")) {
        return;
    }
    let granted = false;
    let initialized = false;
    let pageViewSent = false;
    const events = new Set();
    const disabledKey = `ga-disable-${id}`;
    window[disabledKey] = true;
    const metadata = {
        page_location: measurementUrl(window.location.href, config.dataset.path),
        page_referrer: referrerOrigin(document.referrer),
        page_title: document.title.slice(0, 160),
    };

    function tag(...args) {
        window.dataLayer = window.dataLayer || [];
        // gtag queues Arguments objects rather than arrays.
        (function () { window.dataLayer.push(arguments); })(...args);
    }

    function removeAnalyticsCookies() {
        const hostname = window.location.hostname;
        const domains = [""];
        if (/^[a-z0-9.-]+$/i.test(hostname)) {
            const labels = hostname.split(".");
            // GA may choose a parent-domain cookie. Enumerate only ancestors of
            // the current host; the browser rejects public-suffix scopes.
            for (let index = 0; index < labels.length - 1; index += 1) {
                const domain = labels.slice(index).join(".");
                domains.push(domain, "." + domain);
            }
            if (labels.length === 1) {
                domains.push(hostname);
            }
        }
        for (const cookie of document.cookie.split(";")) {
            const name = cookie.split("=", 1)[0].trim();
            if (!/^_ga(?:_|$)/.test(name)) {
                continue;
            }
            for (const domain of domains) {
                document.cookie = `${name}=; Max-Age=0; Path=/; Secure; SameSite=Lax${domain ? `; Domain=${domain}` : ""}`;
            }
        }
    }

    function applyAuthorization(value, confirmed = true) {
        granted = eligibleMeasurementConfig(config) && measurementAllowed(value, confirmed);
        window[disabledKey] = !granted;
        if (!granted) {
            if (initialized) {
                tag("consent", "update", {...DENIED});
            }
            removeAnalyticsCookies();
            return;
        }
        if (!initialized) {
            initialized = true;
            tag("consent", "default", {...DENIED});
            tag("consent", "update", {...DENIED, analytics_storage: "granted"});
            tag("js", new Date());
            tag("config", id, {
                send_page_view: false, allow_google_signals: false,
                allow_ad_personalization_signals: false,
                cookie_flags: "SameSite=Lax;Secure", ...metadata,
            });
            const script = document.createElement("script");
            script.id = "marketing_ga4_script";
            script.async = true;
            script.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
            document.head.appendChild(script);
        } else {
            tag("consent", "update", {...DENIED, analytics_storage: "granted"});
        }
        if (!pageViewSent) {
            tag("event", "page_view", {...metadata, send_to: id});
            pageViewSent = true;
        }
    }

    document.addEventListener("marketing_center:consent-ready", (event) => applyAuthorization(event.detail));
    document.addEventListener("marketing_center:consent-changed", (event) => {
        applyAuthorization(event.detail, event.detail?.confirmed === true);
    });
    document.addEventListener("marketing_center:action-confirmed", (event) => {
        const detail = event.detail || {};
        if (!granted || !eligibleMeasurementConfig(config) ||
            !validOpaqueUuid(detail.event_id || "") || events.has(detail.event_id)) {
            return;
        }
        const eventName = detail.kind === "form_submission" ? "generate_lead" :
            detail.kind === "whatsapp_handoff" ? "whatsapp_handoff" : "";
        if (!eventName) {
            return;
        }
        events.add(detail.event_id);
        const payload = {...metadata, send_to: id, event_id: detail.event_id, transport_type: "beacon"};
        if (detail.kind === "form_submission" && typeof detail.waitUntil === "function") {
            // Hold the native redirect only for bounded delivery of a confirmed
            // event. A blocked Google script must not hold a form indefinitely.
            detail.waitUntil(new Promise((resolve) => {
                const timer = setTimeout(resolve, 700);
                payload.event_callback = () => { clearTimeout(timer); resolve(); };
                payload.event_timeout = 600;
                tag("event", eventName, payload);
            }));
        } else {
            tag("event", eventName, payload);
        }
    });
    window.addEventListener("focus", () => {
        granted = false;
        window[disabledKey] = true;
        refreshMeasurementConsent().then((value) => applyAuthorization(value)).catch(() => applyAuthorization(null));
    });
    loadConsent().then((value) => applyAuthorization(value)).catch(() => applyAuthorization(null));
}

function boot() {
    startMeasurement(document.getElementById("marketing_measurement_config"));
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, {once: true});
} else {
    boot();
}
