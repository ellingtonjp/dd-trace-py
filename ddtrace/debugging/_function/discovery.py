from collections import defaultdict
from collections import deque

from six import PY2
from wrapt.wrappers import FunctionWrapper


try:
    from typing import Protocol
except ImportError:
    from typing_extensions import Protocol  # type: ignore[assignment]

from os.path import abspath
from types import FunctionType
from types import ModuleType
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple
from typing import Type
from typing import Union
from typing import cast

from ddtrace.internal.compat import PYTHON_VERSION_INFO as PY
from ddtrace.internal.logger import get_logger
from ddtrace.internal.module import origin
from ddtrace.internal.safety import _isinstance
from ddtrace.internal.utils.inspection import linenos


log = get_logger(__name__)

FunctionContainerType = Union[type, property, classmethod, staticmethod, Tuple, ModuleType]

ContainerKey = Union[str, int, Type[staticmethod], Type[classmethod]]

CONTAINER_TYPES = (type, property, classmethod, staticmethod)

if PY < (3, 7):
    # DEV: Prior to Python 3.7 the ``cell_content`` attribute of ``Cell``
    # objects can only be mutated with the C API.
    import ctypes

    PyCell_Set = ctypes.pythonapi.PyCell_Set
    PyCell_Set.argtypes = (ctypes.py_object, ctypes.py_object)
    PyCell_Set.restype = ctypes.c_int

    set_cell_contents = PyCell_Set
else:

    def set_cell_contents(cell, contents):  # type: ignore[misc]
        cell.cell_contents = contents


class FullyNamed(Protocol):
    """A fully named object."""

    __name__: Optional[str] = None
    __fullname__: Optional[str] = None


class FullyNamedFunction(FullyNamed):
    """A fully named function object."""

    def __call__(self, *args, **kwargs):
        pass


class ContainerIterator(Iterator, FullyNamedFunction):
    """Wrapper around different types of function containers.

    A container comes with an origin, i.e. a parent container and a position
    within it in the form of a key.
    """

    def __init__(
        self,
        container: FunctionContainerType,
        origin: Optional[Union[Tuple["ContainerIterator", ContainerKey], Tuple[FullyNamedFunction, str]]] = None,
    ) -> None:
        if isinstance(container, (type, ModuleType)):
            self._iter = iter(container.__dict__.items())
            self.__name__ = container.__name__

        elif isinstance(container, tuple):
            self._iter = iter(enumerate(_.cell_contents for _ in container))  # type: ignore[arg-type]
            self.__name__ = "<locals>"

        elif isinstance(container, property):
            self._iter = iter(
                (m, getattr(container, a)) for m, a in {("getter", "fget"), ("setter", "fset"), ("deleter", "fdel")}
            )
            assert container.fget is not None  # nosec
            self.__name__ = container.fget.__name__

        elif isinstance(container, (classmethod, staticmethod)):
            self._iter = iter([(type(container), container.__func__)])  # type: ignore[list-item]
            self.__name__ = None

        else:
            raise TypeError("Unsupported container type: %s", type(container))

        self._container = container

        if origin is not None and origin[0].__fullname__ is not None:
            origin_fullname = origin[0].__fullname__
            self.__fullname__ = ".".join((origin_fullname, self.__name__)) if self.__name__ else origin_fullname
        else:
            self.__fullname__ = self.__name__

    def __iter__(self) -> Iterator[Tuple[ContainerKey, Any]]:
        return self._iter

    def __next__(self) -> Tuple[ContainerKey, Any]:
        return next(self._iter)

    next = __next__


UnboundMethodType = type(ContainerIterator.__init__) if PY2 else None


def _undecorate(f: FunctionType, name: str, path: str) -> FunctionType:
    # Find the original function object from a decorated function. We use the
    # expected function name to guide the search and pick the correct function.
    # The recursion is needed in case of multiple decorators. We make it BFS
    # to find the function as soon as possible.
    # DEV: We are deliberately not handling decorators that store the original
    #      function in __wrapped__ for now.
    if not (f.__code__.co_name == name and abspath(f.__code__.co_filename) == path):
        seen_functions = {f}
        q = deque([f])  # FIFO: use popleft and append

        while q:
            next_f = q.popleft()

            for g in (
                _.cell_contents for _ in (next_f.__closure__ or []) if _isinstance(_.cell_contents, FunctionType)
            ):
                if g.__code__.co_name == name and abspath(g.__code__.co_filename) == path:
                    return g
                if g not in seen_functions:
                    q.append(g)
                    seen_functions.add(g)

    return f


def _local_name(name: str, f: FunctionType) -> str:
    func_name = f.__name__
    if func_name.startswith("__") and name.endswith(func_name):
        # Quite likely a mangled name
        return func_name

    if name != func_name:
        # Brought into scope by an import, or a decorator
        return ".<alias>.".join((name, func_name))

    return func_name


def _collect_functions(module: ModuleType) -> Dict[str, FullyNamedFunction]:
    """Collect functions from a given module.

    All the collected functions are augmented with a ``__fullname__`` attribute
    to disambiguate the same functions assigned to different names.
    """
    path = origin(module)
    containers = deque([ContainerIterator(module)])
    functions = {}
    seen_containers = set()
    seen_functions = set()

    while containers:
        c = containers.popleft()

        if id(c._container) in seen_containers:
            continue
        seen_containers.add(id(c._container))

        for k, o in c:
            if PY2 and _isinstance(o, UnboundMethodType):
                o = o.__func__

            code = getattr(o, "__code__", None) if _isinstance(o, (FunctionType, FunctionWrapper)) else None
            if code is not None:
                local_name = _local_name(k, o) if isinstance(k, str) else o.__name__

                if o not in seen_functions:
                    seen_functions.add(o)
                    o = cast(FullyNamedFunction, o)
                    o.__fullname__ = ".".join((c.__fullname__, local_name)) if c.__fullname__ else local_name

                for name in (k, local_name) if isinstance(k, str) and k != local_name else (local_name,):
                    fullname = ".".join((c.__fullname__, name)) if c.__fullname__ else name
                    if fullname not in functions or abspath(code.co_filename) == path:
                        # Give precedence to code objects from the module and
                        # try to retrieve any potentially decorated function so
                        # that we don't end up returning the decorator function
                        # instead of the original function.
                        functions[fullname] = _undecorate(o, name, path) if name == k else o

                try:
                    if o.__closure__:
                        containers.append(ContainerIterator(o.__closure__, origin=(o, "<locals>")))
                except AttributeError:
                    pass

            elif _isinstance(o, CONTAINER_TYPES):
                if _isinstance(o, property) and not isinstance(o.fget, FunctionType):
                    continue
                containers.append(ContainerIterator(o, origin=(c, k)))

    return functions


class FunctionDiscovery(defaultdict):
    """Discover all function objects in a module.

    The discovered functions can be retrieved by line number or by their
    qualified name. In principle one wants to create a function discovery
    object per module and then cache the information. For this reason,
    instances of this class should be obtained with the ``from_module`` class
    method. This builds the discovery object and caches the information on the
    module object itself.
    """

    def __init__(self, module: ModuleType) -> None:
        super(FunctionDiscovery, self).__init__(list)
        self._module = module

        functions = _collect_functions(module)
        seen_functions = set()
        module_path = origin(module)

        self._fullname_index = {}

        for fname, function in functions.items():
            if (
                function not in seen_functions
                and abspath(cast(FunctionType, function).__code__.co_filename) == module_path
            ):
                # We only map line numbers for functions that actually belong to
                # the module.
                for lineno in linenos(cast(FunctionType, function)):
                    self[lineno].append(function)
            self._fullname_index[fname] = function
            seen_functions.add(function)

    def at_line(self, line: int) -> List[FullyNamedFunction]:
        """Get the functions at the given line.

        Note that, in general, there can be multiple copies of the same
        functions. This can happen as a result, e.g., of using decorators.
        """
        return self[line]

    def by_name(self, qualname: str) -> FullyNamedFunction:
        """Get the function by its qualified name."""
        fullname = ".".join((self._module.__name__, qualname))
        try:
            return self._fullname_index[fullname]
        except KeyError:
            raise ValueError("Function '%s' not found" % fullname)

    @classmethod
    def from_module(cls, module: ModuleType) -> "FunctionDiscovery":
        """Return a function discovery object from the given module.

        If this is called on a module for the first time, it caches the
        information on the module object itself. Subsequent calls will
        return the cached information.
        """
        # Cache the function tree on the module
        try:
            return module.__function_discovery__
        except AttributeError:
            fd = module.__function_discovery__ = cls(module)  # type: ignore[attr-defined]
            return fd
