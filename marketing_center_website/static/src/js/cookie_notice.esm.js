/** @odoo-module **/

import {loadConsent} from "@marketing_center_website/js/consent.esm";
import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";
import publicWidget from "web.public.widget";

const DISMISSAL_PREFIX = "marketing_center.website.notice.dismissed.";
const started = new WeakSet();
let refreshPending = null;

export function refreshMeasurementConsent() {
    // Both independent consumers can revalidate on focus without racing two
    // refresh requests. Neither consumer authorizes the other locally.
    if (!refreshPending) {
        refreshPending = loadConsent(true).finally(() => { refreshPending = null; });
    }
    return refreshPending;
}

// The server renders this element only for an anonymous public Website request
// with an active binding and an authorized host. Keep a second browser boundary
// for editor activation and stale/cross-page markup.
export function eligibleMeasurementConfig(config) {
    return Boolean(config && /^[1-9][0-9]*$/.test(config.dataset.websiteId || "") &&
        window.location.protocol === "https:" &&
        config.dataset.path === window.location.pathname &&
        eligibleLandingPath(config.dataset.path) &&
        document.body && !document.body.classList.contains("editor_enable"));
}

function dismissed(bar) {
    if (bar.dataset.marketingNoticeDismissed === "1") {
        return true;
    }
    try {
        return window.sessionStorage.getItem(bar.dataset.marketingNoticeStorageKey) === "1";
    } catch (_error) {
        return false;
    }
}

// Native popup dismissal normally persists a cookie choice and UTM cookies.
// An informational notice must only dismiss itself, including when storage is
// blocked. Leave the native modal focus cleanup and ordinary choice unchanged.
publicWidget.registry.cookies_bar.include({
    _showPopup() {
        if (this.el.dataset.marketingTrackingNotice === "1") {
            if (!dismissed(this.el) && !this._popupAlreadyShown && this._canShowPopup()) {
                this.$target.find(".modal").modal("show");
                this.releaseFocus = this._trapFocus();
            }
            return;
        }
        return this._super(...arguments);
    },
    _onHideModal() {
        if (this.el.dataset.marketingTrackingNotice === "1") {
            this._popupAlreadyShown = true;
            this.releaseFocus && this.releaseFocus();
            this.releaseFocus = null;
            return;
        }
        return this._super(...arguments);
    },
});

export function startCookieNotice(config) {
    if (!eligibleMeasurementConfig(config) || started.has(config)) {
        return;
    }
    started.add(config);
    const bar = document.getElementById("website_cookies_bar");
    const policy = bar?.querySelector(".o_cookies_bar_text_policy");
    const notice = policy?.previousElementSibling;
    const modal = bar?.querySelector(".modal");
    if (!notice || notice.tagName !== "SPAN" || !modal) {
        return;
    }
    const controls = Array.from(bar.querySelectorAll("#cookies-consent-essential, #cookies-consent-all"));
    const original = {
        text: notice.textContent,
        policyHref: policy.getAttribute("href"),
        ariaLabel: modal.getAttribute("aria-label"),
        hidden: controls.map((control) => control.classList.contains("d-none")),
    };
    bar.dataset.marketingNoticeStorageKey = DISMISSAL_PREFIX + config.dataset.websiteId;

    function update(value) {
        const informational = eligibleMeasurementConfig(config) &&
            value?.tracking_test_mode === true && value?.capture_allowed === true &&
            Boolean(config.dataset.cookieNotice?.trim() && config.dataset.cookieProceedLabel?.trim());
        const wasInformational = bar.dataset.marketingTrackingNotice === "1";
        let proceed = bar.querySelector(".marketing-cookie-notice-proceed");
        if (informational) {
            // Only plain text from the Website settings, never HTML injection.
            notice.textContent = config.dataset.cookieNotice;
            bar.dataset.marketingTrackingNotice = "1";
            const policyPath = config.dataset.cookiePolicyUrl || "";
            if (/^\/(?!\/)/.test(policyPath) && !/[\\\x00-\x1f\x7f]/.test(policyPath)) {
                policy.setAttribute("href", policyPath);
            }
            for (const control of controls) {
                // Preserve native IDs: Odoo uses them to detect cookie consent.
                control.classList.add("d-none");
            }
            if (!proceed && controls.length) {
                proceed = document.createElement("button");
                proceed.type = "button";
                proceed.className = "marketing-cookie-notice-proceed btn btn-primary";
                proceed.addEventListener("click", () => {
                    bar.dataset.marketingNoticeDismissed = "1";
                    try {
                        window.sessionStorage.setItem(bar.dataset.marketingNoticeStorageKey, "1");
                    } catch (_error) { /* The in-memory dismissal still works. */ }
                    window.Modal.getOrCreateInstance(modal).hide();
                });
                controls[0].parentElement.appendChild(proceed);
            }
            if (proceed) {
                proceed.textContent = config.dataset.cookieProceedLabel;
            }
            if (dismissed(bar)) {
                window.Modal.getOrCreateInstance(modal).hide();
            } else if (!wasInformational) {
                window.Modal.getOrCreateInstance(modal).show();
            }
        } else if (wasInformational) {
            try {
                window.sessionStorage.removeItem(bar.dataset.marketingNoticeStorageKey);
            } catch (_error) { /* Native preference remains authoritative. */ }
            delete bar.dataset.marketingNoticeDismissed;
            const needsChoice = eligibleMeasurementConfig(config) &&
                !/(^|;\s*)website_cookies_bar=/.test(document.cookie);
            if (!needsChoice) {
                // Hide while the informational guard still blocks cookie writes.
                window.Modal.getOrCreateInstance(modal).hide();
            }
            delete bar.dataset.marketingTrackingNotice;
            notice.textContent = original.text;
            for (const [element, attribute, value] of [
                [policy, "href", original.policyHref],
                [modal, "aria-label", original.ariaLabel],
            ]) {
                if (value === null) {
                    element.removeAttribute(attribute);
                } else {
                    element.setAttribute(attribute, value);
                }
            }
            controls.forEach((control, index) => control.classList.toggle("d-none", original.hidden[index]));
            proceed?.remove();
            if (needsChoice) {
                // Avoid hide/show during a Bootstrap transition: leave an open
                // dialog visible while restoring the real native choices.
                window.Modal.getOrCreateInstance(modal).show();
            }
        }
    }

    document.addEventListener("marketing_center:consent-ready", (event) => update(event.detail));
    document.addEventListener("marketing_center:consent-changed", (event) => {
        if (event.detail?.confirmed === true) {
            update(event.detail);
        }
    });
    window.addEventListener("focus", () => {
        refreshMeasurementConsent().then(update).catch(() => update(null));
    });
    loadConsent().then(update).catch(() => update(null));
}

function boot() {
    startCookieNotice(document.getElementById("marketing_measurement_config"));
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, {once: true});
} else {
    boot();
}
