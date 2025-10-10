import asyncio
import os
from io import TextIOWrapper

from django.core.management.base import BaseCommand
from asgiref.sync import async_to_sync
import anyio
from mcp import ClientSession, StdioServerParameters, stdio_server
from mcp.client.stdio import stdio_client

from mcp_server import mcp_server


async def _check_client():

    cl_wr, svr_rd = anyio.create_memory_object_stream(0)
    svr_wr, cl_rd = anyio.create_memory_object_stream(0)

    async def run_server():
        await mcp_server._mcp_server.run(
            svr_rd,
            svr_wr,
            mcp_server._mcp_server.create_initialization_options(),
        )

    async def run_client():
        async with ClientSession(cl_rd, cl_wr) as session:
            await session.initialize()

            print("Tools discovered in server:")
            tool_list = await session.list_tools()
            for tool in tool_list.tools:
                print(f'\n\t{tool.name}: {tool.description}')
                print(f'\tParameters: {tool.inputSchema}')

            print("Resources discovered in server:")
            resource_list = await session.list_resources()
            for resource in resource_list.resources:
                print(f'\n\t{resource.name}: {resource.description}')

            print("Prompts discovered in server:")
            prompt_list = await session.list_prompts()
            for prompt in prompt_list.prompts:
                print(f'\n\t{prompt.name}: {prompt.description}')

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_server)
        tg.start_soon(run_client)


_check_client_sync = async_to_sync(_check_client)


class Command(BaseCommand):
    help = 'Inspect installed tools, resources and prompts'

    def handle(self, *args, **options):
        _check_client_sync()
