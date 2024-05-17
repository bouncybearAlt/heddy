from enum import Enum
from typing import Any, Optional
import openai
import time
import threading
import requests
import json
import logging
from heddy.application_event import ApplicationEvent, ApplicationEventType, ProcessingStatus
from heddy.state_manager import StateManager
from openai.lib.streaming import AssistantEventHandler
from openai.types.beta import Assistant, Thread
from openai.types.beta.threads import Run, RequiredActionFunctionToolCall, TextDelta
from openai.types.beta.assistant_stream_event import (
    ThreadRunRequiresAction, ThreadMessageDelta, ThreadRunCompleted,
    ThreadRunFailed, ThreadRunCancelling, ThreadRunCancelled, ThreadRunExpired, ThreadRunStepFailed,
    ThreadRunStepCancelled, ThreadRunStepDelta)
from dataclasses import dataclass
from heddy.io.sound_effects_player import AudioPlayer
from heddy.utils import encode_image_to_base64
from heddy.vision_module import VisionModule

class AssistantResultStatus(Enum):
    SUCCESS = 1
    ERROR = -1
    ACTION_REQUIED = 2 

class AvailableActions(Enum):
    ZAPIER=1
    PICTURE=2


@dataclass
class AssitsantResult:
    status: AssistantResultStatus
    response: Optional[str] = ""
    calls: Optional[Any] = None
    error: Optional[str] = ""
    
class ThreadManager:
    def __init__(self, client):
        self.client = client
        self.thread_id = None
        self.interaction_in_progress = False
        self.reset_timer = None

    def create_thread(self):
        if self.thread_id is not None and not self.interaction_in_progress:
            print(f"Using existing thread: {self.thread_id}")
            return self.thread_id

        try:
            thread = self.client.beta.threads.create()
            self.thread_id = thread.id
            print(f"New thread created: {self.thread_id}")
            return self.thread_id
        except Exception as e:
            print(f"Failed to create a thread: {e}")
            return None

    def add_message_to_thread(self, content):
        if not self.thread_id:
            print("No thread ID set. Cannot add message.")
            return

        if self.interaction_in_progress:
            print("Previous interaction still in progress. Please wait.")
            return

        try:
            message = self.client.beta.threads.messages.create(
                thread_id=self.thread_id,
                role="user",
                content=content
            )
            print(f"Message added to thread: {self.thread_id}")
        except Exception as e:
            print(f"Failed to add message to thread: {e}")

    def get_image_description(self, transcription, base64_image):
        """Sends the base64-encoded image along with the transcription to the OpenAI API and returns the description."""
        if base64_image:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.client.api_key}"
            }

            payload = {
                "model": "gpt-4-vision-preview",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": transcription},  # Use transcription as the prompt
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                        ]
                    }
                ],
                "max_tokens": 300
            }

            response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            if response.status_code == 200:
                try:
                    return response.json()['choices'][0]['message']['content']
                except KeyError:
                    return "Description not available or wrong response format."
            else:
                print(f"Error in OpenAI API call: {response.text}")
        return "Failed to encode image or image capture failed."

    def handle_interaction(self, content):
        if not self.thread_id or not self.interaction_in_progress:
            self.create_thread()
        self.add_message_to_thread(content)
        StateManager.last_interaction_time = time.time()  # Update the time with each interaction
        self.interaction_in_progress = True
        self.reset_last_interaction_time()

    def reset_thread(self):
        print("Resetting thread.")
        self.thread_id = None
        self.interaction_in_progress = False

    def reset_last_interaction_time(self):
        # This method resets the last interaction time and calls reset_thread after 90 seconds
        def reset():
            StateManager.last_interaction_time = None
            self.reset_thread()  # Reset the thread once the timer completes
            print("Last interaction time reset and thread reset")
            # Play the timer reset sound effect
            audio_player = AudioPlayer()
            audio_player.play_sound('timerreset.wav')  # Adjust the path as necessary
        
        # Cancel existing timer if it exists and is still running
        if self.reset_timer is not None and self.reset_timer.is_alive():
            self.reset_timer.cancel()
        
        # Create and start a new timer
        self.reset_timer = threading.Timer(90, reset)
        self.reset_timer.start()

    def end_of_interaction(self):
        # Call this method at the end of an interaction to reset the timer
        self.reset_last_interaction_time()




class StreamingManager:
    def __init__(self, thread_manager, eleven_labs_manager, assistant_id=None):
        self.thread_manager = thread_manager
        self.eleven_labs_manager = eleven_labs_manager
        self.assistant_id = assistant_id
        self.event_handler = None

    def set_event_handler(self, event_handler):
        self.event_handler = event_handler
    
    def func_name_to_application_event(self, func):
        if func.name == "send_text_message":
            return ApplicationEventType.ZAPIER
        elif func.name == "send_image_description":
            return ApplicationEventType.GET_SNAPSHOT
        else:
            raise NotImplementedError(f"{func.name=}")

    def resolve_calls(self, event):
        data = event.data
        action = data.required_action
        if action.type == "submit_tool_outputs":
            _tool_calls = action.submit_tool_outputs.tool_calls
            tool_calls = []
            for call in _tool_calls:
                func = call.function
                tool_calls.append({
                    "type": self.func_name_to_application_event(func),
                    "args": func.arguments,
                    "tool_call_id": call.id
                })
            
            # TODO: change to object
            return {
                "tools": tool_calls,
                "run_id": data.id,
                "thread_id": data.thread_id
            }
        raise NotImplementedError(f"{action.type=}")
    
    def submit_tool_calls_and_stream(self, result):
        return openai.beta.threads.runs.submit_tool_outputs_stream(
            tool_outputs=[{
                "output": call["output"],
                "tool_call_id": call["tool_call_id"]
            } for call in result["tools"]],
            run_id=result["run_id"],
            thread_id=result["thread_id"]
        )
    
    def handle_stream(self, streaming_manager):
        with streaming_manager as stream:
            for event in stream:
                if isinstance(event, ThreadMessageDelta) and event.data.delta.content:
                    delta = event.data.delta.content[0].text.value
                    self.text +=  delta if delta is not None else ""
                    continue
                if isinstance(event, ThreadRunRequiresAction):
                    print("ActionRequired")
                    return AssitsantResult(
                        calls=self.resolve_calls(event),
                        status=AssistantResultStatus.ACTION_REQUIED
                    )
                if isinstance(event, ThreadRunCompleted):
                    print("\nInteraction completed.")
                    self.thread_manager.interaction_in_progress = False
                    self.thread_manager.end_of_interaction()
                    return AssitsantResult(
                        response=self.text,
                        status=AssistantResultStatus.SUCCESS
                    )
                    # Exit the loop once the interaction is complete
                if isinstance(event, ThreadRunFailed):
                    print("\nInteraction failed.")
                    self.thread_manager.interaction_in_progress = False
                    self.thread_manager.end_of_interaction()
                    return AssitsantResult(
                        error="Generic OpenAI Error",
                        status=AssistantResultStatus.ERROR
                    )
                    # Exit the loop if the interaction fails
                # Add more event types as needed based on your application's requirements

    def handle_streaming_interaction(self, event: ApplicationEvent):
        if not self.assistant_id:
            print("Assistant ID is not set.")
            return
        if not self.thread_manager.thread_id:
            self.thread_manager.create_thread()
        
        content = event.request
        if event.type == ApplicationEventType.AI_INTERACT:
            if "image" in content:
                content = self.thread_manager.get_image_description(
                    content["text"],
                    encode_image_to_base64(content["image"])
                )
                print(content)
            else:
                content = content["text"]
            self.text = ""
            self.thread_manager.add_message_to_thread(content)
            manager = openai.beta.threads.runs.create_and_stream(
                thread_id=self.thread_manager.thread_id,
                assistant_id=self.assistant_id,
            )
        elif event.type == ApplicationEventType.AI_TOOL_RETURN:
            manager = self.submit_tool_calls_and_stream(event.request)
        
        result = self.handle_stream(manager)
        if result.status == AssistantResultStatus.ERROR:
            event.status = ProcessingStatus.ERROR
            event.error = result.error
        else:
            event.status = ProcessingStatus.SUCCESS
            event.result = result
        return event
