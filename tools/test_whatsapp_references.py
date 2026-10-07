"""Exercise reference parsing without an Odoo database or external services."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

path = (
    Path(__file__).resolve().parents[1]
    / "marketing_center_website_whatsapp"
    / "services"
    / "references.py"
)
spec = importlib.util.spec_from_file_location("whatsapp_references", path)
references = importlib.util.module_from_spec(spec)
spec.loader.exec_module(references)


class TestWhatsAppReferences(unittest.TestCase):
    def test_message_round_trip(self):
        message = references.reference_message(
            "Olá! Vim pelo site da Soloz e gostaria de mais informações.",
            "WX2Y",
        )
        self.assertEqual(message.splitlines()[-1], "Meu código é WX2Y")
        self.assertEqual(references.extract_reference(message), "WX2Y")
        self.assertEqual(references.reference_message("", "WX2Y"), "Meu código é WX2Y")

    def test_legacy_round_trip_and_bare_code(self):
        code = "CP-23456789ABCD"
        self.assertEqual(references.extract_reference(code), code)
        self.assertEqual(
            references.extract_reference(references.reference_message("Olá!", code)),
            code,
        )

    def test_label_accepts_ascii_and_case_variations(self):
        for text in ("Meu código é WX2Y.", "meu codigo e WX2Y", "MEU CÓDIGO É WX2Y"):
            with self.subTest(text=text):
                self.assertEqual(references.extract_reference(text), "WX2Y")

    def test_short_code_does_not_match_ordinary_unlabelled_words(self):
        self.assertFalse(
            references.extract_reference("Quero informações sobre SOLO e WX2Y.")
        )
        self.assertFalse(
            references.has_reference_hint("Quero informações sobre SOLO e WX2Y.")
        )

    def test_product_code_question_is_ordinary_text(self):
        question = "Qual o código do produto?"
        self.assertFalse(references.has_reference_hint(question))
        self.assertEqual(
            references.extract_reference("Meu código é WX2Y. " + question), "WX2Y"
        )

    def test_generator_uses_four_unambiguous_consonants_or_digits(self):
        for _ in range(100):
            code = references.new_reference()
            self.assertEqual(len(code), 4)
            self.assertTrue(set(code) <= set("23456789BCDFGHJKLMNPQRSTVWXYZ"))

    def test_invalid_multiple_and_partial_codes_block_temporal_inference(self):
        for text in (
            "Meu código é WX2",
            "Meu código é WX2Y7",
            "Meu código é wx2y",
            "Meu código é WX2Y_",
            "Meu código é WX2YÉ",
            "Meu código é WX2Y-7",
            "Meu código é ???",
            "Meu código é WX2Y. Meu código é AB23",
            "Meu código é WX2Y. Meu código é ???",
            "Meu código é WX2Y. Referência: ???",
            "Meu código é WX2Y. CP-23456789ABCD",
            "Referência: CP-23456789ABC",
            "Referência: cp-23456789abcd",
            "CP-23456789ABCD CP-23456789ABCD",
            "CP-23456789ABCD CP-ZZZZZZZZZZZ",
        ):
            with self.subTest(text=text):
                self.assertFalse(references.extract_reference(text))
                self.assertTrue(references.has_reference_hint(text))


if __name__ == "__main__":
    unittest.main()
