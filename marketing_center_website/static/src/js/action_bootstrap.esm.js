/** @odoo-module **/

import {
    boundedActionResponse,
    createFormPostBridge,
    technicalActionRef,
    trustedHumanActivation,
    validRedirectPath,
    whatsappActionRef,
    whatsappClaimPayload,
    whatsappFallbackPath,
} from "@marketing_center_website/js/action_capture.esm";
import {
    loadConfig,
    newActionEventId,
    validConfig,
    websiteSessionRef,
} from "@marketing_center_website/js/landing_bootstrap.esm";
import ajax from "web.ajax";

const FORM_EXCHANGE_PATH = "/marketing/website-action/form/exchange";
const WHATSAPP_CLAIM_PATH = "/marketing/website-action/whatsapp/claim";
const MAX_BODY_BYTES = 2048;
const CLAIM_TTL_MS = 5 * 60 * 1000;
const RETRY_DELAYS_MS = Object.freeze([0, 250, 750]);
let enabled = false;
let consentRef = "";
let formClaim = null;

function freshClaim(actionRef) {
    return {
        actionRef,
        eventId: newActionEventId(),
        sessionRef: websiteSessionRef(),
        armedAt: Date.now(),
    };
}

function takeFormClaim() {
    const candidate = formClaim;
    formClaim = null;
    if (!candidate || Date.now() - candidate.armedAt > CLAIM_TTL_MS) {
        return null;
    }
    return candidate;
}

function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function postTechnicalAction(path, payload, deadline = Date.now() + 16000) {
    const body = JSON.stringify(payload);
    if (
        typeof window.TextEncoder !== "function" ||
        new window.TextEncoder().encode(body).byteLength > MAX_BODY_BYTES
    ) {
        return null;
    }
    for (let attempt = 0; attempt < RETRY_DELAYS_MS.length; attempt += 1) {
        if (Date.now() + RETRY_DELAYS_MS[attempt] >= deadline) {
            return null;
        }
        if (RETRY_DELAYS_MS[attempt]) {
            await delay(RETRY_DELAYS_MS[attempt]);
        }
        let response = null;
        try {
            response = await boundedActionResponse(path, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                keepalive: true,
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                    ...(consentRef ? {"X-Marketing-Consent-Ref": consentRef} : {}),
                },
                body,
            }, window, Math.max(1, Math.min(5000, deadline - Date.now())));
        } catch (_error) {
            continue;
        }
        if (response.ok) {
            return response.payload;
        }
        // A 429 carries a one-minute server backoff and cannot succeed inside this
        // short user-activation flow.  Retrying it immediately only amplifies load.
        // A transient 503 may still recover within the bounded retry budget.
        if (response.status !== 503) {
            return null;
        }
    }
    return null;
}

async function notifyAction(kind, eventId, deadline = Date.now() + 750) {
    const pending = [];
    let dispatching = true;
    document.dispatchEvent(
        new CustomEvent("marketing_center:action-confirmed", {
            detail: {
                kind,
                event_id: eventId,
                waitUntil(promise) {
                    if (dispatching && promise && typeof promise.then === "function") {
                        pending.push(Promise.resolve(promise).catch(() => null));
                    }
                },
            },
        })
    );
    dispatching = false;
    if (!pending.length) {
        return;
    }
    let timeoutId;
    try {
        await Promise.race([
            Promise.all(pending),
            new Promise((resolve) => {
                timeoutId = window.setTimeout(
                    resolve, Math.max(0, Math.min(750, deadline - Date.now()))
                );
            }),
        ]);
    } finally {
        window.clearTimeout(timeoutId);
    }
}

async function exchangeForm(payload, deadline) {
    const result = await postTechnicalAction(FORM_EXCHANGE_PATH, payload, deadline);
    if (enabled && result && result.accepted === true && Date.now() < deadline) {
        await notifyAction("form_submission", payload.event_id, deadline);
    }
}

async function claimWhatsApp(actionRef, fallbackPath) {
    const payload = whatsappClaimPayload(freshClaim(actionRef));
    const result = payload
        ? await postTechnicalAction(WHATSAPP_CLAIM_PATH, payload)
        : null;
    const redirectPath = result && result.redirect_path;
    if (result && result.accepted === true && validRedirectPath(redirectPath)) {
        await notifyAction("whatsapp_handoff", payload.event_id);
    }
    window.location.assign(
        validRedirectPath(redirectPath) ? redirectPath : fallbackPath
    );
}

function onActivation(event) {
    if (!enabled || !trustedHumanActivation(event, document, navigator)) {
        return;
    }
    const whatsappRef = whatsappActionRef(event, window.location.origin);
    const fallbackPath = whatsappRef
        ? whatsappFallbackPath(event, window.location.origin)
        : "";
    if (whatsappRef && fallbackPath) {
        event.preventDefault();
        claimWhatsApp(whatsappRef, fallbackPath).catch(() =>
            window.location.assign(fallbackPath)
        );
        return;
    }
    const formRef = technicalActionRef(event, "data-marketing-form-action");
    if (formRef) {
        formClaim = freshClaim(formRef);
    }
}

const originalPost = ajax.post;
ajax.post = createFormPostBridge(
    originalPost,
    window.location.origin,
    takeFormClaim,
    exchangeForm
);
document.addEventListener("click", onActivation, true);
document.addEventListener("submit", onActivation, true);
loadConfig()
    .then((config) => {
        enabled = validConfig(config);
        consentRef = enabled ? config.consent_ref || "" : "";
    })
    .catch(() => {
        enabled = false;
    });

document.addEventListener("marketing_center:consent-changed", (event) => {
    enabled = false;
    consentRef = "";
    formClaim = null;
    if (
        event.detail &&
        (event.detail.informational_notice === true ||
            (event.detail.granted === true && event.detail.confirmed === true))
    ) {
        loadConfig(true)
            .then((config) => {
                enabled = validConfig(config);
                consentRef = enabled ? config.consent_ref || "" : "";
            })
            .catch(() => {
                enabled = false;
            });
    }
});
