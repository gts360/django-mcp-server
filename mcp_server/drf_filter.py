"""
django-filter integration for django-mcp-server.

Provides ``register_drf_filter_tool`` which registers a DRF
``ListModelMixin`` ViewSet as a filtered-list MCP tool, exposing every
declared django-filter parameter as a typed, optional tool input.

``django-filter`` is an **optional** dependency -- an ``ImportError`` is
raised only when ``register_drf_filter_tool`` is actually called without
the package installed.
"""

import inspect
import logging
from typing import Optional

from asgiref.sync import sync_to_async
from rest_framework.mixins import ListModelMixin

logger = logging.getLogger(__name__)

# ── Optional dependency guard ────────────────────────────────────────

_DJANGO_FILTERS_INSTALL_MSG = (
    "django-filter is required for register_drf_filter_tool. "
    "Install it with:  pip install django-filter"
)


def _require_django_filters():
    """Raise ``ImportError`` if django-filter is not installed."""
    try:
        import django_filters  # noqa: F401
    except ImportError:
        raise ImportError(_DJANGO_FILTERS_INSTALL_MSG)


# ── Helpers ──────────────────────────────────────────────

def _filter_to_python_type(f):
    """Map a django-filter Filter instance to a Python type for signatures."""
    from django_filters import BooleanFilter, NumberFilter
    if isinstance(f, BooleanFilter):
        return bool
    if isinstance(f, NumberFilter):
        return float
    return str


def _resolve_filterset(view_class):
    """Return a FilterSet class for *view_class*, or ``None``.

    Checks ``filterset_class`` first, then auto-generates one from
    ``filterset_fields`` if present.
    """
    fs = getattr(view_class, "filterset_class", None)
    if fs is not None:
        return fs

    fields = getattr(view_class, "filterset_fields", None)
    if fields is not None:
        from django_filters.rest_framework import FilterSet
        model = view_class.serializer_class.Meta.model
        meta = type("Meta", (), {"model": model, "fields": list(fields)})
        return type(f"{model.__name__}AutoFilter", (FilterSet,), {"Meta": meta})

    return None


def _resolve_queryset(view_class):
    """Return the base queryset for *view_class*.

    Uses the class-level ``queryset`` attribute when available (preserving
    pre-filters like ``Company.objects.filter(is_duplicate=False)``),
    otherwise falls back to ``Model.objects.all()``.
    """
    qs = getattr(view_class, "queryset", None)
    if qs is not None:
        return qs
    return view_class.serializer_class.Meta.model.objects.all()


# ── Caller ───────────────────────────────────────────────

class _DRFFilterCallerTool:
    """Callable that filters a ViewSet's queryset using its FilterSet
    and serialises the results.

    Instantiated once per tool registration; called on every tool
    invocation with the agent-supplied keyword arguments.
    """

    def __init__(self, view_class, filterset_class, max_limit):
        self.serializer_class = view_class.serializer_class
        self.filterset_class = filterset_class
        self.max_limit = max_limit
        self.base_qs = _resolve_queryset(view_class)

    def __call__(self, **kwargs):
        limit = min(kwargs.pop("limit", self.max_limit), self.max_limit)
        qs = self.base_qs.all()

        if self.filterset_class is not None:
            clean = {k: v for k, v in kwargs.items() if v is not None}
            if clean:
                fs = self.filterset_class(data=clean, queryset=qs)
                qs = fs.qs if fs.is_valid() else qs.none()

        return self.serializer_class(qs[:limit], many=True).data


# ── Registration ─────────────────────────────────────────

def register_drf_filter_tool(
    server,
    view_class,
    name=None,
    instructions=None,
    actions=None,
    max_limit=50,
):
    """Register a DRF ``ListModelMixin`` ViewSet as a filtered-list MCP tool.

    Reads ``filterset_class`` (or ``filterset_fields``) from *view_class*
    and exposes every declared filter as a typed, optional tool parameter.
    Results are capped at *max_limit*.

    Requires ``django-filter``.  An ``ImportError`` is raised if the
    package is not installed.

    The function builds a proper ``inspect.Signature`` on the async wrapper
    so that the MCP library's ``func_metadata`` creates the correct Pydantic
    ``arg_model``, ensuring arguments are validated *and* forwarded to the
    tool callable.

    :param server: ``DjangoMCP`` instance.
    :param view_class: DRF ViewSet subclassing ``ListModelMixin``.
    :param name: Tool name; auto-generated as ``search_<model>`` if omitted.
    :param instructions: Description shown to agents; falls back to the
        ViewSet's docstring.
    :param actions: Reserved for forward-compatibility with ViewSet routing.
    :param max_limit: Hard cap on returned rows.
    """
    _require_django_filters()

    if not issubclass(view_class, ListModelMixin):
        raise ValueError(f"{view_class} must be a subclass of DRF ListModelMixin")

    assert instructions or view_class.__doc__, (
        "You need to provide instructions or the class must have a docstring"
    )

    description = instructions or view_class.__doc__
    model = view_class.serializer_class.Meta.model
    tool_name = name or f"search_{model.__name__.lower()}"

    filterset_class = _resolve_filterset(view_class)
    caller = _DRFFilterCallerTool(view_class, filterset_class, max_limit)
    _async_caller = sync_to_async(caller)

    sig_params = []
    if filterset_class is not None:
        for fname, filt in filterset_class.base_filters.items():
            py_type = _filter_to_python_type(filt)
            sig_params.append(inspect.Parameter(
                fname,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Optional[py_type],
            ))

    sig_params.append(inspect.Parameter(
        "limit",
        inspect.Parameter.KEYWORD_ONLY,
        default=max_limit,
        annotation=int,
    ))

    async def _filter_tool(**kwargs):
        return await _async_caller(**kwargs)

    _filter_tool.__signature__ = inspect.Signature(sig_params)
    _filter_tool.__name__ = tool_name
    _filter_tool.__doc__ = description

    server._tool_manager.add_tool(
        fn=_filter_tool,
        name=tool_name,
        description=description,
    )
