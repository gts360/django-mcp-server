from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView

from mcp_server.djangomcp import global_mcp_server


@method_decorator(csrf_exempt, name='dispatch')
class MCPServerStreamableHttpView(APIView):
    mcp_server = global_mcp_server

    def get(self, request, *args, **kwargs):
        return self.mcp_server.handle_django_request(request)

    def post(self, request, *args, **kwargs):
        return self.mcp_server.handle_django_request(request)

    def delete(self, request, *args, **kwargs):
        self.mcp_server.destroy_session(request)
        return HttpResponse(status=200, content="Session destroyed")
