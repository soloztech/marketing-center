/** @odoo-module **/

const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RECEIPT_PATTERN = /^\d{10}\.\d{10}\.[0-9a-f]{64}$/;
const REDIRECT_PATH_PATTERN = /^\/marketing\/website-action\/go\/[A-Za-z0-9_-]{43}$/;
const FORM_PATH_PATTERN = /^\/website\/form\/[a-z0-9_.]+$/i;
// Odoo's canonical Link Tracker owns /r/<code>. Optional native addons add
// descendants such as /r/<code>/m/<trace> and /r/<code>/s/<sms>; they remain
// the same native click fact and must never be converted into our handoff.
const NATIVE_TRACKED_LINK_PATH_PATTERN = /^\/r\/[^/]+(?:\/.*)?$/;
const RESERVED_FORM_QUERY = Object.freeze(["mc_action", "mc_event", "mc_session"]);

export async function boundedActionResponse(
    path,
    options,
    scope = window,
    timeoutMs = 5000
) {
    const controller =
        typeof scope.AbortController === "function"
            ? new scope.AbortController()
            : null;
    let timeoutId;
    const timeout = new Promise((_resolve, reject) => {
        timeoutId = scope.setTimeout(() => {
            if (controller) {
                controller.abort();
            }
            reject(new Error("Action request timed out"));
        }, timeoutMs);
    });
    try {
        return await Promise.race([
            (async () => {
                const response = await scope.fetch(path, {
                    ...options,
                    ...(controller ? {signal: controller.signal} : {}),
                });
                // Receiving headers is not completion: a stalled body must also
                // release the visitor's WhatsApp navigation within the deadline.
                const payload = response.ok ? await response.json() : null;
                return {ok: response.ok, status: response.status, payload};
            })(),
            timeout,
        ]);
    } finally {
        scope.clearTimeout(timeoutId);
    }
}

export function validOpaqueUuid(value) {
    return typeof value === "string" && UUID_PATTERN.test(value);
}

export function technicalActionRef(event, attributeName) {
    if (!event || typeof event.composedPath !== "function") {
        return "";
    }
    for (const item of event.composedPath()) {
        if (!item || typeof item.getAttribute !== "function") {
            continue;
        }
        const candidate = item.getAttribute(attributeName);
        if (validOpaqueUuid(candidate)) {
            return candidate.toLowerCase();
        }
    }
    return "";
}

export function nativeTrackedLinkActivation(event, origin) {
    if (
        !event ||
        typeof event.composedPath !== "function" ||
        typeof origin !== "string"
    ) {
        return false;
    }
    for (const item of event.composedPath()) {
        if (!item || typeof item.getAttribute !== "function") {
            continue;
        }
        const rawHref = item.getAttribute("href");
        if (typeof rawHref !== "string" || !rawHref) {
            continue;
        }
        let parsed = null;
        try {
            parsed = new URL(rawHref, origin);
        } catch (_error) {
            continue;
        }
        if (
            parsed.origin === origin &&
            NATIVE_TRACKED_LINK_PATH_PATTERN.test(parsed.pathname)
        ) {
            return true;
        }
    }
    return false;
}

export function whatsappActionRef(event, origin) {
    if (nativeTrackedLinkActivation(event, origin)) {
        return "";
    }
    return technicalActionRef(event, "data-marketing-whatsapp-action");
}

export function whatsappFallbackPath(event, origin) {
    if (
        !event ||
        typeof event.composedPath !== "function" ||
        typeof origin !== "string"
    ) {
        return "";
    }
    for (const item of event.composedPath()) {
        if (!item || typeof item.getAttribute !== "function") {
            continue;
        }
        const actionRef = item.getAttribute("data-marketing-whatsapp-action");
        const rawHref = item.getAttribute("href");
        if (!validOpaqueUuid(actionRef) || typeof rawHref !== "string" || !rawHref) {
            continue;
        }
        let parsed = null;
        try {
            parsed = new URL(rawHref, origin);
        } catch (_error) {
            return "";
        }
        if (parsed.username || parsed.password) {
            return "";
        }
        if (parsed.origin === origin && parsed.pathname.startsWith("/")) {
            return `${parsed.pathname}${parsed.search}${parsed.hash}`;
        }
        // Keep the site's real WhatsApp link usable when optional capture is
        // declined/unavailable. The tracked server destination stays immutable.
        if (
            parsed.protocol === "https:" &&
            parsed.hostname === "wa.me" &&
            !parsed.port &&
            !parsed.hash &&
            /^\/[1-9][0-9]{7,14}$/.test(parsed.pathname) &&
            Array.from(parsed.searchParams.keys()).every((key) => key === "text") &&
            parsed.searchParams.getAll("text").length <= 1 &&
            parsed.href.length <= 2048
        ) {
            return parsed.href;
        }
        return "";
    }
    return "";
}

export function trustedHumanActivation(event, documentValue, navigatorValue) {
    if (
        !event ||
        event.isTrusted !== true ||
        ("button" in event && event.button !== 0) ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        !documentValue ||
        documentValue.visibilityState !== "visible"
    ) {
        return false;
    }
    const activation = navigatorValue && navigatorValue.userActivation;
    return !activation || activation.isActive === true;
}

function nativeFormUrl(rawUrl, origin) {
    if (typeof rawUrl !== "string") {
        return null;
    }
    let parsed = null;
    try {
        parsed = new URL(rawUrl, origin);
    } catch (_error) {
        return null;
    }
    if (
        parsed.origin !== origin ||
        !FORM_PATH_PATTERN.test(parsed.pathname) ||
        RESERVED_FORM_QUERY.some((name) => parsed.searchParams.has(name))
    ) {
        return null;
    }
    return parsed;
}

export function nativeFormTarget(rawUrl, origin) {
    return Boolean(nativeFormUrl(rawUrl, origin));
}

export function prepareNativeFormUrl(rawUrl, origin, claim) {
    if (
        !validOpaqueUuid(claim && claim.actionRef) ||
        !validOpaqueUuid(claim && claim.eventId) ||
        !validOpaqueUuid(claim && claim.sessionRef)
    ) {
        return {tracked: false, url: rawUrl};
    }
    const parsed = nativeFormUrl(rawUrl, origin);
    if (!parsed) {
        return {tracked: false, url: rawUrl};
    }
    parsed.searchParams.set("mc_action", claim.actionRef);
    parsed.searchParams.set("mc_event", claim.eventId);
    parsed.searchParams.set("mc_session", claim.sessionRef);
    return {
        tracked: true,
        url: `${parsed.pathname}${parsed.search}`,
    };
}

export function receiptFromFormResult(result) {
    if (typeof result !== "string" || result.length > 4096) {
        return "";
    }
    let parsed = null;
    try {
        parsed = JSON.parse(result);
    } catch (_error) {
        return "";
    }
    const receipt = parsed && parsed.marketing_center_receipt;
    return typeof receipt === "string" && RECEIPT_PATTERN.test(receipt) ? receipt : "";
}

export function formExchangePayload(claim, receipt) {
    if (
        !validOpaqueUuid(claim && claim.actionRef) ||
        !validOpaqueUuid(claim && claim.eventId) ||
        !validOpaqueUuid(claim && claim.sessionRef) ||
        !RECEIPT_PATTERN.test(receipt || "")
    ) {
        return null;
    }
    return {
        action_ref: claim.actionRef,
        event_id: claim.eventId,
        session_ref: claim.sessionRef,
        receipt,
    };
}

export function whatsappClaimPayload(claim) {
    if (
        !validOpaqueUuid(claim && claim.actionRef) ||
        !validOpaqueUuid(claim && claim.eventId) ||
        !validOpaqueUuid(claim && claim.sessionRef)
    ) {
        return null;
    }
    return {
        action_ref: claim.actionRef,
        event_id: claim.eventId,
        session_ref: claim.sessionRef,
    };
}

export function validRedirectPath(value) {
    return typeof value === "string" && REDIRECT_PATH_PATTERN.test(value);
}

export function createFormPostBridge(
    originalPost,
    origin,
    takeClaim,
    exchange,
    maxWaitMs = 2000
) {
    return function marketingWebsiteFormPost(rawUrl, opaqueBody) {
        // Preserve unrelated legacy AJAX calls exactly and leave the pending
        // claim armed until the exact native Website form route is observed.
        const claim = nativeFormTarget(rawUrl, origin) ? takeClaim() : null;
        const prepared = prepareNativeFormUrl(rawUrl, origin, claim);
        const nativePromise = originalPost.call(this, prepared.url, opaqueBody);
        if (
            prepared.tracked &&
            nativePromise &&
            typeof nativePromise.then === "function"
        ) {
            return nativePromise.then(async (result) => {
                const receipt = receiptFromFormResult(result);
                const payload = formExchangePayload(claim, receipt);
                if (payload) {
                    let timeoutId;
                    const deadline = Date.now() + maxWaitMs;
                    try {
                        await Promise.race([
                            Promise.resolve().then(() => exchange(payload, deadline)),
                            new Promise((resolve) => {
                                timeoutId = setTimeout(resolve, maxWaitMs);
                            }),
                        ]);
                    } catch (_error) {
                        // Tracking can never turn a successful native form into
                        // a failure, nor keep its navigation pending indefinitely.
                    } finally {
                        clearTimeout(timeoutId);
                    }
                }
                return result;
            });
        }
        return nativePromise;
    };
}

export function nativeEventFromFormResult(result) {
    try {
        const payload = typeof result === "string" ? JSON.parse(result) : null;
        return payload &&
            Number.isInteger(payload.id) &&
            payload.id > 0 &&
            validOpaqueUuid(payload.marketing_center_event_id)
            ? payload.marketing_center_event_id
            : "";
    } catch (_error) {
        return "";
    }
}

export function createNativeFormPostBridge(
    originalPost,
    origin,
    claimEvent,
    confirm,
    enabled
) {
    return function nativeAcquisitionPost(rawUrl, body) {
        const url = nativeFormUrl(rawUrl, origin);
        if (!enabled() || !url || url.pathname !== "/website/form/crm.lead") {
            return originalPost.call(this, rawUrl, body);
        }
        let claim;
        try {
            claim = claimEvent(body);
            if (claim && validOpaqueUuid(claim.eventId)) {
                url.searchParams.set("mc_event", claim.eventId);
            }
        } catch (_error) {
            /* Native submission works without browser storage/crypto. */
        }
        const pending = originalPost.call(this, `${url.pathname}${url.search}`, body);
        if (!pending || typeof pending.then !== "function") {
            return pending;
        }
        return pending.then(async (result) => {
            const eventId = nativeEventFromFormResult(result);
            try {
                const payload = JSON.parse(result);
                if (payload && payload.marketing_center_submission_conflict === true) {
                    // The old key belongs to an earlier completed submission;
                    // a corrected/new demand needs a fresh identity next time.
                    if (claim && typeof claim.complete === "function") {
                        claim.complete();
                    }
                }
            } catch (_error) {
                /* Non-JSON responses keep native error handling. */
            }
            if (eventId) {
                try {
                    if (claim && typeof claim.complete === "function") {
                        claim.complete();
                    }
                    await confirm("form_submission", eventId);
                } catch (_error) {
                    /* Measurement never turns CRM success into failure. */
                }
            }
            return result;
        });
    };
}
