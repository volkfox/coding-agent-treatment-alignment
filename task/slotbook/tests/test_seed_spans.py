import unittest

from slotbook.spans import Span, parse_span


class SpanTests(unittest.TestCase):
    def test_length(self):
        self.assertEqual(Span(3, 7).length, 4)

    def test_contains_start_but_not_end(self):
        span = Span(3, 7)
        self.assertTrue(span.contains(3))
        self.assertTrue(span.contains(6))
        self.assertFalse(span.contains(7))
        self.assertFalse(span.contains(2))

    def test_overlapping_spans(self):
        self.assertTrue(Span(3, 7).overlaps(Span(6, 9)))
        self.assertTrue(Span(3, 7).overlaps(Span(4, 5)))
        self.assertFalse(Span(3, 7).overlaps(Span(8, 9)))

    def test_touching_spans_do_not_overlap(self):
        self.assertFalse(Span(3, 7).overlaps(Span(7, 9)))
        self.assertFalse(Span(7, 9).overlaps(Span(3, 7)))

    def test_written_as_start_dash_end(self):
        self.assertEqual(str(Span(3, 7)), "3-7")
        self.assertEqual(parse_span("3-7"), Span(3, 7))
        self.assertEqual(parse_span(" 10-12\n"), Span(10, 12))

    def test_parse_rejects_other_text(self):
        for text in ("3", "3-", "a-b", "3 to 7", ""):
            with self.assertRaises(ValueError):
                parse_span(text)


if __name__ == "__main__":
    unittest.main()
