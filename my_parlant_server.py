import asyncio
import parlant.sdk as p
from dotenv import load_dotenv
from parlant.adapters.nlp.azure_service import AzureService

load_dotenv()

async def start_conversation_server():

    async with p.Server(nlp_service=AzureService) as server:
        agent = await server.create_agent(
            name="Otto Carmen",
            description="You work at a car dealership",
        )
        print("Server is ready!")

asyncio.run(start_conversation_server())
