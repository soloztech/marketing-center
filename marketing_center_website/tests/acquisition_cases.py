"""Shared acceptance cases run through each consumer's own snapshot builder."""
import datetime
import uuid

from odoo import fields

from ..services.acquisition import QUERY_FIELDS, utc_iso


class AcquisitionMatrixMixin:
    def _matrix_track(self, path, when, **values):
        return self.env["website.track"].create(
            {
                "visitor_id": self.visitor.id,
                "url": self.origin + path,
                "visit_datetime": when,
                **values,
            }
        )

    def test_acquisition_matrix_both_purposes(self):
        now = fields.Datetime.now()
        anchor_at = now - datetime.timedelta(minutes=2)
        entry_at = now - datetime.timedelta(minutes=5)
        # Clear this synthetic visitor so no prior case can become an anchor.
        cases = [
            ("referrer", True),
            ("referrer", False),
            ("track", True),
            ("track", False),
            ("cookie", True),
            ("cookie", False),
            ("none", True),
            ("none", False),
        ]
        for source, anchored in cases:
            with self.subTest(source=source, anchored=anchored):
                tracks = self.env["website.track"].search(
                    [("visitor_id", "=", self.visitor.id)]
                )
                tracks.unlink()
                referrer = self.origin + self.matrix_path
                if source == "referrer":
                    referrer += "?gclid=current-only"
                entry = self.env["website.track"]
                if source == "track":
                    entry = self._matrix_track(
                        "/matrix-entry?utm_campaign=preceding", entry_at
                    )
                anchor = self.env["website.track"]
                if anchored:
                    anchor = self._matrix_track(referrer[len(self.origin) :], anchor_at)
                cookies = (
                    {"odoo_utm_campaign": "old-cookie"}
                    if source in {"referrer", "cookie", "track"}
                    else {}
                )
                result = self._matrix_snapshot(referrer, now, cookies)
                provenance = source if source != "track" or anchored else "cookie"
                self.assertEqual(result["provenance"], provenance)
                self.assertEqual(
                    result["track"], entry if source == "track" and anchored else anchor
                )
                expected_time = None
                if anchored and (
                    source in {"referrer", "track"}
                    or (source == "none" and self.matrix_purpose == "form")
                ):
                    expected_time = entry_at if source == "track" else anchor_at
                self.assertEqual(
                    result["acquisition_at"],
                    utc_iso(expected_time) if expected_time else None,
                )
                self.assertEqual(
                    result["visit_at"], utc_iso(anchor_at) if anchored else None
                )
                self.assertEqual(
                    result["landing_url"],
                    self.origin + "/matrix-entry"
                    if source == "track" and anchored
                    else self.origin + self.matrix_path,
                )
                if source == "referrer":
                    self.assertEqual(result["values"], {"gclid": "current-only"})
                elif source in {"cookie", "track"}:
                    self.assertEqual(
                        result["values"],
                        {
                            "utm_campaign": "preceding"
                            if source == "track" and anchored
                            else "old-cookie"
                        },
                    )
                else:
                    self.assertEqual(result["values"], {})

    def test_acquisition_all_eleven_fields_and_partial_tuple_ownership(self):
        now = fields.Datetime.now()
        values = {
            name: (
                "123" if name in {"gad_campaignid", "gad_source"} else "value-" + name
            )
            for name in QUERY_FIELDS
        }
        query = "&".join(name + "=" + value for name, value in values.items())
        referrer = self.origin + self.matrix_path + "?" + query
        self._matrix_track(self.matrix_path + "?" + query, now)
        result = self._matrix_snapshot(referrer, now, {"odoo_utm_campaign": "old"})
        self.assertEqual(result["values"], values)
        partial = self._matrix_snapshot(
            self.origin + self.matrix_path + "?utm_source=new",
            now,
            {"odoo_utm_campaign": "old"},
        )
        self.assertEqual(partial["values"], {"utm_source": "new"})
        self.assertIsNone(partial["acquisition_at"])

    def test_acquisition_requires_anchor_and_ignores_future_and_old_history(self):
        now = fields.Datetime.now()
        self._matrix_track(
            "/expired?gclid=old", now - datetime.timedelta(hours=24, seconds=1)
        )
        anchor = self._matrix_track(
            self.matrix_path, now - datetime.timedelta(minutes=1)
        )
        self._matrix_track("/other-tab?gclid=later", now)
        self._matrix_track(
            self.matrix_path + "?gclid=future", now + datetime.timedelta(seconds=1)
        )
        result = self._matrix_snapshot(self.origin + self.matrix_path, now, {})
        self.assertEqual(result["provenance"], "none")
        self.assertEqual(result["track"], anchor)
        self.assertEqual(result["values"], {})

    def test_acquisition_200_track_bound_and_same_time_id_order(self):
        now = fields.Datetime.now()
        before = self._matrix_track("/before?gclid=before", now)
        self._matrix_track(self.matrix_path, now)
        self._matrix_track("/after?gclid=after", now)
        result = self._matrix_snapshot(self.origin + self.matrix_path, now, {})
        self.assertEqual(result["track"], before)
        self.assertEqual(result["values"], {"gclid": "before"})
        for i in range(200):
            self._matrix_track("/bounded-%s" % i, now)
        result = self._matrix_snapshot(self.origin + self.matrix_path, now, {})
        self.assertFalse(result["track"])
        self.assertEqual(result["provenance"], "none")
        self.assertIsNone(result["acquisition_at"])

    def test_acquisition_cookie_next_day_has_visit_but_no_click_time(self):
        now = fields.Datetime.now()
        anchor = self._matrix_track(
            self.matrix_path, now - datetime.timedelta(hours=23)
        )
        result = self._matrix_snapshot(
            self.origin + self.matrix_path, now, {"odoo_utm_campaign": "old"}
        )
        self.assertEqual(result["track"], anchor)
        self.assertEqual(result["provenance"], "cookie")
        self.assertIsNone(result["acquisition_at"])
        self.assertEqual(result["visit_at"], utc_iso(anchor.visit_datetime))

    def test_acquisition_same_origin_other_website_tracks_do_not_displace_anchor(self):
        now = fields.Datetime.now()
        other = self.env["website"].create(
            {
                "name": "Acquisition other Website",
                "company_id": self.website.company_id.id,
                "domain": "https://acquisition-other.invalid",
            }
        )
        key = "marketing_center_website.matrix_" + uuid.uuid4().hex
        view = self.env["ir.ui.view"].create(
            {
                "name": "Other Website matrix",
                "type": "qweb",
                "key": key,
                "arch_db": '<t t-name="%s"><div>Other Website</div></t>' % key,
            }
        )
        page = self.env["website.page"].create(
            {
                "name": "Other Website matrix",
                "url": self.matrix_path,
                "view_id": view.id,
                "website_id": other.id,
                "is_published": True,
            }
        )
        # The acquisition boundary is inclusive at exactly 24 hours.
        entry = self._matrix_track(
            "/boundary?gclid=boundary", now - datetime.timedelta(hours=24)
        )
        anchor = self._matrix_track(
            self.matrix_path, now - datetime.timedelta(minutes=1)
        )
        for i in range(201):
            self._matrix_track(
                self.matrix_path + "?gclid=other-site", now, page_id=page.id
            )
            self.env["website.track"].create(
                {
                    "visitor_id": self.visitor.id,
                    "visit_datetime": now,
                    "url": "https://foreign.invalid/%s?gclid=foreign" % i,
                }
            )
        result = self._matrix_snapshot(self.origin + self.matrix_path, now, {})
        self.assertEqual(result["track"], entry)
        self.assertEqual(result["values"], {"gclid": "boundary"})
        self.assertEqual(result["visit_at"], utc_iso(anchor.visit_datetime))

    def test_acquisition_no_anchor_forbids_history_without_cookie_fallback(self):
        now = fields.Datetime.now()
        self._matrix_track("/unanchored-entry?gclid=unanchored", now)
        result = self._matrix_snapshot(self.origin + self.matrix_path, now, {})
        self.assertEqual(result["values"], {})
        self.assertEqual(result["provenance"], "none")
        self.assertFalse(result["track"])
        self.assertIsNone(result["acquisition_at"])
        self.assertEqual(result["landing_url"], self.origin + self.matrix_path)
