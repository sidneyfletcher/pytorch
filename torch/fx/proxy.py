import dis
import torch
import inspect
import operator

from .graph import magic_methods, reflectable_magic_methods, Graph
from typing import Tuple, Dict, Optional, Iterable, Any, Iterator
from .node import Target, Node, Argument, base_types

class TracerBase:
    graph: Graph

    def create_node(self, kind : str, target : Target,
                    args : Tuple[Argument, ...], kwargs : Dict[str, Argument], name : Optional[str] = None,
                    type_expr : Optional[Any] = None) -> Node:
        """
        Inserts a graph node given target, args, kwargs, and name.

        This method can be overridden to do extra checking, validation, or
        modification of values used in node creation. For example, one might
        want to disallow in-place operations from being recorded.
        """
        return self.graph.create_node(kind, target, args, kwargs, name, type_expr)

    def proxy(self, node: Node) -> 'Proxy':
        return Proxy(node, self)

    def create_proxy(self, kind: str, target: Target, args: Tuple[Any, ...], kwargs: Dict[str, Any],
                     name: Optional[str] = None, type_expr : Optional[Any] = None):
        '''
        Create a Node from the given arguments, then return the Node
        wrapped in a Proxy object.

        If kind = 'placeholder', then we're creating a Node that
        represents the parameter of a function. If we need to encode
        a default parameter, we use the `args` tuple. `args` is
        otherwise empty for `placeholder` Nodes.
        '''
        args_ = self.create_arg(args)
        kwargs_ = self.create_arg(kwargs)
        assert isinstance(args_, tuple)
        assert isinstance(kwargs_, dict)
        return self.proxy(self.create_node(kind, target, args_, kwargs_, name, type_expr))

    def create_arg(self, a: Any) -> Argument:
        """
        A method that lowers the objects seen as arguments during symbolic evaluation
        into Argument types that can be stored in IR.

        Can be override to support more trace-specific types.
        """
        # aggregates
        if isinstance(a, (tuple, list)):
            return type(a)(self.create_arg(elem) for elem in a)
        elif isinstance(a, dict):
            r = {}
            for k, v in a.items():
                if not isinstance(k, str):
                    raise NotImplementedError(f"dictionaries with non-string keys: {a}")
                r[k] = self.create_arg(v)
            return r
        elif isinstance(a, slice):
            return slice(self.create_arg(a.start), self.create_arg(a.stop), self.create_arg(a.step))

        if isinstance(a, Proxy):
            # base case: we unwrap the Proxy object
            return a.node
        elif isinstance(a, base_types) or a is None:
            return a

        raise NotImplementedError(f"argument of type: {type(a)}")

    def to_bool(self, obj: 'Proxy') -> bool:
        """Called when a proxy object is being converted to a boolean, such as
        when used in control flow.  Normally we don't know what to do because
        we don't know the value of the proxy, but a custom tracer can attach more
        information to the graph node using create_node and can choose to return a value.
        """
        raise TraceError('symbolically traced variables cannot be used as inputs to control flow')

    def iter(self, obj: 'Proxy') -> Iterator:
        """Called when a proxy object is being iterated over, such as
        when used in control flow.  Normally we don't know what to do because
        we don't know the value of the proxy, but a custom tracer can attach more
        information to the graph node using create_node and can choose to return an iterator.
        """
        raise TraceError('Proxy object cannot be iterated. '
                         'This can be attempted when used in a for loop or as a *args or **kwargs function argument.')

    def keys(self, obj: 'Proxy') -> Any:
        """Called when a proxy object is has the keys() method called.
        This is what happens when ** is called on a proxy. This should return an
        iterator it ** is suppose to work in your custom tracer.
        """
        return Attribute(obj, 'keys')()


# used in Proxy object when just appending to the graph while not tracing.
class GraphAppendingTracer(TracerBase):
    def __init__(self, graph: Graph):
        super().__init__()
        self.graph = graph

class TraceError(ValueError):
    pass


def _create_friendly_names(a: Any, frames_up : int):
    """
    Given an args/kwargs object, go through and try to pull out the names for
    each contained Proxy from the Python interpreter frame `frames_up` above
    us in the stack. If found, assign that name to the Proxy's underlying Node's
    unique name
    """
    frame = inspect.currentframe()
    if not frame:
        raise RuntimeError("failed to inspect frame")

    i = 0
    while i < frames_up + 1:
        frame = frame.f_back
        if not frame:
            raise RuntimeError("failed to get frame")
        i += 1

    f_locals = frame.f_locals

    def _assign_friendly_name(a : Any):
        # aggregates
        if isinstance(a, (tuple, list)):
            for elem in a:
                _assign_friendly_name(elem)
        elif isinstance(a, dict):
            for k, v in a.items():
                if not isinstance(k, str):
                    raise NotImplementedError(f"dictionaries with non-string keys: {a}")
                _assign_friendly_name(v)
        elif isinstance(a, slice):
            _assign_friendly_name(a.start)
            _assign_friendly_name(a.stop)
            _assign_friendly_name(a.step)

        if isinstance(a, Proxy):
            # Base case: look for Proxy objects in locals:

            found_name : Optional[str] = None
            for k, v in f_locals.items():
                if a is v:
                    # Arbitrary tie-breaker: use the shortest name found in the
                    # frame. This will account for cases e.g. `x` and `identity`
                    # in the same frame (as in ResNet). This tie-breaker makes it
                    # so that we use a consistent name. TODO: Futher introspect
                    # into Python bytecode state to get the actual name used?
                    if not found_name or len(k) < len(found_name):
                        found_name = k
            if found_name is not None and found_name != a.node.name:
                a.node.name = a.tracer.graph._create_unique_name(found_name)

    _assign_friendly_name(a)


# Proxy objects are stand-in values for normal values in a PyTorch computation.
# Instead of performing compute they record computation into Graph.
# Each proxy wraps the Node instance that represents the expression that define the
# value.

class Proxy:
    def __init__(self, node: Node, tracer: 'Optional[TracerBase]' = None):
        if tracer is None:
            # this allows you to create a proxy object around a raw node
            # so that if you are doing graph transforms you can use the overloaded operators
            # to add additional things to a graph.
            tracer = GraphAppendingTracer(node.graph)
        self.tracer = tracer
        self.node = node

    def __repr__(self) -> str:
        return f'Proxy({self.node.name})'

    def __getattr__(self, k) -> 'Attribute':
        # note: not added to the graph yet, if this is a method call
        # we peephole optimize to the method invocation
        return Attribute(self, k)

    def __call__(self, *args, **kwargs) -> 'Proxy':
        _create_friendly_names(args, 1)
        return self.tracer.create_proxy('call_method', '__call__', (self,) + args, kwargs)

    def __iter__(self) -> Iterable['Proxy']:
        frame = inspect.currentframe()
        assert frame is not None
        calling_frame = frame.f_back
        assert calling_frame is not None
        inst = list(dis.get_instructions(calling_frame.f_code))[calling_frame.f_lasti // 2]
        if inst.opname == 'UNPACK_SEQUENCE':
            return (self[i] for i in range(inst.argval))  # type: ignore

        return self.tracer.iter(self)

    def __bool__(self) -> bool:
        return self.tracer.to_bool(self)

    def keys(self):
        return self.tracer.keys(self)

    def __torch_function__(self, orig_method, types, args=None, kwargs=None):
        args = args if args else ()
        kwargs = kwargs if kwargs else {}
        _create_friendly_names(args, 1)
        _create_friendly_names(kwargs, 1)
        if torch.overrides.is_tensor_method_or_property(orig_method):
            return self.tracer.create_proxy('call_method', orig_method.__name__, args, kwargs)
        else:
            return self.tracer.create_proxy('call_function', orig_method, args, kwargs,
                                            name=self.tracer.graph._target_to_str(orig_method.__name__))

class Attribute(Proxy):
    def __init__(self, root: Proxy, attr: str):
        self.root = root
        self.attr = attr
        self.tracer = root.tracer
        self._node: Optional[Node] = None

    @property
    def node(self):
        # the node for attributes is added lazily, since most will just be method calls
        # which do not rely on the getitem call
        if self._node is None:
            self._node = self.tracer.create_proxy('call_function', getattr, (self.root, self.attr), {}).node
        return self._node

    def __call__(self, *args, **kwargs):
        _create_friendly_names(args, 1)
        return self.tracer.create_proxy('call_method', self.attr, (self.root,) + args, kwargs)

for method in magic_methods:
    def scope(method):
        def impl(*args, **kwargs):
            tracer = args[0].tracer
            target = getattr(operator, method)
            _create_friendly_names(args, 1)
            _create_friendly_names(kwargs, 1)
            return tracer.create_proxy('call_function', target, args, kwargs)
        impl.__name__ = method
        as_magic = f'__{method}__'
        setattr(Proxy, as_magic, impl)
    scope(method)

def _define_reflectable(orig_method_name):
    method_name = f'__r{orig_method_name}__'

    def impl(self, rhs):
        target = getattr(operator, orig_method_name)
        return self.tracer.create_proxy('call_function', target, (rhs, self), {})
    impl.__name__ = method_name
    impl.__qualname__ = method_name
    setattr(Proxy, method_name, impl)

for orig_method_name in reflectable_magic_methods:
    _define_reflectable(orig_method_name)
