/** @odoo-module **/

import {
    boundedActionResponse, trustedHumanActivation, validOpaqueUuid,
} from "@marketing_center_website/js/action_capture.esm";
import {newActionEventId} from "@marketing_center_website/js/landing_bootstrap.esm";
import {eligibleMeasurementConfig} from "@marketing_center_website/js/cookie_notice.esm";
import {loadConsent} from "@marketing_center_website/js/consent.esm";

const CLAIM_PATH = "/marketing/website-whatsapp/claim";
const claims = new WeakMap();
const confirmed = new Set();
let consent = null;

export function whatsappTarget(rawHref) {
    try {
        const url = new URL(rawHref);
        return url.protocol === "https:" && url.hostname === "wa.me" && !url.port &&
            !url.username && !url.password && !url.hash &&
            /^\/[1-9][0-9]{7,14}$/.test(url.pathname) && url.href.length <= 4096 &&
            Array.from(url.searchParams.keys()).every((key) => key === "text") &&
            url.searchParams.getAll("text").length <= 1 ? url : null;
    } catch (_error) {
        return null;
    }
}

export function verifiedTarget(payload, original) {
    const target = payload?.accepted === true && whatsappTarget(payload.url);
    const reference = payload?.reference || "";
    const referenceText = "Referência: " + reference;
    const serverText = target?.searchParams.get("text") || "";
    if (!target || target.pathname !== original.pathname ||
        !/^[A-Z0-9]{2,8}-[A-Z0-9]{12}$/.test(reference) ||
        !validOpaqueUuid(payload.event_id) ||
        !(serverText === referenceText || serverText.endsWith("\n\n" + referenceText))) return "";
    // Keep the selected editorial CTA intact, including document requests and
    // the floating button on an LP. The server never receives this public text.
    const oldText = original.searchParams.get("text") || "";
    const result = new URL(original.href);
    result.searchParams.set("text", (oldText ? oldText + "\n\n" : "") + referenceText);
    return result.href.length <= 4096 ? result.href : "";
}

export async function claimHandoff(actionRef, eventId, original, scope = window) {
    const deadline = Date.now() + 2200;
    for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
            const response = await boundedActionResponse(CLAIM_PATH, {
                method: "POST", credentials: "same-origin", cache: "no-store",
                headers: {
                    "Content-Type": "application/json", Accept: "application/json",
                },
                body: JSON.stringify({action_ref: actionRef, event_id: eventId}),
            }, scope, Math.max(1, Math.min(1000, deadline - Date.now())));
            if (response.ok) {
                const target = verifiedTarget(response.payload, original);
                return target ? {url: target, eventId: response.payload.event_id} : null;
            }
            if (response.status !== 503) return null;
        } catch (_error) { /* One bounded retry retains the same event identity. */ }
        if (Date.now() >= deadline) return null;
    }
    return null;
}

function notifyConfirmation(eventId) {
    if (confirmed.has(eventId)) return;
    confirmed.add(eventId);
    document.dispatchEvent(new CustomEvent("marketing_center:action-confirmed", {
        detail: {kind: "whatsapp_handoff", event_id: eventId},
    }));
}

export function onWhatsAppActivation(event) {
    const config = document.getElementById("marketing_measurement_config");
    // Modifier/middle clicks keep browser-native tab/window behavior. A normal
    // activation of target=_blank opens synchronously before awaiting capture.
    if (!eligibleMeasurementConfig(config) || config.dataset.captureMode !== "native" ||
        config.dataset.whatsappHandoffEnabled !== "1" ||
        !trustedHumanActivation(event, document, navigator) || event.defaultPrevented) return;
    if (consent && !(consent.granted === true ||
        (consent.informational_notice === true && consent.capture_allowed === true))) return;
    const link = event.composedPath?.().find((item) => item?.tagName === "A" &&
        item.getAttribute?.("href"));
    if (!link || link.hasAttribute("download")) return;
    const actionRef = config.dataset.whatsapp;
    const configuredDestination = config.dataset.whatsappHandoffDestination || "";
    if (!validOpaqueUuid(actionRef) || !/^[1-9][0-9]{7,14}$/.test(configuredDestination)) return;
    const original = whatsappTarget(link.getAttribute("href"));
    if (!original || original.pathname !== "/" + configuredDestination) return;
    const targetName = (link.getAttribute("target") || "_self").toLowerCase();
    if (!["_self", "_blank"].includes(targetName)) return;
    if (claims.has(link)) {
        event.preventDefault();
        return;
    }
    let eventId;
    try { eventId = newActionEventId(); } catch (_error) { return; }
    if (!validOpaqueUuid(eventId)) return;
    let popup = null;
    if (targetName === "_blank") {
        popup = window.open("about:blank", "_blank");
        // A popup blocker must never swallow the existing WhatsApp navigation.
        if (!popup) return;
        popup.opener = null;
    }
    event.preventDefault();
    const pending = claimHandoff(actionRef, eventId, original)
        .then((result) => {
            if (result) notifyConfirmation(result.eventId);
            return result?.url || original.href;
        }, () => original.href)
        .then((url) => {
            if (popup) {
                if (!popup.closed) popup.location.replace(url);
            } else {
                window.location.assign(url);
            }
        }).finally(() => claims.delete(link));
    claims.set(link, pending);
}

document.addEventListener("marketing_center:consent-ready", (event) => { consent = event.detail; });
document.addEventListener("marketing_center:consent-changed", (event) => { consent = event.detail; });
document.addEventListener("click", onWhatsAppActivation);
loadConsent().then((value) => { consent = value; }).catch(() => null);
