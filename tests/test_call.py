import pytest

from code2flow.model import Call

def test_call_repr_and_to_string():
    call = Call('my_function', owner_token='my_module', is_library=True)
    r = repr(call)
    assert '<Call owner_token=my_module' in r
    assert 'token=my_function' in r

def test_call_to_string_only_token():
    call = Call('my_function')
    assert call.to_string() == 'my_function()'

def test_call_to_string_with_owner():
    call = Call('my_function', owner_token='my_module')
    assert call.to_string() == 'my_module.my_function()'

def test_call_attr_with_owner():
    call = Call('my_function', owner_token='my_module')
    assert call.is_attr() is True

def test_call_attr_without_owner():
    call = Call('my_function')
    assert call.is_attr() is False



