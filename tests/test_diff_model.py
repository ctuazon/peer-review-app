from app.diff_model import build_file_diffs, parse_file_patch
from app.globs import matches

PATCH = """@@ -10,4 +10,5 @@ class Foo
 keep one
-old two
+new two
+new three
 keep four
@@ -40,2 +41,2 @@ function bar()
-gone
+here
 tail"""


def test_accepts_right_added_and_context_lines():
    diff = parse_file_patch("a.php", PATCH)
    assert diff.accepts(10, "RIGHT")  # context
    assert diff.accepts(11, "RIGHT")  # added
    assert diff.accepts(13, "RIGHT")  # context after adds
    assert not diff.accepts(14, "RIGHT")  # between hunks
    assert not diff.accepts(None, "RIGHT")


def test_accepts_left_deleted_and_context_lines():
    diff = parse_file_patch("a.php", PATCH)
    assert diff.accepts(11, "LEFT")  # deleted "old two"
    assert diff.accepts(10, "LEFT")  # context
    assert diff.accepts(40, "LEFT")
    assert diff.accepts(12, "LEFT")  # context 'keep four' is old line 12
    assert not diff.accepts(13, "LEFT")


def test_annotated_numbers_every_line():
    text = parse_file_patch("a.php", PATCH).annotated()
    assert "--- a.php" in text
    lines = text.splitlines()
    assert "R10     keep one" in lines
    assert "L11    -old two" in lines
    assert "R11    +new two" in lines
    assert "L40    -gone" in lines
    assert "R41    +here" in lines


def test_enclosing_symbol_from_hunk_header():
    diff = parse_file_patch("a.php", PATCH)
    assert diff.enclosing_symbol(11) == "class Foo"
    assert diff.enclosing_symbol(41) == "function bar()"
    assert diff.enclosing_symbol(30) is None


def test_map_forward_through_hunks():
    diff = parse_file_patch("a.php", PATCH)
    assert diff.map_forward(5) == 5  # before any hunk
    assert diff.map_forward(10) == 10  # context
    assert diff.map_forward(11) is None  # deleted
    assert diff.map_forward(12) == 13  # context shifted by the extra add
    assert diff.map_forward(20) == 21  # between hunks: +1 offset
    assert diff.map_forward(40) is None
    assert diff.map_forward(41) == 42  # 'tail'
    assert diff.map_forward(100) == 101


def test_right_lines_requires_every_line_in_diff():
    diff = parse_file_patch("a.php", PATCH)
    assert diff.right_lines(11, 12) == ["new two", "new three"]
    assert diff.right_lines(12, 14) is None


def test_build_file_diffs_marks_binary():
    diffs = build_file_diffs([{"filename": "img.png", "status": "added"}, {"filename": "a.php", "patch": PATCH}])
    assert diffs["img.png"].is_binary
    assert not diffs["a.php"].is_binary


def test_globs():
    assert matches("**/*.lock", "composer.lock")
    assert matches("**/*.lock", "a/b/yarn.lock")
    assert matches("**/vendor/**", "vendor/x/y.php")
    assert matches("src/Api/**", "src/Api/Http/Foo.php")
    assert not matches("src/*.php", "src/a/b.php")
    assert matches("**/.env.*", ".env.local")
