import pytest

from code2flow.model import Variable, Node, Group, GROUP_TYPE


def test_variable_init_requires_token_and_points_to():
    # token must be truthy
    with pytest.raises(ValueError):
        Variable('', 'x')

    # points_to must not be None
    with pytest.raises(ValueError):
        Variable('a', None)

    # falsy-but-valid points_to (e.g., empty dict) is accepted
    v = Variable('a', {})
    assert v.points_to == {}


def test_variable_repr_and_to_string_with_string_points_to():
    v = Variable('var1', 'module.name')
    r = repr(v)
    assert '<Variable token=var1' in r
    assert 'module.name' in r
    assert v.to_string() == 'var1->module.name'


def test_variable_to_string_with_group_and_node():
    grp = Group('myfile.py', GROUP_TYPE.FILE, 'File')
    node = Node('func', calls=[], variables=[], parent=grp)

    # Variable pointing to a Group uses the group's token
    vg = Variable('x', grp)
    assert vg.to_string() == 'x->myfile.py'

    # Variable pointing to a Node uses the node's token
    vn = Variable('y', node)
    assert vn.to_string() == 'y->func'
    rep = repr(vn)
    assert '<Variable token=y' in rep
    assert 'Node token=func' in rep


def test_variable_accepts_falsy_non_none_points_to():
    # Zero is falsy but allowed
    v = Variable('z', 0)
    assert v.points_to == 0
    assert v.to_string() == 'z->0'
