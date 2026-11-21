"""Conservative snapshots for complete-suite operands; V3 stays unchanged."""

from build_body_worker import _CONVERTERS, _SCALARS


_MRO = type.__dict__['__mro__'].__get__
_NAMESPACE = type.__dict__['__dict__'].__get__


def method(klass, name):
    for base in _MRO(klass):
        namespace = _NAMESPACE(base)
        if name in namespace:
            return namespace[name]


_PRIMITIVE_INFO = {id(klass): (method(klass, '__eq__'), method(klass, '__ne__'))
                   for klass in (*_SCALARS, object)}


def snapshot(value):
    # Attribute lookup through the MRO bypasses both descriptor invocation and
    # metaclass hooks. Compare descriptors by identity, never equality/hash.
    klass = type(value)
    fixed = _PRIMITIVE_INFO.get(id(klass))
    if fixed is not None:
        eq, ne = fixed
    else:
        mro = _MRO(klass)
        primitive = next((base for base in mro
                          if any(base is scalar for scalar in _SCALARS)), object)
        eq, ne = method(klass, '__eq__'), method(klass, '__ne__')
        expected_eq, expected_ne = _PRIMITIVE_INFO[id(primitive)]
        if eq is not expected_eq or ne is not expected_ne:
            return id(value), ("unknown",)
    if eq is object.__eq__:
        return id(value), ('identity',)
    # Reuse V3's builtin-only conversion table and bounded serialization. Do
    # not call its generic _method: metaclass data descriptors can intercept
    # even type.__getattribute__(klass, '__dict__').
    scalar = None if value is None else _CONVERTERS[eq](value)
    name = type(scalar).__name__
    if scalar is None:
        name = 'none'
    elif type(scalar) is int:
        if int.bit_length(scalar) > 426:  # At most 129 decimal digits.
            return id(value), ('unknown',)
        scalar = str(scalar)
        if len(scalar) > 128:
            return id(value), ('unknown',)
    elif type(scalar) is float:
        scalar = scalar.hex()
    elif type(scalar) is bytes:
        if len(scalar) > 128:
            return id(value), ('unknown',)
        scalar = scalar.hex()
    elif type(scalar) is str and len(scalar) > 256:
        return id(value), ('unknown',)
    kind = 'scalar' if any(klass is scalar for scalar in _SCALARS) else 'scalar_subclass'
    return id(value), (kind, name, scalar)
