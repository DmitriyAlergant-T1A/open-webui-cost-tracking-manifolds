"""
title: Anthropic Manifold Pipe - Custom
author: Dmitriy Alergant
author_url: https://github.com/... TBD
version: 0.1.0
required_open_webui_version: 0.3.17
license: MIT
"""

from pydantic import BaseModel, Field
from typing import Optional, Union, Generator, Iterator, AsyncGenerator

import traceback

from open_webui.utils.misc import (
    get_last_user_message,
    get_last_assistant_message,
    get_messages_content,
    pop_system_message,
)

from typing import Any, Awaitable, Callable, Optional

import os
import requests
import sys
import asyncio
import time
import json

from fastapi.responses import StreamingResponse

from collections.abc import Iterator


class Pipe:
    class Valves(BaseModel):
        ANTHROPIC_API_BASE_URL: str = Field(
            default="https://api.anthropic.com/v1",
            description="The base URL for Anthropic API endpoints.",
        )
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Required API key to access Anthropic API.",
        )
        DEBUG: bool = Field(default=False, description="Display debugging messages")

        pass

    def __init__(self):
        self.type = "manifold"
        self.id = "anthropic"
        self.name = "anthropic/"

        self.valves = self.Valves()

        self.debug_prefix = "DEBUG:    " + __name__ + " -"

        pass

    def get_anthropic_models(self):
        return [
            {"id": "claude-3-haiku-20240307", "name": "claude-3-haiku"},
            {"id": "claude-3-opus-20240229", "name": "claude-3-opus"},
            {"id": "claude-3-sonnet-20240229", "name": "claude-3-sonnet"},
            {"id": "claude-3-5-sonnet-20240620", "name": "claude-3.5-sonnet"},
            {"id": "claude-3-5-sonnet-20241022", "name": "claude-3.6-sonnet (3.5-new)"},
        ]

    def pipes(self):
        if self.valves.ANTHROPIC_API_KEY:
            try:
                # For now, return the hardcoded list of models
                models = self.get_anthropic_models()
                return models
            except Exception as e:
                print(f"Error: {e}")
                return [
                    {
                        "id": "error",
                        "name": "Could not fetch models from Anthropic, please update the API Key in the valves.",
                    },
                ]
        else:
            print(f"Anthropic Manifold function: ANTHROPIC_API_KEY valve not provided")
            return [
                {
                    "id": "error",
                    "name": f"Anthropic Manifold function: ANTHROPIC_API_KEY valve not provided",
                },
            ]

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __task__,
    ) -> Union[str, StreamingResponse]:

        if not self.valves.ANTHROPIC_API_KEY:
            raise Exception(
                f"Anthropic Manifold function: ANTHROPIC_API_KEY valve not provided"
            )

        # Initialize CostTrackingManager from "costs_tracking_util" function

        cost_tracker_module_name = "function_costs_tracking_util"
        if cost_tracker_module_name not in sys.modules:
            raise Exception(f"Module {cost_tracker_module_name} is not loaded")
        cost_tracker_module = sys.modules[cost_tracker_module_name]

        cost_tracking_manager = cost_tracker_module.CostTrackingManager(
            body["model"], __user__, __task__
        )

        # Remove the "anthropic/" prefix from the model name
        model_id = body["model"][body["model"].find(".") + 1 :]

        # Extract system message and pop from messages
        system_message, messages = pop_system_message(body["messages"])

        # Process messages as per the existing Anthropic plugin
        processed_messages = []
        image_count = 0
        total_image_size = 0

        for message in messages:
            processed_content = []
            if isinstance(message.get("content"), list):
                for item in message["content"]:
                    if item["type"] == "text":
                        processed_content.append({"type": "text", "text": item["text"]})
                    elif item["type"] == "image_url":
                        if image_count >= 5:
                            raise ValueError(
                                "Maximum of 5 images per API call exceeded"
                            )

                        processed_image = self.process_image(item)
                        processed_content.append(processed_image)

                        if processed_image["source"]["type"] == "base64":
                            image_size = len(processed_image["source"]["data"]) * 3 / 4
                        else:
                            image_size = 0

                        total_image_size += image_size
                        if total_image_size > 100 * 1024 * 1024:
                            raise ValueError(
                                "Total size of images exceeds 100 MB limit"
                            )

                        image_count += 1
            else:
                processed_content = [
                    {"type": "text", "text": message.get("content", "")}
                ]

            processed_messages.append(
                {"role": message["role"], "content": processed_content}
            )

        # Ensure the system_message is coerced to a string
        payload = {
            "model": model_id,
            "messages": processed_messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 0.8),
            "top_k": body.get("top_k", 40),
            "top_p": body.get("top_p", 0.9),
            "stop_sequences": body.get("stop", []),
            **({"system": str(system_message)} if system_message else {}),
            "stream": body.get("stream", False),
        }

        # Calculate Input Tokens (Estimated)
        input_content = get_messages_content(body["messages"])
        input_tokens = cost_tracking_manager.count_tokens(input_content) + len(
            body["messages"]
        )

        if self.valves.DEBUG:
            print(f"{self.debug_prefix} Anthropic Request Payload: {payload}")

        try:
            start_time = time.time()

            cost_tracking_manager.calculate_costs_update_status_and_persist(
                input_tokens=input_tokens,
                generated_tokens=None,
                reasoning_tokens=None,
                start_time=start_time,
                __event_emitter__=__event_emitter__,
                status="Requested...",
                persist_usage=False,
            )

            headers = {
                "x-api-key": self.valves.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            url = f"{self.valves.ANTHROPIC_API_BASE_URL}/messages"

            if body.get("stream", False):

                if self.valves.DEBUG:
                    print(f"{self.debug_prefix} sending a streaming request...")

                response = requests.post(
                    url=url,
                    headers=headers,
                    json=payload,
                    stream=True,
                )

                response.raise_for_status()

                if response.status_code != 200:
                    raise Exception(
                        f"HTTP Error {response.status_code}: {response.text()}"
                    )

                async def stream_generator():
                    streamed_content_buffer = ""
                    generated_tokens = 0
                    start_time = time.time()
                    last_update_time = 0
                    stream_completed = False

                    try:
                        for line in response.iter_lines():
                            if line:
                                line = line.decode("utf-8").strip()
                                if line.startswith("data: "):
                                    try:
                                        data = json.loads(line[6:])

                                        if data["type"] == "content_block_start":
                                            content = data["content_block"]["text"]
                                            streamed_content_buffer += content
                                            yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"

                                        elif data["type"] == "content_block_delta":
                                            delta = data["delta"]["text"]
                                            streamed_content_buffer += delta
                                            yield f"data: {json.dumps({'choices': [{'delta': {'content': delta}}]})}\n\n"

                                        elif data["type"] == "message_stop":
                                            break

                                        elif data["type"] == "message":
                                            for content_item in data.get("content", []):
                                                if content_item["type"] == "text":
                                                    content = content_item["text"]
                                                    streamed_content_buffer += content
                                                    yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"

                                    except json.JSONDecodeError:
                                        if self.valves.DEBUG:
                                            print(f"Failed to parse JSON: {line}")

                            current_time = time.time()
                            if current_time - last_update_time >= 1:

                                generated_tokens += cost_tracking_manager.count_tokens(
                                    streamed_content_buffer
                                )

                                cost_tracking_manager.calculate_costs_update_status_and_persist(
                                    input_tokens=input_tokens,
                                    generated_tokens=generated_tokens,
                                    reasoning_tokens=None,
                                    start_time=start_time,
                                    __event_emitter__=__event_emitter__,
                                    status="Streaming...",
                                    persist_usage=False,
                                )

                                streamed_content_buffer = ""
                                last_update_time = current_time

                        stream_completed = True
                        yield "data: [DONE]\n\n"

                    except GeneratorExit:
                        if self.valves.DEBUG:
                            print(
                                "DEBUG Anthropic stream_response wrapper... aborted by client"
                            )
                    except Exception as e:
                        _, _, tb = sys.exc_info()
                        line_number = tb.tb_lineno
                        print(f"Error on line {line_number}: {e}")
                        raise e

                    finally:

                        generated_tokens += cost_tracking_manager.count_tokens(
                            streamed_content_buffer
                        )

                        cost_tracking_manager.calculate_costs_update_status_and_persist(
                            input_tokens=input_tokens,
                            generated_tokens=generated_tokens,
                            reasoning_tokens=None,
                            start_time=start_time,
                            __event_emitter__=__event_emitter__,
                            status="Completed" if stream_completed else "Stopped",
                            persist_usage=True,
                        )

                        if self.valves.DEBUG:
                            print(
                                f"DEBUG Finalized stream (completed: {stream_completed})"
                            )

                return StreamingResponse(
                    stream_generator(), media_type="text/event-stream"
                )

            else:
                # Non-streaming response
                if self.valves.DEBUG:
                    print(f"{self.debug_prefix} sending non-stream request...")

                response = requests.post(
                    url=url,
                    headers=headers,
                    json=payload,
                    stream=True,
                )

                response.raise_for_status()

                if response.status_code != 200:
                    raise Exception(
                        f"HTTP Error {response.status_code}: {response.text}"
                    )

                res = response.json()

                if self.valves.DEBUG:
                    print(f"{self.debug_prefix} Anthropic response: {res}")

                generated_text = ""

                for content_item in res.get("content", []):
                    if content_item["type"] == "text":
                        generated_text += content_item["text"]

                # Try to get generated tokens from usage response, if not available default to input_tokens we already had
                input_tokens = res.get("usage", {}).get("input_tokens", input_tokens)

                # Try to get generated tokens from usage response, if not available default to our own calculations
                generated_tokens = res.get("usage", {}).get(
                    "output_tokens", cost_tracking_manager.count_tokens(generated_text)
                )

                cost_tracking_manager.calculate_costs_update_status_and_persist(
                    input_tokens=input_tokens,
                    generated_tokens=generated_tokens,
                    reasoning_tokens=None,
                    start_time=start_time,
                    __event_emitter__=__event_emitter__,
                    status="Completed",
                    persist_usage=True,
                )

                if self.valves.DEBUG:
                    print(
                        f"{self.debug_prefix} returning non-stream response: {generated_text}"
                    )

                return generated_text

        except Exception as e:
            _, _, tb = sys.exc_info()
            line_number = tb.tb_lineno
            print(f"Error on line {line_number}: {e}")
            raise e

    def process_image(self, image_data):
        if image_data["image_url"]["url"].startswith("data:image"):
            mime_type, base64_data = image_data["image_url"]["url"].split(",", 1)
            media_type = mime_type.split(":")[1].split(";")[0]
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_data,
                },
            }
        else:
            return {
                "type": "image",
                "source": {"type": "url", "url": image_data["image_url"]["url"]},
            }
