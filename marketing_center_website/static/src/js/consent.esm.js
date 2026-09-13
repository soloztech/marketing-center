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

function notify(name, detail) {
    document.dispatchEvent(new CustomEvent(`marketing_center:${name}`, {detail}));
}

function clearOptionalSession() {
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
                    value && value.available === true
                        ? value
                        : {available: false, granted: false};
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
        pending = null;
        choice = {...(choice || {}), granted: false};
        clearOptionalSession();
        // Fail closed immediately in this document, even if the request fails.
        notify("consent-changed", {granted: false, confirmed: false});
    }
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
    const actualGrant = accepted && result.granted === true;
    choice = {...config, granted: actualGrant};
    pending = Promise.resolve(choice);
    notify("consent-changed", {...choice, confirmed: accepted});
    return accepted;
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
