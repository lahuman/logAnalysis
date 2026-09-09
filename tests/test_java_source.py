from __future__ import annotations

import unittest

from log_analyzer.source_code.java import enclosing_method_lines


class JavaSourceTests(unittest.TestCase):
    def test_comments_literals_annotations_and_nested_lambda_keep_full_method(self) -> None:
        source = '''class Service {
    @SuppressWarnings(value = {"unused", "unchecked"})
    public <T> void run(
        T value
    ) throws IllegalStateException {
        String text = "} fake() { ";
        char brace = '}';
        String block = """
            } not a method() {
            """;
        // } fake() {
        /* } more() { */
        Runnable work = () -> {
            if (value != null) {
                throw new IllegalStateException();
            }
        };
        work.run();
    }
}'''
        self.assertEqual((2, 19), enclosing_method_lines(source, 15))

    def test_line_number_selects_correct_overload_and_constructor(self) -> None:
        source = '''class Service {
    Service() {
        initialize();
    }
    void run() {
        first();
    }
    void run(String value) {
        second();
    }
}'''
        for line, expected in ((3, (2, 4)), (6, (5, 7)), (9, (8, 10))):
            with self.subTest(line=line):
                self.assertEqual(expected, enclosing_method_lines(source, line))

    def test_anonymous_class_method_is_innermost_method(self) -> None:
        source = '''class Service {
    void run() {
        Runnable task = new Runnable() {
            public void run() {
                fail();
            }
        };
        task.run();
    }
}'''
        self.assertEqual((4, 6), enclosing_method_lines(source, 5))
        self.assertEqual((2, 9), enclosing_method_lines(source, 8))

    def test_initializer_and_incomplete_method_use_fallback(self) -> None:
        self.assertIsNone(enclosing_method_lines("class C {\n static { fail(); }\n}", 2))
        self.assertIsNone(enclosing_method_lines("class C {\n void run() {\n fail();\n", 3))


if __name__ == "__main__":
    unittest.main()
