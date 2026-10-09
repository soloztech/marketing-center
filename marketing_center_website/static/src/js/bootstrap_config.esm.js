/** @odoo-module **/

const BOOTSTRAP_PATH = "/marketing/website/bootstrap-config";
let generation = 0;
let bootstrapPromise = null;
let individualCaptureBlocked = false;

export function bootstrapGeneration() {
    return generation;
}

export function invalidateBootstrap(blockIndividual = false) {
    generation += 1;
    bootstrapPromise = null;
    if (blockIndividual) individualCaptureBlocked = true;
}

export function captureModeHint() {
    const node = document.getElementById("marketing_measurement_config");
    const mode = node && node.dataset.captureMode;
    return mode === "native" || mode === "legacy" ? mode : null;
}

export function loadBootstrap(refresh = false) {
    if (refresh) invalidateBootstrap();
    if (!bootstrapPromise) {
        const requestedGeneration = generation;
        let response;
        try {
            response = window.fetch(BOOTSTRAP_PATH, {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                headers: {Accept: "application/json"},
            });
        } catch (_error) {
            response = Promise.resolve(null);
        }
        bootstrapPromise = Promise.resolve(response)
            .then((response) => (response.ok ? response.json() : null))
            .then((value) => {
                if (requestedGeneration !== generation || value?.schema_version !== 1) {
                    return null;
                }
                if (individualCaptureBlocked && value.consent?.available === true) {
                    // A new GET may precede the withdrawal POST in another tab.
                    // Only a confirmed decision can lift this local fence.
                    const consent = {...value.consent, granted: false};
                    if (consent.informational_notice !== true)
                        consent.capture_allowed = false;
                    const ingress =
                        value.ingress?.informational_notice === true
                            ? value.ingress
                            : {enabled: false};
                    return {...value, ingress, consent};
                }
                return value;
            })
            .catch(() => null);
    }
    return bootstrapPromise;
}

// This dependency is loaded before its readers. Invalidate before any listener
// starts reading, including externally dispatched and cross-tab withdrawals.
document.addEventListener("marketing_center:consent-changed", (event) => {
    const detail = event.detail || {};
    individualCaptureBlocked = !(detail.granted === true && detail.confirmed === true);
    invalidateBootstrap();
});
