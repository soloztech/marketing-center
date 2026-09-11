from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import Form, TransactionCase


@tagged("post_install", "-at_install")
class TestCatalogFaqEditing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Subject = cls.env["marketing.center.catalog.subject"]
        cls.Item = cls.env["marketing.center.catalog.item"]
        cls.subject = cls.Subject.create({"name": "FAQ editing fixture"})

    def test_quick_create_uses_question_as_title_and_keeps_internal_default(self):
        values = {
            "subject_id": self.subject.id,
            "kind": "faq",
            "question": "Where can I find the warranty?",
            "answer": "<p>See the <strong>order terms</strong>.</p>",
        }
        item = self.Item.create(values)
        self.assertEqual(item.name, values["question"])
        self.assertEqual(item.visibility, "internal")
        self.assertIn("<strong>order terms</strong>", item.answer)
        self.assertNotIn("name", values)

        item = self.Item.with_context(default_kind="faq").create(
            {
                "subject_id": self.subject.id,
                "question": "Where is the manual?",
                "answer": "<p>In the materials tab.</p>",
            }
        )
        self.assertEqual(item.kind, "faq")
        self.assertEqual(item.name, item.question)

    def test_inline_faq_can_be_created_without_a_separate_title(self):
        with Form(self.Subject) as subject:
            subject.name = "Inline FAQ fixture"
            with subject.faq_ids.new() as faq:
                faq.question = "What is included?"
                faq.answer = "<p>See the product specifications.</p>"
        item = subject.save().faq_ids
        self.assertEqual(len(item), 1)
        self.assertEqual(item.name, "What is included?")
        self.assertEqual(item.kind, "faq")

    def test_question_edits_preserve_custom_titles_and_formatted_answers(self):
        item = self.Item.create(
            {
                "name": "Warranty overview",
                "subject_id": self.subject.id,
                "kind": "faq",
                "question": "What is the warranty?",
                "answer": "<p>Original answer.</p>",
            }
        )
        with Form(self.subject) as subject:
            with subject.faq_ids.edit(0) as faq:
                faq.question = "Where can I find the warranty terms?"
                faq.answer = "<p>See the <strong>order terms</strong>.</p>"
        self.assertEqual(item.name, "Warranty overview")
        self.assertEqual(item.question, "Where can I find the warranty terms?")
        self.assertIn("<strong>order terms</strong>", item.answer)

    def test_quick_create_still_requires_question_and_nonempty_answer(self):
        values = {"subject_id": self.subject.id, "kind": "faq"}
        for content in (
            {"answer": "<p>An answer alone is not a FAQ.</p>"},
            {"question": "Where is the answer?", "answer": "<p><br></p>"},
        ):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.Item.create(dict(values, **content))
