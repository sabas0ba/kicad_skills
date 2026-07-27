from eda_toolkit.kicad import sexpr
from eda_toolkit.kicad.sexpr import SNode, SexprError

import pytest


def test_parses_nested_lists_and_types():
    node = sexpr.loads('(root (version 20231120) (flag yes) (off no) (pos 1.5 -2 3))')
    assert node.name == "root"
    assert node.value("version") == 20231120
    assert isinstance(node.value("version"), int)
    assert node.flag("flag") is True
    assert node.flag("off") is False
    assert node.child("pos").atoms() == [1.5, -2, 3]


def test_quoted_strings_and_escapes():
    node = sexpr.loads(r'(root (property "Value" "10 \"k\" \\ ohm") (empty ""))')
    assert node.value("property", 1) == '10 "k" \\ ohm'
    assert node.value("empty") == ""


def test_walk_finds_nested_nodes():
    node = sexpr.loads("(a (b (c (pin 1)) (pin 2)) (pin 3))")
    assert len(list(node.walk("pin"))) == 3


def test_children_filtering_and_missing_values():
    node = sexpr.loads("(a (b 1) (b 2) (c 3))")
    assert len(node.children("b")) == 2
    assert node.value("missing", default="fallback") == "fallback"
    assert node.child("missing") is None


@pytest.mark.parametrize("text", ["(a", "a)", "", "(a \"unterminated"])
def test_malformed_documents_raise(text):
    with pytest.raises(SexprError):
        sexpr.loads(text)


def test_dumps_round_trip():
    original = '(kicad_sch (version 20231120) (paper "A4") (wire (pts (xy 1 2) (xy 3 4))))'
    node = sexpr.loads(original)
    reparsed = sexpr.loads(sexpr.dumps(node))
    assert reparsed.value("version") == 20231120
    assert reparsed.child("wire").child("pts").children("xy")[1].atoms() == [3, 4]


def test_dumps_quotes_when_needed():
    node = SNode("prop", ["Value", "10 k", True, 1.5])
    assert sexpr.dumps(node) == '(prop Value "10 k" yes 1.5)'
