import json
import re
from urllib.parse import urlencode

from odoo.tests import HttpCase, no_retry, tagged


@tagged("post_install", "-at_install", "marketing_qunit")
class TestMarketingCatalogContactCenterJS(HttpCase):
    """Run this addon's real suites; missing or skipped modules cannot pass."""

    _qunit_modules = (
        "Content Catalog composer bridge",
        "Content Catalog side panel",
    )

    @no_retry
    def test_js_minified(self):
        self._run_qunit("minified")

    @no_retry
    def test_js_debug_assets(self):
        self._run_qunit("assets")

    def _run_qunit(self, mode):
        module_filter = "|".join(re.escape(name) for name in self._qunit_modules)
        query = urlencode(
            {
                "mod": "marketing_center_catalog_contact_center",
                "filter": "/^(?:%s):/" % module_filter,
                "debug": "assets" if mode == "assets" else "",
            }
        )
        # browser_js awaits this promise even if Odoo logs its success signal
        # first. Check completed tests, not merely registered modules/assertions.
        # Odoo's own suite still rejects missing JS dependencies and assertions.
        code = """
            (async () => {
                const required = %s;
                const deadline = Date.now() + 180000;
                while (!document.querySelector("#qunit-testresult .total")) {
                    if (Date.now() > deadline) {
                        throw new Error("Marketing QUnit completion timed out");
                    }
                    await new Promise((resolve) => setTimeout(resolve, 50));
                }
                // Odoo enables hidepassed; QUnit detaches those result nodes.
                const hidden = document.querySelector("#qunit-urlconfig-hidepassed");
                if (hidden.checked) {
                    hidden.click();
                }
                const counts = Object.fromEntries(required.map((name) => [name, 0]));
                const passed = document.querySelectorAll("#qunit-tests > li.pass");
                for (const test of passed) {
                    if (["skipped", "todo"].some((c) => test.classList.contains(c))) {
                        continue;
                    }
                    const name = test.querySelector(".module-name")?.textContent;
                    if (Object.prototype.hasOwnProperty.call(counts, name)) {
                        counts[name]++;
                    }
                }
                for (const name of required) {
                    if (!counts[name]) {
                        throw new Error("Marketing QUnit ran zero tests for " + name);
                    }
                }
                const failed = document.querySelector("#qunit-testresult .failed");
                if (Number(failed.textContent)) {
                    throw new Error("Marketing QUnit has failed assertions");
                }
                console.log("Marketing QUnit completed test counts:",
                            JSON.stringify(counts));
            })()
        """ % json.dumps(
            self._qunit_modules
        )
        self.browser_js(
            "/web/tests?" + query,
            code,
            ready="Boolean(window.QUnit)",
            login="admin",
            timeout=240,
        )
        self._logger.info(
            "Marketing QUnit verified: marketing_center_catalog_contact_center %s", mode
        )
