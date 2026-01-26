#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from http import HTTPMethod
from typing import Any, Dict, List, Optional, TypedDict, Union

import boto3
import uvicorn
from botocore.response import StreamingBody
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

# Import TURN credential management
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.turn_credential_manager import TurnCredentialManager
from shared.turn_credential_store import TurnCredentialStore
from shared.turn_providers import create_provider_from_env

load_dotenv(override=True)

# Note: lifespan is defined later in the file and assigned at the bottom
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Add your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# In-memory store of active sessions: session_id -> session info
active_sessions: Dict[str, Dict[str, Any]] = {}

# Initialize Bedrock client
bedrock = boto3.client("bedrock-agentcore")

# You can find this inside .bedrock_agentcore.yaml
AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN")

# Mount the frontend at /
app.mount("/client", SmallWebRTCPrebuiltUI)


# Ice servers
class IceServer(TypedDict, total=False):
    urls: Union[str, List[str]]
    username: Optional[str]
    credential: Optional[str]


# Global credential manager and store (initialized in lifespan)
credential_manager: Optional[TurnCredentialManager] = None
credential_store: Optional[TurnCredentialStore] = None


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/client/")


async def post_offer(request: Request, session_id: str):
    """Handle WebRTC offer requests."""

    data = await request.json()
    request = {"type": "offer", "data": data}

    response = bedrock.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        contentType="application/json",
        payload=json.dumps(request),
        runtimeSessionId=session_id,
    )

    answer_sdp = None

    if "text/event-stream" in response.get("contentType", ""):
        # Handle streaming response
        streaming_body: StreamingBody = response["response"]
        for line in streaming_body.iter_lines(chunk_size=1):
            if line:
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    line = line[6:]
                    print(f"Received line: {line}")
                    try:
                        event = json.loads(line)
                        print("Received event:", event)

                        # 4. Check for the 'answer' key
                        if "answer" in event:
                            payload = event["answer"]

                            if payload.get("type") == "answer":
                                answer_sdp = payload
                                print("WebRTC answer found. Stopping stream processing.")
                                # Break the line loop immediately
                                break

                    except json.JSONDecodeError:
                        print(f"Failed to parse extracted SSE payload as JSON: {line}")
                        pass

    if answer_sdp is None:
        raise HTTPException(500, "Did not find WebRTC answer in agent output")

    return answer_sdp


async def patch_offer(request: Request, session_id: str):
    """Handle WebRTC new ice candidate requests."""

    data = await request.json()
    request = {"type": "ice-candidates", "data": data}

    response = bedrock.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        contentType="application/json",
        payload=json.dumps(request),
        runtimeSessionId=session_id,
    )

    result = None

    if "text/event-stream" in response.get("contentType", ""):
        # Handle streaming response
        streaming_body: StreamingBody = response["response"]
        for line in streaming_body.iter_lines(chunk_size=1):
            if line:
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    line = line[6:]
                    print(f"Received line: {line}")
                    try:
                        # Assume the first valid JSON line is the result
                        result = json.loads(line)
                        print("Received event:", result)
                        break
                    except json.JSONDecodeError:
                        print(f"Failed to parse extracted SSE payload as JSON: {line}")
                        pass

    if result is None:
        raise HTTPException(500, "Did not get ICE candidate ack from agent")

    return result


@app.post("/start")
async def rtvi_start(request: Request):
    """Mimic Pipecat Cloud's /start endpoint."""

    class IceConfig(TypedDict):
        iceServers: List[IceServer]

    class StartBotResult(TypedDict, total=False):
        sessionId: str
        iceConfig: Optional[IceConfig]

    # Parse the request body
    try:
        request_data = await request.json()
        logger.debug(f"Received request: {request_data}")
    except Exception as e:
        logger.error(f"Failed to parse request body: {e}")
        request_data = {}

    # Store session info immediately in memory, replicate the behavior expected on Pipecat Cloud
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = request_data

    result: StartBotResult = {"sessionId": session_id}

    # Return dynamic ICE server credentials if requested
    if request_data.get("enableDefaultIceServers"):
        try:
            # Get fresh credentials from credential manager
            if credential_manager:
                credentials = await credential_manager.get_credentials()

                ice_servers = [
                    IceServer(
                        urls=credentials.urls,
                        username=credentials.username,
                        credential=credentials.credential,
                    )
                ]

                logger.debug(f"Providing ICE servers: {len(credentials.urls)} URLs from {credentials.provider}")
                result["iceConfig"] = IceConfig(iceServers=ice_servers)
            else:
                logger.warning("Credential manager not initialized, no ICE servers provided")
                raise HTTPException(500, "TURN credentials not available")

        except Exception as e:
            logger.error(f"Failed to get ICE servers: {e}")
            raise HTTPException(500, f"Failed to get ICE servers: {e}")

    return result


@app.api_route(
    "/sessions/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_request(
    session_id: str, path: str, request: Request, background_tasks: BackgroundTasks
):
    """Mimic Pipecat Cloud's proxy."""
    active_session = active_sessions.get(session_id)
    if active_session is None:
        return Response(content="Invalid or not-yet-ready session_id", status_code=404)

    if path.endswith("api/offer"):
        try:
            if request.method == HTTPMethod.POST.value:
                return await post_offer(request, session_id)
            elif request.method == HTTPMethod.PATCH.value:
                return await patch_offer(request, session_id)
        except Exception as e:
            logger.error(f"Failed to parse WebRTC request: {e}")
            return Response(content="Invalid WebRTC request", status_code=400)

    logger.info(f"Received request for path: {path}")
    return Response(status_code=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize and manage TURN credential manager lifecycle.

    Sets up the credential manager with the configured provider,
    starts the background refresh loop, and registers observers
    to update AWS Secrets Manager when credentials change.
    """
    global credential_manager, credential_store

    logger.info("Starting TURN credential management system")

    try:
        # 1. Initialize provider based on TURN_PROVIDER env var
        provider = create_provider_from_env()
        logger.info(f"Using TURN provider: {provider.get_provider_name()}")

        # 2. Initialize Secrets Manager store (if configured)
        secret_name = os.getenv("ICE_SERVER_CREDENTIALS_SECRET", "ice-server-credentials")
        region = os.getenv("AWS_REGION", "us-east-1")

        # Only initialize store if not using static provider
        if provider.get_provider_name() != "static":
            credential_store = TurnCredentialStore(secret_name, region)
            logger.info(f"Initialized Secrets Manager store: {secret_name} (region: {region})")

        # 3. Initialize credential manager
        refresh_buffer = int(os.getenv("ICE_SERVER_REFRESH_BUFFER", "300"))
        credential_manager = TurnCredentialManager(
            provider=provider,
            refresh_buffer_seconds=refresh_buffer
        )

        # 4. Register observer to update Secrets Manager (if store is configured)
        if credential_store:
            async def update_secrets(old_creds, new_creds):
                logger.debug("Updating credentials in Secrets Manager")
                await credential_store.update_credentials(new_creds)

            credential_manager.add_observer(update_secrets)

        # 5. Start background refresh
        await credential_manager.start()

        logger.info("TURN credential management system started successfully")

    except Exception as e:
        logger.error(f"Failed to initialize TURN credential management: {e}", exc_info=True)
        logger.warning("Server will start but TURN credentials may not be available")

    yield  # Run app

    # 6. Cleanup
    logger.info("Shutting down TURN credential management system")
    if credential_manager:
        await credential_manager.shutdown()
    logger.info("TURN credential management system shutdown complete")


# Register lifespan with the app
app.router.lifespan_context = lifespan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC demo")
    parser.add_argument(
        "--host", default="localhost", help="Host for HTTP server (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=7860, help="Port for HTTP server (default: 7860)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    logger.remove(0)
    if args.verbose:
        logger.add(sys.stderr, level="TRACE")
    else:
        logger.add(sys.stderr, level="DEBUG")

    uvicorn.run(app, host=args.host, port=args.port)
