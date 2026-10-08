/** @odoo-module **/

import {loadConsent} from "@marketing_center_website/js/consent.esm";
import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";

let refreshPending = null;

export function refreshMeasurementConsent() {
    if (!refreshPending) {
        refreshPending = loadConsent(true).finally(() => {
            refreshPending = null;
        });
    }
    return refreshPending;
}

export function eligibleMeasurementConfig(config) {
    return Boolean(
        config &&
            /^[1-9][0-9]*$/.test(config.dataset.websiteId || "") &&
            window.location.protocol === "https:" &&
            config.dataset.path === window.location.pathname &&
            eligibleLandingPath(config.dataset.path) &&
            document.body &&
            !document.body.classList.contains("editor_enable")
    );
}
