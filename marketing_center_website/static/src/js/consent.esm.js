/** @odoo-module **/

import publicWidget from "web.public.widget";
import "website.s_popup";
import {deleteCookie, setCookie} from "web.utils.cookies";
import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";

const CONFIG = "/marketing/website-consent/config";
const DECISION = "/marketing/website-consent/decision";
const STORAGE_PREFIX = "marketing_center.website.v1";
let pending = null;
let choice = null;
let serial = 0;
let decisionQueue = Promise.resolve();
let withdrawalPending = false;
const WITHDRAWAL_SIGNAL = "marketing_center.consent.withdrawal";
const channel =
    typeof window.BroadcastChannel === "function"
        ? new window.BroadcastChannel(WITHDRAWAL_SIGNAL)
        : null;

function receiveWithdrawal() {
    ++serial;
    withdrawalPending = true;
    pending = null;
    choice = {...(choice || {}), granted: false};
    clearOptionalSession();
    notify("consent-changed", {...choice, granted: false, confirmed: false});
    loadConsent(true);
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

function clearOptionalSession() {
    // An explicit server informational policy preserves attribution independently of the
    // cookie choice. It never changes that choice or creates a grant.
    if (choice && choice.informational_notice === true) {
        return;
    }
    const hostname = window.location.hostname || "";
    const domains = /^[a-z0-9.-]+$/i.test(hostname)
        ? [hostname, `.${hostname}`]
        : [];
    for (const name of ["odoo_utm_campaign", "odoo_utm_source", "odoo_utm_medium"]) {
        deleteCookie(name);
        // Odoo's native server UTM cookie can have an explicit Domain. Its
        // legacy deleteCookie helper removes only the host-cookie scope.
        for (const domain of domains) {
            document.cookie = `${name}=; Max-Age=0; Path=/; Domain=${domain}; Secure; SameSite=Lax`;
        }
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
    if (!pending || refresh) {
        const generation = serial;
        pending = window
            .fetch(CONFIG, {
                credentials: "same-origin",
                cache: "no-store",
                headers: {Accept: "application/json"},
            })
            .then((response) => (response.ok ? response.json() : null))
            .then((value) => {
                if (generation !== serial) {
                    return choice || {available: false, granted: false};
                }
                choice =
                    value && (value.available === true || value.informational_notice === true)
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
                if (!choice.granted) {
                    clearOptionalSession();
                }
                notify("consent-ready", choice);
                return choice;
            })
            .catch(() => ({available: false, granted: false}));
    }
    return pending;
}

export async function submitConsent(granted) {
    const sequence = ++serial;
    if (!granted) {
        setCookie(
            "website_cookies_bar",
            '{"required":true,"optional":false}',
            999 * 86400,
            "required"
        );
        broadcastWithdrawal();
        withdrawalPending = true;
        pending = null;
        choice = {...(choice || {}), granted: false};
        clearOptionalSession();
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
        if (granted && !config.available) {
            return false;
        }
        const response = await window.fetch(DECISION, {
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
        });
        const result = response.ok ? await response.json() : null;
        if (sequence !== serial) {
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
            capture_allowed: accepted && (result.capture_allowed === true || actualGrant),
        };
        if (!actualGrant && config.informational_notice === true && !choice.informational_notice) {
            clearOptionalSession();
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
        if (this.el.dataset.marketingTrackingNotice === "1") {
            // Defensive against stale CMS markup: an informational notice never
            // delegates to Odoo's optional-cookie grant/persistence handler.
            event.preventDefault?.();
            return;
        }
        const control =
            event.currentTarget ||
            event.target.closest("#cookies-consent-all, #cookies-consent-essential");
        const granted = control && control.id === "cookies-consent-all";
        // Odoo's native method reads target.id; a nested icon/span must retain
        // the meaning of its containing choice button.
        this._super({target: control || event.target});
        // The native handler has now stored the actual optional-cookie choice.
        submitConsent(granted).catch(() =>
            notify("consent-changed", {granted: false, confirmed: false})
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
    if (document.getElementById("website_cookies_bar")?.dataset.marketingTrackingNotice === "1") {
        // A stale CMS shortcut cannot change cookies or reload a native choice
        // on a site whose server-rendered notice is informational.
        return;
    }
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
