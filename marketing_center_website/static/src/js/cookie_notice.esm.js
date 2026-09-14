/** @odoo-module **/

import {loadConsent} from "@marketing_center_website/js/consent.esm";
import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";
import publicWidget from "web.public.widget";

const started = new WeakSet();
let refreshPending = null;

export function refreshMeasurementConsent() {
    if (!refreshPending) {
        refreshPending = loadConsent(true).finally(() => { refreshPending = null; });
    }
    return refreshPending;
}

export function eligibleMeasurementConfig(config) {
    return Boolean(config && /^[1-9][0-9]*$/.test(config.dataset.websiteId || "") &&
        window.location.protocol === "https:" &&
        config.dataset.path === window.location.pathname &&
        eligibleLandingPath(config.dataset.path) &&
        document.body && !document.body.classList.contains("editor_enable"));
}

function informational(bar) {
    // This attribute is rendered by QWeb before the native widget starts. The
    // notice does not depend on GA4, an asynchronous policy fetch, or its success.
    return bar?.dataset.marketingTrackingNotice === "1";
}

function dismissed(bar) {
    if (bar.dataset.marketingNoticeDismissed === "1") {
        return true;
    }
    try {
        return window.localStorage.getItem(bar.dataset.marketingNoticeStorageKey) === "1";
    } catch (_error) {
        return false;
    }
}

publicWidget.registry.cookies_bar.include({
    start() {
        const result = this._super(...arguments);
        if (informational(this.el)) {
            // An old native preference must neither suppress a changed notice
            // nor re-enable native persistence. Reset the popup lifecycle using
            // only this notice's separate dismissal key.
            clearTimeout(this.timeout);
            this._popupAlreadyShown = dismissed(this.el);
            if (!this._popupAlreadyShown) {
                this._bindPopup();
            }
            startCookieNotice(this.el);
        }
        return result;
    },
    _showPopup() {
        if (informational(this.el)) {
            if (!dismissed(this.el) && !this._popupAlreadyShown && this._canShowPopup()) {
                this.$target.find(".modal").modal("show");
                this.releaseFocus = this._trapFocus();
            }
            return;
        }
        return this._super(...arguments);
    },
    _onHideModal() {
        if (informational(this.el)) {
            this._popupAlreadyShown = true;
            this.releaseFocus && this.releaseFocus();
            this.releaseFocus = null;
            return;
        }
        return this._super(...arguments);
    },
});

export function startCookieNotice(bar) {
    if (!informational(bar) || started.has(bar) ||
        document.body?.classList.contains("editor_enable")) {
        return;
    }
    started.add(bar);
    // Delegation survives native widget recreation and edited inner markup.
    bar.addEventListener("click", (event) => {
        const button = event.target.closest?.(".marketing-cookie-notice-proceed");
        if (!button || !bar.contains(button)) {
            return;
        }
        event.preventDefault();
        bar.dataset.marketingNoticeDismissed = "1";
        try {
            window.localStorage.setItem(bar.dataset.marketingNoticeStorageKey, "1");
        } catch (_error) { /* The in-memory dismissal still works. */ }
        const modal = bar.querySelector(".modal");
        if (modal) {
            window.Modal.getOrCreateInstance(modal).hide();
        }
    });
}

function boot() {
    startCookieNotice(document.getElementById("website_cookies_bar"));
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, {once: true});
} else {
    boot();
}
