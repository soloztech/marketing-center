from odoo.addons.marketing_center_website.tests.test_js import TestMarketingWebsiteJS


class TestWebsiteWhatsappJourneyJS(TestMarketingWebsiteJS):
    _qunit_addon = "marketing_center_website_whatsapp"
    _qunit_modules = ("marketing_center_website_whatsapp > visitor journey",)
