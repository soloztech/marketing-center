/** @odoo-module **/

import {eligibleLandingPath} from "@marketing_center_website/js/landing_capture.esm";

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
