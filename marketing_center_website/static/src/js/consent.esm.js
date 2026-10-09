/** @odoo-module **/

import publicWidget from "web.public.widget";
import "website.s_popup";
import {deleteCookie, setCookie} from "web.utils.cookies";
import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";

import {
    bootstrapGeneration,
    invalidateBootstrap,
    loadBootstrap,
} from "@marketing_center_website/js/bootstrap_config.esm";
const DECISION = "/marketing/website-consent/decision";
const STORAGE_PREFIX = "marketing_center.website.v1";
let pending = null;
let pendingGeneration = -1;
let choice = null;
let serial = 0;
let decisionQueue = Promise.resolve();
let withdrawalPending = false;
const WITHDRAWAL_SIGNAL = "marketing_center.consent.withdrawal";
const channel =
    typeof window.BroadcastChannel === "function"
        ? new window.BroadcastChannel(WITHDRAWAL_SIGNAL)
        : null;

function broadcastWithdrawal() {
    if (channel) {
        channel.postMessage("withdrawn");
    } else {
        try {
            window.localStorage.setItem(WITHDRAWAL_SIGNAL, String(Date.now()));
            window.localStorage.removeItem(WITHDRAWAL_SIGNAL);
        } catch (_error) {
            /* The server and shared native cookie still deny capture. */
        }
    }
}

function notify(name, detail) {
    document.dispatchEvent(new CustomEvent(`marketing_center:${name}`, {detail}));
}

function clearNativeUtms() {
    const hostname = window.location.hostname || "";
    const domains = /^[a-z0-9.-]+$/i.test(hostname) ? [hostname, `.${hostname}`] : [];
    for (const name of ["odoo_utm_campaign", "odoo_utm_source", "odoo_utm_medium"]) {
        deleteCookie(name);
        // Odoo's native server UTM cookie can have an explicit Domain. Its
        // legacy deleteCookie helper removes only the host-cookie scope.
        for (const domain of domains) {
            document.cookie = `${name}=; Max-Age=0; Path=/; Domain=${domain}; Secure; SameSite=Lax`;
        }
    }
}

function clearOptionalSession(confirmedIndividualWithdrawal = false) {
    // Unknown/unavailable configuration cannot justify erasing attribution.
    if (
        !confirmedIndividualWithdrawal &&
        (!choice || choice.available !== true || choice.informational_notice === true)
    ) {
        return;
    }
    try {
        for (const key of Object.keys(window.sessionStorage)) {
            if (key.startsWith(STORAGE_PREFIX)) {
                window.sessionStorage.removeItem(key);
            }
        }
    } catch (_error) {
        // Server-side revocation remains authoritative when storage is blocked.
    }
}

export function loadConsent(refresh = false) {
    if (!pending || refresh || pendingGeneration !== bootstrapGeneration()) {
        if (refresh) invalidateBootstrap();
        const generation = serial;
        const configGeneration = bootstrapGeneration();
        pendingGeneration = configGeneration;
        pending = loadBootstrap()
            .then((envelope) => envelope && envelope.consent)
            .then((value) => {
                if (
                    generation !== serial ||
                    configGeneration !== bootstrapGeneration()
                ) {
                    if (configGeneration === bootstrapGeneration()) {
                        // An unfenced native-cookie grant keeps this shared GET
                        // valid. Follow its current consent reader rather than
                        // overwriting a ready reader with a denied placeholder.
                        return loadConsent();
                    }
                    return {available: false, granted: false, capture_allowed: false};
                }
                choice =
                    value &&
                    (value.available === true || value.informational_notice === true)
                        ? {...value}
                        : {
                              available: false,
                              granted: false,
                              informational_notice: false,
                              capture_allowed: false,
                          };
                if (withdrawalPending) {
                    // A cross-tab revocation cannot grant consent through GET.
                    // The independent operator mode still comes from the server.
                    choice.granted = false;
                    choice.capture_allowed = Boolean(
                        choice.informational_notice && choice.capture_allowed
                    );
                }
                if (value && !choice.granted) {
                    clearOptionalSession();
                }
                notify("consent-ready", choice);
                return choice;
            })
            .catch(() => ({available: false, granted: false}));
    }
    return pending;
}

document.addEventListener("marketing_center:consent-changed", (event) => {
    pending = null;
    if (
        choice &&
        !(event.detail?.confirmed === true && event.detail.granted === true)
    ) {
        choice = {
            ...choice,
            granted: false,
            capture_allowed:
                choice.informational_notice === true && choice.capture_allowed === true,
        };
    }
});

function receiveWithdrawal() {
    ++serial;
    invalidateBootstrap(true);
    withdrawalPending = true;
    pending = null;
    if (choice) {
        choice = {
            ...choice,
            granted: false,
            capture_allowed:
                choice.informational_notice === true && choice.capture_allowed === true,
        };
        clearOptionalSession();
    }
    notify("consent-changed", {...choice, granted: false, confirmed: false});
    loadConsent();
}
if (channel) {
    channel.addEventListener("message", (event) => {
        if (event.data === "withdrawn") {
            receiveWithdrawal();
        }
    });
} else {
    window.addEventListener("storage", (event) => {
        if (event.key === WITHDRAWAL_SIGNAL && event.newValue) {
            receiveWithdrawal();
        }
    });
}

export async function submitConsent(granted) {
    const sequence = ++serial;
    // Native cookie choices cannot revoke an independently configured legal
    // basis. Fence a grant only when an individual-consent policy is known.
    if (!granted || choice?.available === true) invalidateBootstrap(true);
    pending = null;
    let utmsCleared = false;
    if (granted && choice?.available === true) {
        if (choice)
            choice = {
                ...choice,
                granted: false,
                capture_allowed:
                    choice.informational_notice === true &&
                    choice.capture_allowed === true,
            };
        notify("consent-changed", {...choice, granted: false, confirmed: false});
    }
    if (!granted) {
        broadcastWithdrawal();
        withdrawalPending = true;
        pending = null;
        if (choice) {
            choice = {
                ...choice,
                granted: false,
                capture_allowed:
                    choice.informational_notice === true &&
                    choice.capture_allowed === true,
            };
            clearOptionalSession();
        }
        // Unknown policy cannot justify erasing first-party storage.
        // Fail closed immediately in this document, even if the request fails.
        notify("consent-changed", {...choice, granted: false, confirmed: false});
    }
    const previous = decisionQueue;
    let release;
    decisionQueue = new Promise((resolve) => {
        release = resolve;
    });
    await previous;
    try {
        const config = choice || (await loadConsent());
        if (sequence !== serial) return false;
        if (
            !granted &&
            config.available === true &&
            config.informational_notice !== true
        ) {
            // Explicit individual refusal only; never a failed GET or a site
            // whose native cookie bar is disabled / policy is informational.
            clearNativeUtms();
            utmsCleared = true;
        }
        if (granted && !config.available) {
            // Informational capture has no grant POST. Refresh here as well on
            // Blog/jobs and pages without a Google measurement consumer.
            await loadConsent();
            return false;
        }
        const response = await window
            .fetch(DECISION, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                    "X-Marketing-Consent": "1",
                },
                body: JSON.stringify({
                    granted,
                    config_revision: config.config_revision || 0,
                    policy_version: config.policy_version || "",
                    notice_version: config.notice_version || "",
                }),
            })
            .catch((error) => {
                if (!granted && config.informational_notice === true) return null;
                throw error;
            });
        const result = response && response.ok ? await response.json() : null;
        if (sequence !== serial) {
            return false;
        }
        if (
            result?.accepted !== true &&
            !granted &&
            config.informational_notice === true
        ) {
            choice = {...config, granted: false};
            pending = Promise.resolve(choice);
            notify("consent-changed", {...choice, confirmed: false});
            await loadConsent();
            return false;
        }
        const accepted = Boolean(result && result.accepted === true);
        if (accepted && sequence === serial) {
            withdrawalPending = false;
        }
        const actualGrant = accepted && result.granted === true;
        choice = {
            ...config,
            granted: actualGrant,
            informational_notice: accepted && result.informational_notice === true,
            capture_allowed:
                accepted && (result.capture_allowed === true || actualGrant),
        };
        if (
            accepted &&
            !granted &&
            !actualGrant &&
            result.informational_notice !== true
        ) {
            // The accepted POST is authoritative if its preceding config GET
            // failed or the cached policy changed in the meantime.
            if (!utmsCleared) clearNativeUtms();
            clearOptionalSession(true);
        }
        pending = Promise.resolve(choice);
        notify("consent-changed", {...choice, confirmed: accepted});
        return accepted;
    } finally {
        release();
    }
}

publicWidget.registry.cookies_bar.include({
    _onAcceptClick(event) {
        const control =
            event.currentTarget ||
            event.target.closest("#cookies-consent-all, #cookies-consent-essential");
        const granted = control && control.id === "cookies-consent-all";
        // Odoo's native method reads target.id; a nested icon/span must retain
        // the meaning of its containing choice button.
        this._super({target: control || event.target});
        // The native handler has now stored the actual optional-cookie choice.
        const decision = submitConsent(granted);
        notify("native-cookie-choice", {granted, settled: decision});
        decision.catch(() =>
            notify("consent-changed", {
                ...(choice || {}),
                granted: false,
                confirmed: false,
            })
        );
    },
});

document.addEventListener("click", (event) => {
    const control =
        event.target.closest && event.target.closest("[data-marketing-consent-revoke]");
    if (!control || !event.isTrusted) {
        return;
    }
    event.preventDefault();
    setCookie(
        "website_cookies_bar",
        '{"required":true,"optional":false}',
        365 * 86400,
        "required"
    );
    submitConsent(false).finally(() => {
        // Reopen the existing native choice on reload; never generate a grant.
        deleteCookie("website_cookies_bar");
        window.location.reload();
    });
});

if (
    eligibleLandingPath(window.location.pathname) &&
    !document.body.classList.contains("editor_enable")
) {
    loadConsent();
}
