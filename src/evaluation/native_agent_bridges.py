"""Native framework adapters loaded only inside agent containers."""

import base64
import json
import os
import shutil
import uuid
from pathlib import Path

from agent_worker import auxiliary_snapshot, mutate_files, snapshot


def text_content(content):
    if isinstance(content, str):
        return content
    return "".join(
        item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        for item in (content or [])
    )


def pack_checkpoint(value):
    """Encode native checkpoint bytes/tuple keys as data, without pickle/code."""
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "value": [[pack_checkpoint(k), pack_checkpoint(v)] for k, v in value.items()],
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": "tuple" if isinstance(value, tuple) else "list",
            "value": [pack_checkpoint(v) for v in value],
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return {"type": "scalar", "value": value}
    raise TypeError(f"Unsupported native checkpoint value: {type(value).__name__}")


def unpack_checkpoint(value):
    kind, raw = value["type"], value["value"]
    if kind == "bytes":
        return base64.b64decode(raw, validate=True)
    if kind == "dict":
        return {unpack_checkpoint(k): unpack_checkpoint(v) for k, v in raw}
    if kind in {"tuple", "list"}:
        values = [unpack_checkpoint(v) for v in raw]
        return tuple(values) if kind == "tuple" else values
    if kind == "scalar" and (raw is None or isinstance(raw, (bool, int, float, str))):
        return raw
    raise ValueError("Invalid native checkpoint encoding")


class OpenHandsRuntime:
    def __init__(self, config, channel):
        from openhands.sdk import LLM, Conversation, Tool
        from openhands.sdk.tool.client_tool import ClientTool, ClientToolObservation, ClientToolSpec
        from openhands.sdk.tool.registry import register_tool
        from openhands.sdk.tool.tool import ToolExecutor
        from openhands.tools.preset.default import get_default_agent
        from pydantic import SecretStr

        self.channel = channel
        self.config = config
        self.events = []
        self.active = {}
        self.completed_calls = set()
        self.mock_names = {s["name"] for s in config["tools"]}
        runtime = self

        class MockExecutor(ToolExecutor):
            def __init__(self, name):
                self.name = name

            def __call__(self, action, conversation=None):
                args = action.model_dump(exclude={"kind", "summary", "security_risk"})
                reply = runtime.channel.rpc(
                    "mock", {"name": self.name, "arguments": args, "workspace": snapshot()}
                )
                mutate_files(reply.get("file_changes", []))
                result = reply["result"]
                return ClientToolObservation.from_text(
                    text=result if isinstance(result, str) else json.dumps(result)
                )

        llm = LLM(
            model="openai/" + config["model"],
            api_key=SecretStr("local-relay"),
            base_url="http://model.local/v1",
            api_mode="chat",
        )
        agent = get_default_agent(llm=llm, cli_mode=True)
        for spec in config["tools"]:
            definition = ClientTool.from_spec(ClientToolSpec(**spec))
            definition = definition.model_copy(update={"executor": MockExecutor(spec["name"])})
            register_tool(spec["name"], definition)
            agent.tools.append(Tool(name=spec["name"]))
        self.conversation = Conversation(
            agent=agent,
            workspace="/workspace",
            persistence_dir=None,
            callbacks=[self.on_event],
            max_iteration_per_run=config.get("max_iterations", 15),
            visualizer=None,
        )
        self.conversation._ensure_agent_ready()

    def on_event(self, event):
        from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent

        value = event.model_dump(mode="json")
        self.events.append(value)
        self.channel.emit({"event": "native_event", "native": value})
        if isinstance(event, ActionEvent):
            args = json.loads(event.tool_call.arguments)
            self.active[event.tool_call_id] = {
                "id": event.tool_call_id,
                "name": event.tool_name,
                "arguments": args,
            }
            self.channel.emit(
                {"event": "tool_start", **self.active[event.tool_call_id], "workspace": snapshot()}
            )
        elif isinstance(event, ObservationEvent):
            self.completed_calls.add(event.tool_call_id)
            result = text_content(event.observation.to_llm_content)
            self.channel.emit(
                {
                    "event": "tool_complete",
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "result": result,
                    "native_result": value,
                    "workspace": snapshot(),
                }
            )
        elif isinstance(event, MessageEvent) and event.source == "agent":
            self.channel.emit(
                {"event": "assistant_message", "content": text_content(event.llm_message.content)}
            )

    def catalog(self):
        return [tool.to_openai_tool() for tool in self.conversation.agent.tools_map.values()]

    def profile(self):
        return {
            "framework": "openhands",
            "entry_point": "SDK Conversation",
            "preset": "default cli_mode=True",
            "native_tools": True,
            "browser_enabled": False,
            "subagents_enabled": False,
            "native_control_internals": "unobserved",
            "transport": "network-disabled container; buffered HTTP relay",
        }

    def run(self, prompt):
        self.events = []
        self.conversation.send_message(prompt)
        self.conversation.run()
        answer = ""
        for event in self.events:
            if event.get("source") == "agent" and "llm_message" in event:
                answer = text_content(event["llm_message"].get("content")) or answer
            if event.get("tool_name") == "finish":
                action = event.get("action") or {}
                answer = action.get("message", answer)
        status = self.conversation.state.execution_status.value
        if status == "finished":
            for event in self.events:
                cid = event.get("tool_call_id")
                if event.get("tool_name") == "finish" and cid not in self.completed_calls:
                    # Native finish is handled by the agent loop, not a tool
                    # executor. Record the observed stop, with its actual layer.
                    self.channel.emit(
                        {
                            "event": "tool_complete",
                            "id": cid,
                            "name": "finish",
                            "result": {"native_terminal_action": True, "message": answer},
                            "workspace": snapshot(),
                            "observation_layer": "native_turn_completion",
                        }
                    )
                    self.completed_calls.add(cid)
        return {
            "final_response": answer,
            "native_status": status,
            "completed": status == "finished",
            "native_events": self.events,
            "messages": self.history(),
        }

    def reference(self, prompt, actions, final_response):
        from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent
        from openhands.sdk.llm import Message, TextContent

        self.events = []
        self.conversation.send_message(prompt)
        for call in actions:
            name, args = call["tool"], call.get("arguments", {})
            tool = self.conversation.agent.tools_map[name]
            action = tool.action_type.model_validate(args)
            ident = "ref_" + uuid.uuid4().hex
            event = ActionEvent(
                thought=[],
                action=action,
                tool_name=name,
                tool_call_id=ident,
                tool_call={
                    "id": ident,
                    "origin": "completion",
                    "name": name,
                    "arguments": json.dumps(args),
                },
                llm_response_id="authored_reference_" + uuid.uuid4().hex,
            )
            self.conversation._on_event_with_state_lock(event)
            result = tool(action, self.conversation)
            self.conversation._on_event_with_state_lock(
                ObservationEvent(
                    tool_name=name, tool_call_id=ident, action_id=event.id, observation=result
                )
            )
        self.conversation._on_event_with_state_lock(
            MessageEvent(
                source="agent",
                llm_message=Message(role="assistant", content=[TextContent(text=final_response)]),
            )
        )
        return {
            "final_response": final_response,
            "completed": True,
            "native_status": "authored_reference",
            "native_events": self.events,
        }

    def export(self):
        state = self.conversation.state
        return {
            "runtime": "openhands",
            "history": self.history(),
            "events": [e.model_dump(mode="json") for e in state.events],
            "state": {
                key: getattr(state, key)
                for key in (
                    "leaf_event_id",
                    "head_is_empty",
                    "agent_state",
                    "activated_knowledge_skills",
                    "activated_path_rules",
                )
            },
        }

    def history(self):
        from openhands.sdk.event import LLMConvertibleEvent

        return [
            message.to_chat_dict(
                cache_enabled=False,
                vision_enabled=True,
                function_calling_enabled=True,
                force_string_serializer=True,
                send_reasoning_content=True,
            )
            for message in LLMConvertibleEvent.events_to_messages(
                list(self.conversation.state.view.events)
            )
        ]

    def restore(self, state):
        from openhands.sdk import Conversation
        from openhands.sdk.event import Event

        if state["runtime"] != "openhands":
            raise ValueError("Mismatched native checkpoint runtime")
        # Follow native LocalConversation.fork: copy the full event tree into
        # an empty conversation, then restore HEAD and native derived state.
        # Re-emitting events would run hooks again and break parent identities.
        old_agent = self.conversation.agent
        agent = type(old_agent).model_validate(
            old_agent.model_dump(context={"expose_secrets": True})
        )
        self.conversation.close()
        self.conversation = Conversation(
            agent=agent,
            workspace="/workspace",
            persistence_dir=None,
            callbacks=[self.on_event],
            max_iteration_per_run=self.config.get("max_iterations", 15),
            visualizer=None,
        )
        native = self.conversation.state
        with native:
            for value in state["events"]:
                native.events.append(Event.model_validate(value))
            for key in (
                "leaf_event_id",
                "head_is_empty",
                "agent_state",
                "activated_knowledge_skills",
                "activated_path_rules",
            ):
                setattr(native, key, state["state"][key])
            native.rebuild_view()
        self.conversation._ensure_agent_ready()
        return {"restored_events": len(state["events"])}

    def truncate(self, keep):
        if keep != 0:
            raise ValueError("OpenHands adapter currently supports only zero-history truncation")
        self.conversation.navigate_to(None)
        return {"applied_method": "native_navigate_to_root", "retained_history_events": 0}

    def close(self):
        self.conversation.close()


class DeerFlowRuntime:
    """Native embedded lead agent, with its ordinary middleware and delegation.

    The local sandbox's 'host' is this disposable container. Research services
    are scenario tools; direct internet access is intentionally unavailable.
    """

    def __init__(self, config, channel):
        import yaml
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.tools import StructuredTool
        from langgraph.checkpoint.memory import InMemorySaver

        self.channel = channel
        self.thread_id = "workspace_1"
        # LangGraph counts middleware graph supersteps, not LLM iterations.
        self.recursion_limit = config.get("recursion_limit", 100)
        self.memory_flush_timeout = config.get("memory_flush_timeout", 30)
        if type(self.memory_flush_timeout) is not int or self.memory_flush_timeout < 1:
            raise ValueError("memory_flush_timeout must be a positive integer")
        self.events = []
        self.home = Path("/home/worker/profile")
        self.memory_root = self.home / "memory"
        self.home.mkdir(parents=True, exist_ok=True)
        os.environ["DEER_FLOW_PROJECT_ROOT"] = str(self.home)
        os.environ["DEER_FLOW_HOME"] = str(self.home / "state")
        runtime = self

        class NativeObservationMiddleware(AgentMiddleware):
            def observe_model(self, result):
                # The configured tool wrapper is inside native write guards.
                # Observe proposals separately so short-circuited calls are not
                # mistaken for calls the model never made. Wrapping the existing
                # call adds no graph nodes or recursion-budget consumption.
                for message in getattr(result, "result", [result]):
                    for call in getattr(message, "tool_calls", []):
                        channel.emit(
                            {
                                "event": "tool_attempt",
                                "id": call["id"],
                                "name": call["name"],
                                "arguments": call["args"],
                                "message_id": getattr(message, "id", None),
                                "observation_layer": "native_model_wrapper",
                            }
                        )

            def wrap_model_call(self, request, handler):
                result = handler(request)
                self.observe_model(result)
                return result

            async def awrap_model_call(self, request, handler):
                result = await handler(request)
                self.observe_model(result)
                return result

            def before(self, request):
                call = request.tool_call
                channel.emit(
                    {
                        "event": "tool_start",
                        "id": call["id"],
                        "name": call["name"],
                        "arguments": call["args"],
                        "workspace": snapshot(),
                        "observation_layer": "extension_tool_wrapper",
                        "auxiliary_workspace": auxiliary_snapshot(),
                    }
                )

            def after(self, request, result=None, error=None):
                from langchain_core.messages import ToolMessage
                from langgraph.types import Command

                call = request.tool_call
                observed = result
                command = None
                if isinstance(result, Command):
                    command = str(result)
                    update = result.update if isinstance(result.update, dict) else {}
                    matches = [
                        message
                        for message in update.get("messages", [])
                        if isinstance(message, ToolMessage) and message.tool_call_id == call["id"]
                    ]
                    if len(matches) == 1:
                        observed = matches[0]
                native = (
                    observed.model_dump(mode="json")
                    if hasattr(observed, "model_dump")
                    else str(observed)
                )
                channel.emit(
                    {
                        "event": "tool_complete",
                        "id": call["id"],
                        "name": call["name"],
                        "result": getattr(observed, "content", native),
                        "native_result": native,
                        **({"native_command": command} if command is not None else {}),
                        "error": error,
                        "workspace": snapshot(),
                        "observation_layer": "extension_tool_wrapper",
                        "auxiliary_workspace": auxiliary_snapshot(),
                    }
                )

            def wrap_tool_call(self, request, handler):
                self.before(request)
                try:
                    result = handler(request)
                except Exception as exc:
                    self.after(request, error=f"{type(exc).__name__}: {exc}")
                    raise
                self.after(request, result)
                return result

            async def awrap_tool_call(self, request, handler):
                self.before(request)
                try:
                    result = await handler(request)
                except Exception as exc:
                    self.after(request, error=f"{type(exc).__name__}: {exc}")
                    raise
                self.after(request, result)
                return result

        # DeerFlow's documented extension slot applies to lead and subagents.
        globals()["NativeObservationMiddleware"] = NativeObservationMiddleware

        def mock_function(name):
            def invoke(**arguments):
                reply = runtime.channel.rpc(
                    "mock", {"name": name, "arguments": arguments, "workspace": snapshot()}
                )
                mutate_files(reply.get("file_changes", []))
                return reply["result"]

            return invoke

        tool_configs = []
        for spec in config["tools"]:
            name = spec["name"]
            attr = "scenario_tool_" + name
            globals()[attr] = StructuredTool.from_function(
                func=mock_function(name),
                name=name,
                description=spec["description"],
                args_schema=spec["parameters"],
            )
            tool_configs.append({"name": name, "group": "web", "use": __name__ + ":" + attr})

        native_tools = [
            ("ls", "file:read"),
            ("read_file", "file:read"),
            ("glob", "file:read"),
            ("grep", "file:read"),
            ("write_file", "file:write"),
            ("str_replace", "file:write"),
            ("bash", "bash"),
        ]
        for name, group in native_tools:
            tool_configs.append(
                {"name": name, "group": group, "use": "deerflow.sandbox.tools:" + name + "_tool"}
            )
        # Unspecified settings retain the installed native defaults, including
        # middleware policies. No benchmark constraints enter the system prompt.
        settings = {
            "config_version": 64,
            "models": [
                {
                    "name": "primary",
                    "use": "langchain_openai:ChatOpenAI",
                    "model": config["model"],
                    "api_key": "local-relay",
                    "base_url": "http://model.local/v1",
                    "supports_thinking": False,
                }
            ],
            "tools": tool_configs,
            "tool_groups": [{"name": name} for name in ("web", "file:read", "file:write", "bash")],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
                "allow_host_bash": True,
            },
            "skills": {"path": "/opt/agent-source/skills"},
            "memory": {"backend_config": {"storage_path": str(self.memory_root)}},
            "extensions": {"middlewares": [__name__ + ":NativeObservationMiddleware"]},
        }
        path = self.home / "config.yaml"
        path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        from deerflow.client import DeerFlowClient
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        self.checkpointer = InMemorySaver()
        self.client = DeerFlowClient(
            config_path=str(path),
            checkpointer=self.checkpointer,
            model_name="primary",
            thinking_enabled=False,
            subagent_enabled=True,
            plan_mode=True,
        )
        user = get_effective_user_id()
        get_paths().ensure_thread_dirs(self.thread_id, user_id=user)
        self.audit_root = get_paths().sandbox_user_data_dir(self.thread_id, user_id=user)
        self.workspace_root = self.audit_root / "workspace"
        self.visible_workspace = "/mnt/user-data/workspace"
        self.user_id = user

    def profile(self):
        return {
            "framework": "deerflow",
            "entry_point": "DeerFlowClient embedded",
            "sandbox": "LocalSandboxProvider inside Docker",
            "host_bash": "container only",
            "subagents_enabled": True,
            "plan_mode": True,
            "checkpointer": "InMemorySaver",
            "native_control_internals": "unobserved",
            "tool_observation": "extension middleware; controls outside this slot may be unobserved",
            "tool_attempt_observation": "passive model-call wrapper, including calls rejected before the tool wrapper; outer middleware can suppress proposals and those suppressed before this slot may be unobserved",
            "research_sources": "scenario mock services; direct networking disabled",
            "visible_workspace": self.visible_workspace,
            "auxiliary_snapshot_roots": ["/mnt/user-data/uploads", "/mnt/user-data/outputs"],
            "transport": "network-disabled container; buffered HTTP relay",
            "graph_recursion_limit": self.recursion_limit,
            "memory_flush_timeout_seconds": self.memory_flush_timeout,
        }

    def catalog(self):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from langgraph.prebuilt import ToolNode

        # Match the compiled registry, as DeerFlow's own subagent executor does:
        # base tools omit middleware tools (for example write_todos), late skill
        # tools, and authorization/deferred-tool assembly decisions.
        self._graph_context()
        node = self.client._agent.get_graph().nodes.get("tools")
        if node is None or not isinstance(node.data, ToolNode):
            raise RuntimeError("Native DeerFlow compiled tool registry is unavailable")
        return [convert_to_openai_tool(tool) for tool in node.data.tools_by_name.values()]

    def run(self, prompt):
        from deerflow.client import _AIMessageAccumulator
        from langgraph.errors import GraphRecursionError

        self.events = []
        accumulator = _AIMessageAccumulator()
        ended = False
        try:
            for event in self.client.stream(
                prompt, thread_id=self.thread_id, recursion_limit=self.recursion_limit
            ):
                value = {"type": event.type, "data": event.data}
                self.events.append(value)
                self.channel.emit({"event": "native_event", "native": value})
                accumulator.observe(event)
                ended = ended or event.type == "end"
        except GraphRecursionError:
            return {
                "final_response": accumulator.answer(),
                "completed": False,
                "native_status": "recursion_limit",
                "native_events": self.events,
            }
        return {
            "final_response": accumulator.answer(),
            "completed": ended,
            "native_status": "finished" if ended else "incomplete_stream",
            "native_events": self.events,
            "messages": self.history(),
        }

    def reference(self, prompt, actions, final_response):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        config, context = self._graph_context()
        graph = self.client._agent
        # Execute native before-agent middleware, but stop before inference.
        graph.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
            context=context,
            interrupt_before=["model"],
        )
        for action in actions:
            ident = "ref_" + uuid.uuid4().hex
            self.channel.emit(
                {
                    "event": "tool_attempt",
                    "id": ident,
                    "name": action["tool"],
                    "arguments": action.get("arguments", {}),
                    "observation_layer": "authored_reference",
                }
            )
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": ident,
                        "type": "tool_call",
                        "name": action["tool"],
                        "args": action.get("arguments", {}),
                    }
                ],
            )
            graph.update_state(config, {"messages": [message]}, as_node="model")
            graph.invoke(
                None,
                config=config,
                context=context,
                interrupt_after=["tools"],
                interrupt_before=["model"],
            )
            state = graph.get_state(config).values
            if not any(
                isinstance(m, ToolMessage) and m.tool_call_id == ident for m in state["messages"]
            ):
                raise RuntimeError("Native reference action did not produce its tool result")
        graph.update_state(
            config, {"messages": [AIMessage(content=final_response)]}, as_node="model"
        )
        graph.invoke(None, config=config, context=context, interrupt_before=["model"])
        if graph.get_state(config).next:
            raise RuntimeError("Authored reference did not reach a native completed turn boundary")
        return {
            "final_response": final_response,
            "completed": True,
            "native_status": "authored_reference",
        }

    def _graph_context(self):
        config = self.client._get_runnable_config(
            self.thread_id, recursion_limit=self.recursion_limit
        )
        context = {"thread_id": self.thread_id, "user_id": self.user_id, "run_id": uuid.uuid4().hex}
        self.client._ensure_agent(config, context=context)
        return config, context

    def export(self):
        config, _ = self._graph_context()
        if self.client._agent.get_state(config).next:
            raise ValueError("Cannot checkpoint an unfinished native graph")
        from deerflow.agents.memory import get_memory_manager, reset_memory_manager

        manager = get_memory_manager()
        if not manager.shutdown_flush(timeout=self.memory_flush_timeout):
            raise ValueError(
                "Native memory did not quiesce; cannot create a valid branch checkpoint"
            )
        manager.close()
        reset_memory_manager()
        memory_files = {}
        for path in sorted(self.memory_root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Native memory checkpoint contains an unsupported symbolic link")
            if path.is_file():
                memory_files[path.relative_to(self.memory_root).as_posix()] = base64.b64encode(
                    path.read_bytes()
                ).decode("ascii")
        return {
            "runtime": "deerflow",
            "history": self.history(),
            "state": pack_checkpoint(
                {name: getattr(self.checkpointer, name) for name in ("storage", "writes", "blobs")}
            ),
            "thread_incarnations": self.client._thread_incarnations,
            "memory_files": memory_files,
            "checkpoint_method": "native graph checkpoint plus quiesced persistent memory",
        }

    def history(self):
        from langchain_core.messages import convert_to_openai_messages

        config, _ = self._graph_context()
        return convert_to_openai_messages(
            self.client._agent.get_state(config).values.get("messages", [])
        )

    def restore(self, state):
        if state["runtime"] != "deerflow":
            raise ValueError("Mismatched native checkpoint runtime")
        if self.checkpointer.storage or self.events:
            raise ValueError("Checkpoint restoration requires a fresh native runtime")
        restored = unpack_checkpoint(state["state"])
        from deerflow.agents.memory import get_memory_manager, reset_memory_manager

        manager = get_memory_manager()
        if not manager.shutdown_flush(timeout=self.memory_flush_timeout):
            raise ValueError("Cannot import over pending native memory work")
        manager.close()
        reset_memory_manager()
        if self.memory_root.resolve() != self.home.resolve() / "memory":
            raise ValueError("Unexpected native memory directory")
        if self.memory_root.exists():
            # A fresh manager creates empty index/schema files at initialization.
            # Replace only that new container's fixed memory root with the snapshot.
            shutil.rmtree(self.memory_root)
        for name, encoded in state["memory_files"].items():
            path = (self.memory_root / name).resolve()
            if (
                not path.is_relative_to(self.memory_root.resolve())
                or path == self.memory_root.resolve()
            ):
                raise ValueError("Memory checkpoint path escapes its directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded, validate=True))
        self.checkpointer.storage.clear()
        for thread, spaces in restored["storage"].items():
            for namespace, values in spaces.items():
                self.checkpointer.storage[thread][namespace].update(values)
        for name in ("writes", "blobs"):
            target = getattr(self.checkpointer, name)
            target.clear()
            target.update(restored[name])
        self.client._thread_incarnations = dict(state["thread_incarnations"])
        self._graph_context()
        return {"restored_threads": len(restored["storage"])}

    def truncate(self, keep):
        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        if keep != 0:
            raise ValueError("DeerFlow adapter currently supports only zero-history truncation")
        config, _ = self._graph_context()
        self.client._agent.update_state(
            config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]}
        )
        return {"applied_method": "native_remove_all_messages", "retained_history_events": 0}

    def close(self):
        # Native shutdown waits for any pending memory extraction before exit.
        from deerflow.agents.memory import get_memory_manager

        manager = get_memory_manager()
        if manager is not None:
            if not manager.shutdown_flush(timeout=self.memory_flush_timeout):
                raise RuntimeError("Native memory shutdown flush did not complete")
            manager.close()


def create_runtime(config, channel):
    if config["agent_kind"] == "openhands":
        return OpenHandsRuntime(config, channel)
    if config["agent_kind"] == "deerflow":
        return DeerFlowRuntime(config, channel)
    raise ValueError(f"Native runtime adapter not available: {config['agent_kind']}")
