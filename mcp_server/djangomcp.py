import contextvars
import functools
import inspect
import json
import logging
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Type, Callable

from asgiref.sync import sync_to_async, async_to_sync
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import QuerySet
from django.http import HttpResponse, HttpRequest
from mcp.server import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from rest_framework.mixins import CreateModelMixin, UpdateModelMixin, DestroyModelMixin, ListModelMixin
from rest_framework.serializers import Serializer
from starlette.datastructures import Headers
from starlette.types import Scope, Receive, Send

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

django_request_ctx = contextvars.ContextVar("django_request")



def drf_serialize_output(serializer_class: type[Serializer]):
    """
    This annotation will process the tool result thorugh the given DRF serializer

    ```
    @drf_serialize_output(MyDRFSerializer)
    def my_function(args):
        return MyInstance()
    ```


    :param serializer_class:
    :return:
    """

    def annotator(fn):
        fn.__dmcp_drf_serializer = serializer_class
        return fn

    return annotator


class _SyncToolCallWrapper:
    def __init__(self, fn):
        self.fn = fn
        functools.update_wrapper(self, fn)

    def __call__(self, *args, **kwargs):
        try:
            ret = self.fn(*args, **kwargs)
        except:
            # TODO create kind of exception like "ToolError" that is logged only debug
            logger.exception("Error in tool invocation")
            raise
        if isinstance(ret, QuerySet):
            ret = list(ret)
        serializer_class = getattr(self.fn, '__dmcp_drf_serializer', None)
        if serializer_class is not None:
            ret = serializer_class(ret).data
        return ret


async def _call_starlette_handler(django_request: HttpRequest, session_manager: StreamableHTTPSessionManager):
    """
    Adapts a Django request into a Starlette request and calls session_manager.handle_request.

    Returns:
        A Django HttpResponse
    """
    django_request_ctx.set(django_request)
    body = json.dumps(django_request.data, cls=DjangoJSONEncoder).encode("utf-8")

    # Build ASGI scope
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": django_request.method,
        "headers": [
                       (key.lower().encode("latin-1"), value.encode("latin-1"))
                       for key, value in django_request.headers.items() if key.lower() != "content-length"
                   ] + [("Content-Length", str(len(body)).encode("latin-1"))],
        "path": django_request.path,
        "raw_path": django_request.get_full_path().encode("utf-8"),
        "query_string": django_request.META["QUERY_STRING"].encode("latin-1"),
        "scheme": "https" if django_request.is_secure() else "http",
        "client": (django_request.META.get("REMOTE_ADDR"), 0),
        "server": (django_request.get_host(), django_request.get_port()),
    }

    async def receive() -> Receive:
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    # Prepare to collect send events
    response_started = {}
    response_body = bytearray()

    async def send(message: Send):
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
            response_started["headers"] = Headers(raw=message["headers"])
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    async with session_manager.run():
        # Call transport
        await session_manager.handle_request(scope, receive, send)

    # Build Django HttpResponse
    status = response_started.get("status", 500)
    headers = response_started.get("headers", {})

    response = HttpResponse(
        bytes(response_body),
        status=status,
    )
    for key, value in headers.items():
        response[key] = value

    return response


class _ToolsetMethodCaller:

    def __init__(self, class_, method_name, context_kwarg, forward_context_kwarg):
        self.class_ = class_
        self.method_name = method_name
        self.context_kwarg = context_kwarg
        self.forward_context_kwarg = forward_context_kwarg

    def __call__(self, *args, **kwargs):
        # Get the class instance
        instance = self.class_(context=kwargs[self.context_kwarg],
                               request=django_request_ctx.get(SimpleNamespace()))
        # Get the method
        method = sync_to_async(_SyncToolCallWrapper(getattr(instance, self.method_name)))
        if not self.forward_context_kwarg:
            # Remove the context kwarg from kwargs
            del kwargs[self.context_kwarg]

        return method(*args, **kwargs)


MCP_SESSION_ID_HDR = "Mcp-Session-Id"


# FIXME: shall I reimplement the necessary without the
# Stuff pulled to support embedded server ?
class DjangoMCP(FastMCP):

    def __init__(self, name=None, instructions=None, stateless=False):
        # Prevent extra server settings as we do not use the embedded server
        super().__init__(name or "django_mcp_server", instructions)
        self.stateless = stateless
        engine = import_module(settings.SESSION_ENGINE)
        self.SessionStore = engine.SessionStore
        
        # Optionally publish a tool that returns the global server instructions
        if getattr(settings, "DJANGO_MCP_GET_SERVER_INSTRUCTIONS_TOOL", True):
            async def _get_server_instructions():
                return self._mcp_server.instructions or ""

            self._tool_manager.add_tool(
                fn=_get_server_instructions,
                name="get_server_instructions",
                description="Return MCP server instructions (if any). Always call first."
            )

    @property
    def session_manager(self) -> StreamableHTTPSessionManager:
        return StreamableHTTPSessionManager(
            app=self._mcp_server,
            event_store=self._event_store,
            json_response=True,
            stateless=True,  # Sessions will be managed as Django sessions.
        )

    def handle_django_request(self, request):
        """
        Handle a Django request and return a response.
        This method is called by the Django view when a request is received.
        """
        if not self.stateless:
            session_key = request.headers.get(MCP_SESSION_ID_HDR)
            if session_key:
                session = self.SessionStore(session_key)
                if session.exists(session_key):
                    request.session = session
                else:
                    return HttpResponse(status=404, content="Session not found")
            elif request.data.get('method') == 'initialize':
                # FIXME: Trick to read body before data to avoid DRF complaining
                request.session = self.SessionStore()
            else:
                return HttpResponse(status=400, content="Session required for stateful server")

        result = async_to_sync(_call_starlette_handler)(request, self.session_manager)

        # Only persist and strip the session in stateful mode when we actually
        # added it to the request.
        if not self.stateless and hasattr(request, "session"):
            request.session.save()
            result.headers[MCP_SESSION_ID_HDR] = request.session.session_key
            delattr(request, "session")

        return result

    def destroy_session(self, request):
        session_key = request.headers.get(MCP_SESSION_ID_HDR)
        if not self.stateless and session_key:
            self.SessionStore(session_key).flush()
            request.session = None

    def append_instructions(self, new_instructions):
        """
        Append instructions to the server instructions.
        This method is called by the Django view when a request is received.
        """
        inst = self._mcp_server.instructions
        if not inst:
            inst = new_instructions
        else:
            inst = inst.strip() + "\n\n" + new_instructions.strip()
        self._mcp_server.instructions = inst

    def register_mcptoolset(self, toolset):
        return toolset._add_tools_to(self._tool_manager)

    def register_drf_create_tool(
            self,
            view_class: type("GenericAPIView"),
            name=None,
            instructions=None,
            body_schema: dict | None = None,
            actions: dict | None = None,
    ):
        """
        Function or Decorator to register a DRF CreateModelMixin view as a MCP Toolset.
        :param view_class: The DRF view subclassing CreateModelMixin.
        :param name: the tool name, can be auto generated
        :param instructions: the instructions to provide to the MCP client, mandatory if the view does not have a docstring.
        :param body_schema: JSON Schema, optional in reasonably recent DRF that supports schema generation.
                            If DRF does not support schema generation, this becomes mandatory
        :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is not a ViewSet
                        subclass. Example: {'post': 'create'}
        :return:
        """
        assert instructions or view_class.__doc__, "You need to provide instructions or the class must have a docstring"

        async def _dumb_create(body: dict):
            pass

        tool = self._tool_manager.add_tool(
            fn=_dumb_create,
            name=name or f"{view_class.__name__}_CreateTool",
            description=instructions or view_class.__doc__
        )
        tool.fn = sync_to_async(_DRFCreateAPIViewCallerTool(self, view_class, actions=actions))

        if body_schema is not None:
            tool.parameters['properties']['body'] = body_schema
        else:
            try:
                # Extract schema for a specific serializer manually
                tool.parameters['properties']['body'] = view_class.schema.map_serializer(view_class.serializer_class())
            except AttributeError:
                logger.warning(
                    "DRF version installed does not support schema generation, officially, trying privte API")
                try:
                    tool.parameters['properties']['body'] = view_class.schema._map_serializer(
                        view_class.serializer_class()
                    )
                except Exception:
                    logger.critical("DRF does not support schema generation, you must provide a body_schema parameter "
                                    "to the tool registration")
            except Exception:
                logger.critical(f"Error extracting schema for {view_class}, you must provide body_schema",
                                exc_info=True)
                raise

    def register_drf_list_tool(
            self,
            view_class: type("GenericAPIView"),
            name: str | None = None,
            instructions: str | None = None,
            actions: dict | None = None,
    ):
        assert instructions or view_class.__doc__, "You need to provide instructions or the class must have a docstring"

        async def _dumb_list():
            pass

        tool = self._tool_manager.add_tool(
            fn=_dumb_list,
            name=name or f"{view_class.__name__}_ListTool",
            description=instructions or view_class.__doc__
        )
        tool.fn = sync_to_async(_DRFListAPIViewCallerTool(self, view_class, actions=actions))

    def register_drf_update_tool(
            self,
            view_class: type("GenericAPIView"),
            name: str | None = None,
            instructions: str | None = None,
            body_schema: dict | None = None,
            actions: dict | None = None,
    ):
        """
        Function or Decorator to register a DRF CreateModelMixin view as a MCP Toolset.
        :param view_class: The DRF view subclassing CreateModelMixin.
        :param name: the tool name, can be auto generated
        :param instructions: the instructions to provide to the MCP client, mandatory if the view does not
                             have a docstring.
        :param body_schema: JSON Schema, optional in reasonably recent DRF that supports schema generation. If DRF does
                            not support schema generation, this becomes mandatory
        :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is
                        not a ViewSet subclass. Example: {'put': 'update'}
        :return:
        """
        assert instructions or view_class.__doc__, "You need to provide instructions or the class must have a docstring"

        async def _dumb_update(id, body: dict):
            pass

        tool = self._tool_manager.add_tool(
            fn=_dumb_update,
            name=name or f"{view_class.__name__}_UpdateTool",
            description=instructions or view_class.__doc__
        )
        tool.fn = sync_to_async(_DRFUpdateAPIViewCallerTool(self, view_class, actions=actions))

        # Extract schema for a specific serializer manually
        if body_schema is not None:
            tool.parameters['properties']['body'] = body_schema
        else:
            try:
                # Extract schema for a specific serializer manually
                tool.parameters['properties']['body'] = view_class.schema.map_serializer(view_class.serializer_class())
            except AttributeError:
                logger.warning(
                    "DRF version installed does not support schema generation, officially, trying privte API")
                try:
                    tool.parameters['properties']['body'] = view_class.schema._map_serializer(
                        view_class.serializer_class()
                    )
                except Exception:
                    logger.critical("DRF does not support schema generation, you must provide a body_schema parameter "
                                    "to the tool registration")
            except Exception:
                logger.critical(f"Error extracting schema for {view_class}, you must provide body_schema",
                                exc_info=True)
                raise

    def register_drf_destroy_tool(
            self,
            view_class: type("GenericAPIView"),
            name: str | None = None,
            instructions: str | None = None,
            actions: dict | None = None,
    ):
        assert instructions or view_class.__doc__, "You need to provide instructions or the class must have a docstring"

        async def _dumb_delete(id):
            pass

        tool = self._tool_manager.add_tool(
            fn=_dumb_delete,
            name=name or f"{view_class.__name__}_DeleteTool",
            description=instructions or view_class.__doc__
        )
        tool.fn = sync_to_async(_DRFDeleteAPIViewCallerTool(self, view_class, actions=actions))


global_mcp_server = DjangoMCP(**getattr(settings, 'DJANGO_MCP_GLOBAL_SERVER_CONFIG', {}))


class ToolsetMeta(type):
    registry = {}

    def __init__(cls, name, bases, namespace):
        super().__init__(name, bases, namespace)
        # Skip base class itself
        if name != "MCPToolset" and issubclass(cls, MCPToolset):
            ToolsetMeta.registry[name] = cls

    @staticmethod
    def iter_all():
        """
        Iterate over all toolsets
        """
        for name, cls in ToolsetMeta.registry.items():
            yield name, cls


class MCPToolset(metaclass=ToolsetMeta):
    """
    Base class for MCP toolsets. This class provides a way to create tools that can be used with
    the built in MCP serfver in a declarative way.

    ```
    class MyAppTools(MCPToolset):
        def my_tool(param : Type) -> ReturnType:
            ...
    ```

    Any "private" method (i.e. its name starting with _) will not be declared as a tool.
    Any other method is published as an MCP Tool that MCP Clients can use.

    During tool execution, self.request contains the original django request, this allows, for example,
    access to request.user ...

    """

    """You can define your own instance of DjangoMCP here """
    mcp_server: DjangoMCP = None

    def __init__(self, context=None, request=None):
        self.context = context
        self.request = request
        if self.mcp_server is None:
            self.mcp_server = global_mcp_server

    def _add_tools_to(self, tool_manager):
        """
        ADd tools to the manager
        :param tool_manager:
        :return: list of tools added
        """
        ret = []
        # ITerate all the methods whose name does not start with _ and register them with mcp_server.add_tool
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if not callable(method) or name.startswith("_"):
                continue
            tool = tool_manager.add_tool(sync_to_async(method))
            if tool.context_kwarg is None:
                forward_context = False
                tool.context_kwarg = "_context"
            else:
                forward_context = True
            tool.fn = _ToolsetMethodCaller(self.__class__, name, tool.context_kwarg, forward_context)
            ret.append(tool)
        return ret


def init():
    # Register the tools
    for _name, cls in ToolsetMeta.iter_all():
        if cls.mcp_server is None:
            cls.mcp_server = global_mcp_server

    for _name, cls in ToolsetMeta.iter_all():
        cls.mcp_server.register_mcptoolset(cls())

    from . import query_tool
    query_tool.init(global_mcp_server)


class _DRFRequestWrapper(HttpRequest):

    def __init__(self, mcp_server, mcp_request, method, body_json=None, id=None):
        super().__init__()
        self._serialized_body = json.dumps(body_json).encode("utf-8") if body_json else b''
        self.method = method
        self.content_type = "application/json"
        self.META = {
            'CONTENT_TYPE': 'application/json',
            'HTTP_ACCEPT': 'application/json',
            'CONTENT_LENGTH': len(self._serialized_body)
        }

        self._stream = BytesIO(self._serialized_body)
        self._read_started = False
        self.user = mcp_request.user
        self.session = mcp_request.session
        self.original_request = mcp_request
        self.path = f'/_djangomcpserver/{mcp_server.name}'
        if id:
            self.path += f"/{id}"


class BaseAPIViewCallerTool:
    view: Type["APIView"]

    @staticmethod
    def _patched_initialize_request(self, request, *args, **kwargs):
        original_request = request.original_request
        original_request.request = request
        original_request.method = request.method
        return original_request

    def __init__(self, view_class, **kwargs):
        view_class.initialize_request = self._patched_initialize_request
        self.view = view_class.as_view(**kwargs)


class _DRFCreateAPIViewCallerTool(BaseAPIViewCallerTool):
    def __init__(self, mcp_server, view_class, actions=None):
        if not issubclass(view_class, CreateModelMixin):
            raise ValueError(f"{view_class} must be a subclass of DRF CreateModelMixin")
        self.mcp_server = mcp_server
        self.view_class = view_class

        def raise_exception(exp):
            raise exp

        kwargs = dict(
            filter_backends=[],
            authentication_classes=[],
            permission_classes=view_class.permission_classes,
            handle_exception=raise_exception
        )
        if actions is not None:
            kwargs['actions'] = actions

        # Disable built in tauth
        super().__init__(view_class, **kwargs)

    def __call__(self, body: dict):
        # Create a request
        request = _DRFRequestWrapper(self.mcp_server, django_request_ctx.get(SimpleNamespace()), "POST", body_json=body)

        # Create the view
        try:
            return self.view(request).data
        except Exception as exp:
            logger.exception("Error in DRF tool invocation", exc_info=exp)
            raise exp


class _DRFListAPIViewCallerTool(BaseAPIViewCallerTool):
    def __init__(self, mcp_server, view_class, actions=None):
        if not issubclass(view_class, ListModelMixin):
            raise ValueError(f"{view_class} must be a subclass of DRF ListModelMixin")
        self.mcp_server = mcp_server
        self.view_class = view_class

        def raise_exception(exp):
            raise exp

        kwargs = dict(
            filter_backends=[],
            authentication_classes=[],
            permission_classes=view_class.permission_classes,
            handle_exception=raise_exception,
            pagination_class=None,
        )
        if actions is not None:
            kwargs['actions'] = actions

        # Disable built in tauth
        super().__init__(view_class, **kwargs)

    def __call__(self):
        # Create a request
        request = _DRFRequestWrapper(self.mcp_server, django_request_ctx.get(SimpleNamespace()), "GET")

        # Create the view
        try:
            return self.view(request).data
        except Exception as exp:
            logger.exception("Error in DRF tool invocation", exc_info=exp)
            raise exp


class _DRFUpdateAPIViewCallerTool(BaseAPIViewCallerTool):
    def __init__(self, mcp_server, view_class, actions=None):
        if not issubclass(view_class, UpdateModelMixin):
            raise ValueError(f"{view_class} must be a subclass of DRF UpdateModelMixin")
        self.mcp_server = mcp_server
        self.view_class = view_class

        def raise_exception(exp):
            raise exp

        kwargs = dict(
            filter_backends=[],
            authentication_classes=[],
            permission_classes=view_class.permission_classes,
            handle_exception=raise_exception
        )
        if actions is not None:
            kwargs['actions'] = actions

        # Disable built in tauth
        super().__init__(view_class, **kwargs)

    def __call__(self, id, body: dict):
        # Create a request
        request = _DRFRequestWrapper(self.mcp_server, django_request_ctx.get(SimpleNamespace()), "PUT", id=id,
                                     body_json=body)

        # Create the view
        try:
            return self.view(request, **{(self.view_class.lookup_url_kwarg or self.view_class.lookup_field): id}).data
        except Exception as exp:
            logger.exception("Error in DRF tool invocation", exc_info=exp)
            raise exp


class _DRFDeleteAPIViewCallerTool(BaseAPIViewCallerTool):
    def __init__(self, mcp_server, view_class, actions=None):
        if not issubclass(view_class, DestroyModelMixin):
            raise ValueError(f"{view_class} must be a subclass of DRF DestroyModelMixin")
        self.mcp_server = mcp_server
        self.view_class = view_class

        def raise_exception(exp):
            raise exp

        kwargs = dict(
            filter_backends=[],
            authentication_classes=[],
            permission_classes=view_class.permission_classes,
            handle_exception=raise_exception
        )
        if actions is not None:
            kwargs['actions'] = actions

        # Disable built in tauth
        super().__init__(view_class, **kwargs)

    def __call__(self, id):
        # Create a request
        request = _DRFRequestWrapper(self.mcp_server, django_request_ctx.get(SimpleNamespace()), "DELETE", id=id)

        # Create the view
        try:
            return self.view(request, **{(self.view_class.lookup_url_kwarg or self.view_class.lookup_field): id}).data
        except Exception as exp:
            logger.exception("Error in DRF tool invocation", exc_info=exp)
            raise exp


def drf_publish_create_mcp_tool(
        *args,
        name: str | None = None,
        instructions: str | None = None,
        server: DjangoMCP | None = None,
        body_schema: dict | None = None,
        actions: dict | None = None,
):
    """
    Function or Decorator to register a DRF CreateModelMixin view as an MCP Toolset.

    :param instructions: Instructions to provide to the MCP client.
    :param server: The server to use, if not set, the global one will be used.
    :param body_schema: JSON Schema, optional in reasonably recent DRF that supports schema generation.
                        If DRF does not support schema generation, this becomes mandatory
    :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is not a
                    ViewSet subclass. Example: {'post': 'create'}

    :return:
    """
    assert len(args) <= 1, "You must provide the DRF view or nothing as argument"

    def decorator(view_class):
        (server or global_mcp_server).register_drf_create_tool(
            view_class,
            name=name,
            instructions=instructions,
            body_schema=body_schema,
            actions=actions,
        )
        return view_class

    if args:
        decorator(args[0])
    else:
        return decorator


def drf_publish_list_mcp_tool(
        *args,
        name: str | None = None,
        instructions: str | None = None,
        server: DjangoMCP | None = None,
        actions: dict | None = None):
    """
    Function or Decorator to register a DRF ListModelMixin view as an MCP Toolset.

    :param instructions: Instructions to provide to the MCP client.
    :param server: The server to use, if not set, the global one will be used.
    :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is not a
                    ViewSet subclass. Example: {'get': 'list'}
    :return:
    """
    assert len(args) <= 1, "You must provide the DRF view or nothing as argument"

    def decorator(view_class):
        (server or global_mcp_server).register_drf_list_tool(
            view_class,
            name=name,
            instructions=instructions,
            actions=actions,
        )
        return view_class

    if args:
        decorator(args[0])
    else:
        return decorator


def drf_publish_update_mcp_tool(
        *args,
        name: str | None = None,
        instructions: str | None = None,
        server: DjangoMCP | None = None,
        body_schema: dict | None = None,
        actions: dict | None = None,
):
    """
    Function or Decorator to register a DRF UpdateModelMixin view as an MCP Toolset.

    :param instructions: Instructions to provide to the MCP client.
    :param server: The server to use, if not set, the global one will be used.
    :param body_schema: JSON Schema, optional in reasonably recent DRF that supports schema generation. If DRF does not
                        support schema generation, this becomes mandatory
    :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is not a ViewSet
                    subclass. Example: {'put': 'update'}'}
    :return:
    """
    assert len(args) <= 1, "You must provide the DRF view or nothing as argument"

    def decorator(view_class):
        (server or global_mcp_server).register_drf_update_tool(
            view_class,
            name=name,
            instructions=instructions,
            body_schema=body_schema,
            actions=actions,
        )
        return view_class

    if args:
        decorator(args[0])
    else:
        return decorator


def drf_publish_destroy_mcp_tool(
        *args,
        name: str | None = None,
        instructions: str | None = None,
        server: DjangoMCP | None = None,
        actions: dict | None = None,
):
    """
    Function or Decorator to register a DRF UpdateModelMixin view as an MCP Toolset.

    :param instructions: Instructions to provide to the MCP client.
    :param server: The server to use, if not set, the global one will be used.
    :param actions: DRF action mapping for ViewSet initialization. Omit if the class that is added is not a ViewSet
                    subclass. Example: {'delete': 'destroy'}'}
    :return:
    """
    assert len(args) <= 1, "You must provide the DRF view or nothing as argument"

    def decorator(view_class):
        (server or global_mcp_server).register_drf_destroy_tool(
            view_class,
            name=name,
            instructions=instructions,
            actions=actions,
        )
        return view_class

    if args:
        decorator(args[0])
    else:
        return decorator
